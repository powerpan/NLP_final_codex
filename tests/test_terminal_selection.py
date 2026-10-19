"""Selection follows source text across painted rows, without a real clipboard."""
import importlib.util
import unittest


TUI = importlib.util.find_spec("prompt_toolkit") is not None
if TUI:
    from prompt_toolkit import Application
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.layout import Layout, ScrollOffsets, Window
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen, WritePosition
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType, MouseModifier
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.utils import get_cwidth

    from maplatform.terminal_viewport import ScrollableText


@unittest.skipUnless(TUI, "Optional terminal dependency is not installed")
class SelectionTests(unittest.TestCase):
    def make_view(self, text, *, width=12, height=8, selectable=True):
        view = ScrollableText(selectable=selectable)
        view.set_fragments((("class:md.bold", text),))
        view.create_content(width, height)
        return view

    def event(self, view, kind, x, y, *, button=None, shift=False):
        if button is None:
            button = MouseButton.LEFT
        view.mouse_handler(MouseEvent(Point(x, y), kind, button,
            frozenset((MouseModifier.SHIFT,)) if shift else frozenset()))

    def click(self, view, x, y, *, shift=False):
        self.event(view, MouseEventType.MOUSE_DOWN, x, y, shift=shift)
        self.event(view, MouseEventType.MOUSE_UP, x, y, shift=shift)

    def test_shift_click_across_pages_copies_contiguous_source_not_wraps(self):
        text = "".join(f"第{i:03d}段中文內容。" for i in range(90))
        view = self.make_view(text, width=11, height=4)
        view.to_start()
        self.click(view, 1, 0)
        start = view.selection_range[0]
        for _ in range(12):
            view.page(1)
            view.create_content(11, 4)
        row = view.top + 2
        end = view.positions[row][2][1]
        self.click(view, 2, row, shift=True)
        self.assertEqual(view.selection_text(), text[start:end])
        self.assertGreater(len(view.selection_text()), 100)
        self.assertNotIn("\n", view.selection_text())
        self.assertFalse(view.follow)

    def test_reverse_drag_has_same_source_range_and_highlight(self):
        view = self.make_view("中文\talpha\nbeta", width=8)
        self.event(view, MouseEventType.MOUSE_DOWN, 2, 2)
        self.event(view, MouseEventType.MOUSE_MOVE, 1, 0)
        self.event(view, MouseEventType.MOUSE_UP, 1, 0)
        self.assertEqual(view.selection_text(), "文\talpha\nbet")
        content = view.create_content(8, 8)
        highlighted = "".join(text for row in range(content.line_count)
            for style, text in content.get_line(row) if "class:selection" in style)
        self.assertEqual(highlighted, "文    alphabet")
        self.assertTrue(any("class:md.bold" in style and "class:selection" in style
            for row in range(content.line_count) for style, _ in content.get_line(row)))

    def test_selection_freezes_source_through_resize_and_applies_latest_update_on_clear(self):
        text = "甲乙丙丁中文內容。" * 40
        view = self.make_view(text, width=19, height=5)
        view.to_start()
        self.click(view, 2, 0)
        self.click(view, 3, 6, shift=True)
        selection = view.selection_range
        selected = view.selection_text()
        view.set_fragments((("", text + "第一段更新"),))
        view.set_fragments((("class:md.code", text + "最新更新"),))
        for width in (7, 23, 11, 19):
            view.create_content(width, 5)
            self.assertEqual(view.text, text)
            self.assertEqual(view.selection_range, selection)
            self.assertEqual(view.selection_text(), selected)
        view.clear_selection()
        view.create_content(19, 5)
        self.assertFalse(view.selection_active)
        self.assertEqual(view.selection_text(), "")
        self.assertEqual(view.text, text + "最新更新")
        self.assertEqual(view.fragments[0][0], "class:md.code")

    def test_reset_discards_old_selection_and_queued_source(self):
        view = self.make_view("old source " * 50)
        self.click(view, 0, view.top)
        view.set_fragments((("", "queued old reply"),))
        view.set_fragments((("", "new conversation " * 50),), reset=True)
        view.create_content(12, 8)
        self.assertFalse(view.selection_active)
        self.assertTrue(view.follow)
        self.assertEqual(view.top, view.max_top)
        view.clear_selection()
        self.assertEqual(view.text, "new conversation " * 50)

    def test_nonselectable_pane_never_exposes_text_after_mouse_events(self):
        view = self.make_view("AGENTS secret status\n" * 20, selectable=False)
        self.event(view, MouseEventType.MOUSE_DOWN, 0, 0)
        self.event(view, MouseEventType.MOUSE_MOVE, 5, 8)
        self.event(view, MouseEventType.MOUSE_UP, 5, 8)
        self.click(view, 7, 10, shift=True)
        self.assertFalse(view.selection_active)
        self.assertIsNone(view.selection_range)
        self.assertEqual(view.selection_text(), "")
        old_top = view.top
        self.event(view, MouseEventType.SCROLL_UP, 0, 0, button=MouseButton.NONE)
        self.assertEqual(view.top, old_top - 3)

    def test_hover_after_release_outside_does_not_extend_selection(self):
        view = self.make_view("abcdefghijk\nlmnopqrstuv")
        self.event(view, MouseEventType.MOUSE_DOWN, 1, 0)
        self.event(view, MouseEventType.MOUSE_MOVE, 3, 0)
        selected = view.selection_text()
        # The release was delivered to another control; this pane sees hover.
        self.event(view, MouseEventType.MOUSE_MOVE, 8, 1, button=MouseButton.NONE)
        self.event(view, MouseEventType.SCROLL_DOWN, 8, 1, button=MouseButton.NONE)
        self.event(view, MouseEventType.MOUSE_UP, 8, 1)
        self.assertEqual(view.selection_text(), selected)


