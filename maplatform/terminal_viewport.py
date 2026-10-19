"""Read-only terminal panes with scrolling measured in painted rows.

Keep this optional prompt_toolkit module out of the core/--plain import path.
Wrapping before Window renders avoids its source-line/cursor scrolling rules.
"""
from bisect import bisect_right

from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout import ScrollOffsets, VSplit, Window
from prompt_toolkit.layout.controls import UIContent, UIControl
from prompt_toolkit.mouse_events import MouseButton, MouseEventType, MouseModifier
from prompt_toolkit.utils import get_cwidth


def wrap_fragments(fragments, width):
    """Return styled rows, row offsets, and source spans for painted characters."""
    width = max(1, width)
    source = "".join(text for _, text in fragments)
    lines, offsets, positions = [[]], [0], [[]]
    column = offset = 0

    def append(style, text):
        row = lines[-1]
        if row and row[-1][0] == style:
            row[-1] = (style, row[-1][1] + text)
        else:
            row.append((style, text))

    for style, text in fragments:
        for char in text:
            if char == "\n":
                lines.append([])
                offsets.append(offset + 1)
                positions.append([])
                column = 0
            else:
                # The renderer does not expand tabs itself. Use explicit tab stops.
                display = " " * (4 - column % 4) if char == "\t" else char
                for glyph in display:
                    cells = get_cwidth(glyph)
                    # Window drops a combining mark if its base fills the final
                    # cell. Leave that cluster room, even across style fragments.
                    combining_next = (char != "\t" and offset + 1 < len(source)
                                      and source[offset + 1] not in "\n\t"
                                      and get_cwidth(source[offset + 1]) == 0)
                    reserve = int(combining_next and cells < width)
                    if cells and column + cells + reserve > width and column:
                        lines.append([])
                        offsets.append(offset)
                        positions.append([])
                        column = 0
                    if cells > width:  # A two-cell glyph cannot fit a one-cell pane.
                        glyph, cells = "�", 1
                    append(style, glyph)
                    positions[-1].append((offset, offset + 1))
                    column += cells
            offset += 1
    return lines, offsets, positions