@unittest.skipUnless(TUI, "Optional terminal dependency is not installed")
class WindowSelectionTests(unittest.TestCase):
    """Use screen coordinates so PTK's cell-to-character mapping is exercised."""

    def setUp(self):
        self.view = ScrollableText(selectable=True)
        self.window = Window(self.view, wrap_lines=False, scroll_offsets=ScrollOffsets(),
            get_vertical_scroll=lambda _: self.view.top, always_hide_cursor=True)
        self.app = Application(layout=Layout(self.window), input=DummyInput(), output=DummyOutput())
        self.xpos, self.ypos = 5, 3

    def draw(self, text=None, *, width=9, height=8):
        if text is not None:
            self.view.set_fragments((("class:md.code", text),))
        self.screen = Screen()
        self.handlers = MouseHandlers()
        with set_app(self.app):
            self.window.write_to_screen(self.screen, self.handlers,
                WritePosition(self.xpos, self.ypos, width, height), "", True, None)

    def mouse(self, kind, x, y, *, shift=False):
        x += self.xpos
        y += self.ypos
        with set_app(self.app):
            self.handlers.mouse_handlers[y][x](MouseEvent(Point(x, y), kind, MouseButton.LEFT,
                frozenset((MouseModifier.SHIFT,)) if shift else frozenset()))

    def click(self, x, y, *, shift=False):
        self.mouse(MouseEventType.MOUSE_DOWN, x, y, shift=shift)
        self.mouse(MouseEventType.MOUSE_UP, x, y, shift=shift)

    def source_cell(self, offset, *, right_half=False):
        for row, spans in enumerate(self.view.positions):
            for col, (start, end) in enumerate(spans):
                if start <= offset < end:
                    text = "".join(value for _, value in self.view.lines[row])
                    x = get_cwidth(text[:col])
                    if right_half and get_cwidth(text[col]) == 2:
                        x += 1
                    return x, row - self.view.top
        self.fail(f"source offset {offset} was not painted")

    def test_wide_second_cell_tabs_and_combining_copy_original_source(self):
        text = "左中文\te\u0301\n下🙂界"
        self.draw(text, width=7)
        start = self.source_cell(text.index("中"), right_half=True)
        end = self.source_cell(text.index("界"), right_half=True)
        self.mouse(MouseEventType.MOUSE_DOWN, *start)
        self.mouse(MouseEventType.MOUSE_MOVE, *end)
        self.mouse(MouseEventType.MOUSE_UP, *end)
        self.assertEqual(self.view.selection_text(), "中文\te\u0301\n下🙂界")
        self.assertEqual(self.view.selection_text().count("\n"), 1)

    def test_combining_base_selects_whole_grapheme(self):
        self.draw("e\u0301中", width=7)
        self.click(0, 0)
        self.assertEqual(self.view.selection_text(), "e\u0301")

    def test_click_before_redraw_selects_painted_snapshot_not_unpainted_update(self):
        original = "可見舊答案\n後段"
        updated = "背景新增答案完全不同"
        self.draw(original)
        point = self.source_cell(original.index("舊"))
        # Refresh can update the data between a render and the next mouse event.
        # This handler still belongs to the previously painted screen.
        self.view.set_fragments((("", updated),))
        self.click(*point)
        self.assertEqual(self.view.selection_text(), "舊")
        self.assertEqual(self.view.text, original)
        self.view.clear_selection()
        self.draw()
        self.assertEqual(self.view.text, updated)
        self.assertFalse(self.view.selection_active)

    def test_tab_expansion_split_over_wrapped_rows_selects_one_tab(self):
        self.draw("\t中", width=3)
        self.click(0, 1)  # Fourth painted space still belongs to the same tab.
        self.assertEqual(self.view.selection_text(), "\t")
        self.view.clear_selection()
        self.click(2, 0)
        self.assertEqual(self.view.selection_text(), "\t")

    def test_blank_row_click_stays_at_that_line_and_can_select_next_line(self):
        self.draw("alpha\n\nbeta", width=9)
        self.click(7, 1)  # Padding on a completely empty source line.
        self.assertEqual(self.view.selection_range, (6, 6))
        self.assertEqual(self.view.selection_text(), "")
        self.click(0, 2, shift=True)
        self.assertEqual(self.view.selection_text(), "\nb")

    def test_right_padding_and_trailing_blank_row_are_source_boundaries(self):
        self.draw("ab\ncd\n", width=9)
        self.click(8, 0)
        self.assertEqual(self.view.selection_range, (2, 2))
        self.click(8, 2, shift=True)
        self.assertEqual(self.view.selection_text(), "\ncd\n")

    def test_single_cell_replacement_copies_the_original_wide_glyph(self):
        self.draw("中🙂", width=1)
        self.mouse(MouseEventType.MOUSE_DOWN, 0, 0)
        self.mouse(MouseEventType.MOUSE_MOVE, 0, 1)
        self.mouse(MouseEventType.MOUSE_UP, 0, 1)
        self.assertEqual(self.view.selection_text(), "中🙂")


if __name__ == "__main__":
    unittest.main()