def remap_anchor(old, new, anchor):
    """Keep the viewed text anchored through appends, edits and history trimming."""
    prefix = 0
    limit = min(len(old), len(new))
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    if anchor <= prefix:
        return anchor
    suffix = 0
    while suffix < limit - prefix and old[-suffix - 1] == new[-suffix - 1]:
        suffix += 1
    if anchor >= len(old) - suffix:
        return max(0, anchor + len(new) - len(old))
    # At the display cap, an append also removes the oldest prefix. Find the
    # retained suffix/prefix overlap in linear time, including repetitive text.
    # Prefix/suffix comparison alone cannot recognise this moving window.
    overlap = suffix_prefix_overlap(old, new)
    if overlap >= min(32, max(1, limit // 2)):
        return max(0, anchor - (len(old) - overlap))
    return min(anchor, len(new))


def suffix_prefix_overlap(old, new):
    if not new:
        return 0
    failure = [0] * len(new)
    matched = 0
    for index in range(1, len(new)):
        while matched and new[index] != new[matched]:
            matched = failure[matched - 1]
        if new[index] == new[matched]:
            matched += 1
        failure[index] = matched
    matched = 0
    for char in old:
        while matched and (matched == len(new) or char != new[matched]):
            matched = failure[matched - 1]
        if char == new[matched]:
            matched += 1
    return matched


class ScrollableText(UIControl):
    """A non-focusable viewport; the input composer always owns the real cursor."""

    def __init__(self, *, follow=True, selectable=False):
        self.fragments = ()
        self.text = ""
        self.lines, self.offsets = [[]], [0]
        self.positions = [[]]
        self.selectable = selectable
        self._selection_anchor = self._selection_focus = None
        self._dragging = False
        self._pending_fragments = None
        self._laid_fragments = ()
        self._laid_text = ""
        self.top = 0
        self.follow = follow
        self.height = 1
        self._width = None
        self._dirty = True
        self._anchor = 0

    @property
    def max_top(self):
        return max(0, len(self.lines) - self.height)

    def set_fragments(self, fragments, *, reset=False):
        fragments = tuple(fragments)
        if reset:
            self.clear_selection(apply_pending=False)
        elif self.selection_active:
            # A selection always refers to the exact text the user saw. Status
            # and other panes may keep updating; this pane catches up on clear.
            self._pending_fragments = fragments
            return
        if fragments != self.fragments:
            text = "".join(value for _, value in fragments)
            if not self.follow:
                self._anchor = remap_anchor(self.text, text, self._anchor)
            self.text, self.fragments = text, fragments
            self._dirty = True
        if reset:
            self.follow = True

    def create_content(self, width, height):
        width = max(1, width)
        self.height = max(1, height)
        if self._dirty or width != self._width:
            self.lines, self.offsets, self.positions = wrap_fragments(self.fragments, width)
            self._laid_fragments, self._laid_text = self.fragments, self.text
            self._width, self._dirty = width, False
        if self.follow:
            self.top = self.max_top
        else:
            # Retain the character anchor across repeated resizes, rather than
            # replacing it with the start of each newly wrapped row (which drifts).
            self.top = min(self.max_top, max(0, bisect_right(self.offsets, self._anchor) - 1))
        return UIContent(get_line=self._render_line, line_count=len(self.lines),
                         cursor_position=Point(x=0, y=self.top), show_cursor=False)

    @property
    def selection_active(self):
        return self._selection_anchor is not None

    @property
    def selection_range(self):
        if not self.selection_active:
            return None
        a, b = self._selection_anchor, self._selection_focus
        return min(a[0], b[0]), max(a[1], b[1])

    def selection_text(self):
        selected = self.selection_range
        return self.text[selected[0]:selected[1]] if selected else ""

    def clear_selection(self, *, apply_pending=True):
        self._selection_anchor = self._selection_focus = None
        self._dragging = False
        pending, self._pending_fragments = self._pending_fragments, None
        if apply_pending and pending is not None:
            self.set_fragments(pending)

    def _render_line(self, row):
        fragments = self.lines[row]
        if not self.selectable:
            return fragments
        selected = self.selection_range
        result = []
        index = 0
        for style, text in fragments:
            for char in text:
                start, end = self.positions[row][index]
                mark = selected and start < selected[1] and end > selected[0]
                char_style = style + " class:selection" if mark else style
                if result and result[-1][0] == char_style:
                    result[-1] = (char_style, result[-1][1] + char)
                else:
                    result.append((char_style, char))
                index += 1
        # Empty rows and end-of-line padding need a mouse hit target. This
        # sentinel is only painted, never part of the source/copied text.
        result.append(("", " "))
        return result

    def _span_at(self, point):
        row = max(0, min(len(self.lines) - 1, point.y))
        spans = self.positions[row]
        if not spans or point.x >= len(spans):
            end = spans[-1][1] if spans else self.offsets[row]
            return end, end
        start, end = spans[max(0, point.x)]
        # Keep combining marks with their base character in the clipboard.
        while start > 0 and self.text[start] not in "\n\t" and get_cwidth(self.text[start]) == 0:
            start -= 1
        while end < len(self.text) and self.text[end] not in "\n\t" and get_cwidth(self.text[end]) == 0:
            end += 1
        return start, end

    def _extend_selection(self, point):
        self._selection_focus = self._span_at(point)

    def _keep_painted_source(self):
        if self._dirty:
            # A queued model update may have arrived since the last paint. A
            # mouse event still refers to the cells actually shown to the user.
            self._pending_fragments = self.fragments
            self.fragments, self.text = self._laid_fragments, self._laid_text
            self._dirty = False

    def scroll_to(self, row):
        self.top = max(0, min(self.max_top, row))
        self.follow = self.top == self.max_top
        self._anchor = self.offsets[self.top]

    def scroll_by(self, rows):
        self.scroll_to(self.top + rows)

    def page(self, direction):
        self.scroll_by(direction * max(1, self.height - 1))

    def to_start(self):
        self.scroll_to(0)

    def to_end(self):
        self.scroll_to(self.max_top)

    def mouse_handler(self, mouse_event):
        kind = mouse_event.event_type
        point = mouse_event.position
        if kind in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            old_top = self.top
            old_row = max(0, min(len(self.lines) - 1, point.y))
            old_line = "".join(t for _, t in self.lines[old_row])
            cell_column = get_cwidth(old_line[:point.x])
            self.scroll_by(-3 if kind == MouseEventType.SCROLL_UP else 3)
            if self._dragging:
                row = min(len(self.lines) - 1, max(0, point.y + self.top - old_top))
                column = cells = 0
                for char in "".join(t for _, t in self.lines[row]):
                    if cells + get_cwidth(char) > cell_column:
                        break
                    cells += get_cwidth(char)
                    column += 1
                self._extend_selection(Point(x=column, y=row))
        elif self.selectable:
            if kind == MouseEventType.MOUSE_DOWN and mouse_event.button == MouseButton.LEFT:
                self._keep_painted_source()
                span = self._span_at(point)
                if MouseModifier.SHIFT not in mouse_event.modifiers or not self.selection_active:
                    self._selection_anchor = span
                self._selection_focus = span
                self._dragging = True
                self.follow = False
                self._anchor = self.offsets[self.top]
            elif kind == MouseEventType.MOUSE_MOVE:
                if self._dragging and mouse_event.button == MouseButton.LEFT:
                    self._extend_selection(point)
                else:
                    # Mouse release outside this pane is delivered elsewhere.
                    self._dragging = False
            elif kind == MouseEventType.MOUSE_UP:
                if self._dragging:
                    self._extend_selection(point)
                self._dragging = False
        # Consume mouse events: never invoke Window's cursor/source-line scrolling.
        # Only explicitly selectable panes expose content to clipboard actions.
        return None


class ViewportScrollbar(UIControl):
    """Interactive scrollbar with the same display-row range as its pane."""

    def __init__(self, viewport):
        self.viewport = viewport
        self.height = 1
        self.thumb_top = self.thumb_size = self.travel = 0
        self._drag_offset = None

    def create_content(self, width, height):
        self.height = max(1, height)
        track = max(0, height - 2)
        view = self.viewport
        self.thumb_size = min(track, max(1, round(track * view.height / len(view.lines))))
        self.travel = track - self.thumb_size
        self.thumb_top = round(self.travel * view.top / view.max_top) if view.max_top else 0
        rows = []
        for row in range(height):
            if row in (0, height - 1):
                rows.append([("class:scrollbar.arrow", "^" if row == 0 else "v")])
            else:
                thumb = self.thumb_top <= row - 1 < self.thumb_top + self.thumb_size
                rows.append([("class:scrollbar.button" if thumb else "class:scrollbar.background", " ")])
        return UIContent(get_line=lambda i: rows[i], line_count=len(rows), show_cursor=False)

    def mouse_handler(self, event):
        kind = event.event_type
        if kind in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            return self.viewport.mouse_handler(event)
        row = event.position.y
        if kind == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
            self._drag_offset = None
            if row == 0:
                self.viewport.scroll_by(-3)
            elif row == self.height - 1:
                self.viewport.scroll_by(3)
            elif self.thumb_top <= row - 1 < self.thumb_top + self.thumb_size:
                self._drag_offset = row - 1 - self.thumb_top
            else:
                self.viewport.page(-1 if row - 1 < self.thumb_top else 1)
        elif kind in (MouseEventType.MOUSE_MOVE, MouseEventType.MOUSE_UP):
            if kind == MouseEventType.MOUSE_MOVE and event.button != MouseButton.LEFT:
                # A release outside the one-column bar is delivered elsewhere.
                self._drag_offset = None
                return None
            if self._drag_offset is not None and self.travel:
                target = max(0, min(self.travel, row - 1 - self._drag_offset))
                self.viewport.scroll_to(round(target * self.viewport.max_top / self.travel))
            if kind == MouseEventType.MOUSE_UP or event.button != MouseButton.LEFT:
                self._drag_offset = None
        return None


def scrollable_pane(control, *, width=None, height=None):
    body = Window(control, wrap_lines=False, scroll_offsets=ScrollOffsets(),
                  get_vertical_scroll=lambda _: control.top, always_hide_cursor=True)
    scrollbar = Window(ViewportScrollbar(control), width=1, always_hide_cursor=True)
    return VSplit([body, scrollbar], width=width, height=height)
