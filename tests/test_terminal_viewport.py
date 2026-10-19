"""Display-row layout and reading-position regressions; no model calls."""
import importlib.util
import unittest


TUI = importlib.util.find_spec("prompt_toolkit") is not None
if TUI:
    from prompt_toolkit.utils import get_cwidth
    from maplatform.terminal_viewport import ScrollableText


def text_of(fragments):
    return "".join(text for _, text in fragments)


@unittest.skipUnless(TUI, "Optional terminal dependency is not installed")
class ViewportTests(unittest.TestCase):
    def make_view(self, text, width=17, height=5):
        view = ScrollableText()
        view.set_fragments((("", text),))
        view.create_content(width, height)
        return view

    def test_odd_width_chinese_combining_and_styles_preserve_content(self):
        fragments = (("class:md.bold", "中文"), ("class:md.code", "🙂e\u0301中文abc"))
        view = ScrollableText()
        view.set_fragments(fragments)
        content = view.create_content(5, 3)
        rows = [content.get_line(i) for i in range(content.line_count)]
        self.assertEqual("".join(map(text_of, rows)), text_of(fragments))
        for row in rows:
            self.assertLessEqual(get_cwidth(text_of(row)), 5)
            self.assertFalse(text_of(row).startswith("\u0301"))
        self.assertTrue(any(style == "class:md.bold" and "中文" in text
                            for row in rows for style, text in row))
        self.assertTrue(any(style == "class:md.code" and "e\u0301" in text
                            for row in rows for style, text in row))

    def test_tabs_use_four_cell_stops_including_wide_characters(self):
        view = self.make_view("中\tx\nab\t中e\u0301", width=5)
        self.assertEqual(list(map(text_of, view.lines)), ["中  x", "ab  ", "中e\u0301"])
        self.assertNotIn("\t", "".join(map(text_of, view.lines)))
        self.assertEqual(view.text, "中\tx\nab\t中e\u0301")

    def test_blank_lines_and_trailing_newline_are_retained(self):
        view = self.make_view("alpha\n\nbeta\n", width=20)
        self.assertEqual(list(map(text_of, view.lines)), ["alpha", "", "beta", ""])
        self.assertEqual(view.offsets, [0, 6, 7, 12])

    def test_tiny_and_empty_panes_have_valid_content(self):
        view = self.make_view("", width=0, height=0)
        content = view.create_content(0, 0)
        self.assertGreaterEqual(content.line_count, 1)
        self.assertEqual(content.get_line(0), [])
        view.set_fragments((("", "中文🙂"),))
        content = view.create_content(1, 2)
        for i in range(content.line_count):
            self.assertLessEqual(get_cwidth(text_of(content.get_line(i))), 1)
        self.assertEqual(view.text, "中文🙂")

    def test_single_long_paragraph_scrolls_by_display_rows(self):
        text = "".join(f"第{i:03d}段中文回覆內容。" for i in range(100))
        view = self.make_view(text)
        self.assertGreater(len(view.lines), 50)
        bottom = view.top
        view.scroll_by(-3)
        view.create_content(17, 5)
        self.assertEqual(view.top, bottom - 3)
        self.assertFalse(view.follow)
        view.to_start()
        view.create_content(17, 5)
        self.assertEqual(view.top, 0)
        self.assertIn("第000段", text_of(view.lines[view.top]))
        view.page(1)
        self.assertGreater(view.top, 0)
        self.assertLessEqual(view.top, view.height)
        for _ in range(len(view.lines)):
            view.page(1)
        self.assertEqual(view.top, bottom)
        self.assertTrue(view.follow)
        view.scroll_by(-10000)
        self.assertEqual(view.top, 0)

    def test_append_preserves_manual_position_and_bottom_resumes_following(self):
        original = "".join(f"段落{i:03d}內容。" for i in range(100))
        view = self.make_view(original)
        view.scroll_by(-20)
        anchor = view.offsets[view.top]
        row = text_of(view.lines[view.top])
        updated = original + "這是新串流文字。" * 60
        view.set_fragments((("", updated),))
        view.create_content(17, 5)
        self.assertEqual(view.offsets[view.top], anchor)
        self.assertEqual(text_of(view.lines[view.top]), row)
        self.assertFalse(view.follow)
        view.to_end()
        previous_bottom = view.top
        view.set_fragments((("", updated + "最後新增內容。" * 60),))
        view.create_content(17, 5)
        self.assertTrue(view.follow)
        self.assertGreater(view.top, previous_bottom)
        self.assertEqual(view.top, len(view.lines) - view.height)

    def test_resize_roundtrip_keeps_same_source_anchor(self):
        original = "".join(f"段落{i:03d}內容。" for i in range(100))
        view = self.make_view(original, width=31)
        view.scroll_by(-20)
        original_top = view.top
        anchor = view.offsets[view.top]
        for width in (17, 49, 23, 31):
            view.create_content(width, 5)
            self.assertLessEqual(view.offsets[view.top], anchor)
            self.assertGreater(view.offsets[view.top + 1], anchor)
            self.assertFalse(view.follow)
        self.assertEqual(view.top, original_top)

    def test_style_only_update_repaints_without_moving_reading_position(self):
        original = "paragraph content " * 100
        view = self.make_view(original)
        view.scroll_by(-12)
        old_top = view.top
        view.set_fragments((("class:md.bold", original),))
        content = view.create_content(17, 5)
        self.assertEqual(view.top, old_top)
        self.assertFalse(view.follow)
        self.assertTrue(all(style == "class:md.bold" for style, _ in content.get_line(view.top)))

    def test_clipped_history_with_append_retains_existing_reading_position(self):
        original = "\n".join(f"line {i:03d}: unique content" for i in range(100))
        view = self.make_view(original, width=40)
        view.scroll_by(-20)
        row = text_of(view.lines[view.top])
        cut = original.index("line 030:")
        updated = original[cut:] + "\n" + "\n".join(f"new {i:03d}" for i in range(20))
        view.set_fragments((("", updated),))
        view.create_content(40, 5)
        self.assertEqual(text_of(view.lines[view.top]), row)
        self.assertFalse(view.follow)

    def test_explicit_reset_returns_replacement_content_to_bottom(self):
        view = self.make_view("old paragraph " * 100)
        view.to_start()
        view.set_fragments((("", "new paragraph " * 100),), reset=True)
        view.create_content(17, 5)
        self.assertTrue(view.follow)
        self.assertEqual(view.top, len(view.lines) - view.height)

    def test_combining_mark_at_right_edge_is_actually_painted(self):
        from prompt_toolkit import Application
        from prompt_toolkit.application.current import set_app
        from prompt_toolkit.input import DummyInput
        from prompt_toolkit.layout import Layout, Window
        from prompt_toolkit.layout.mouse_handlers import MouseHandlers
        from prompt_toolkit.layout.screen import Screen, WritePosition
        from prompt_toolkit.output import DummyOutput

        view = self.make_view("ab\u0301", width=2, height=2)
        window = Window(view, get_vertical_scroll=lambda _: view.top)
        app = Application(layout=Layout(window), input=DummyInput(), output=DummyOutput())
        screen = Screen()
        with set_app(app):
            window.write_to_screen(screen, MouseHandlers(), WritePosition(0, 0, 2, 2), "", True, None)
        painted = "".join(screen.data_buffer[y][x].char for y in range(2) for x in range(2))
        self.assertIn("b\u0301", painted)

    def test_releasing_drag_outside_bar_does_not_scroll_on_later_hover(self):
        from prompt_toolkit.data_structures import Point
        from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
        from maplatform.terminal_viewport import ViewportScrollbar

        view = self.make_view("\n".join(map(str, range(100))), height=10)
        view.to_start()
        bar = ViewportScrollbar(view)
        bar.create_content(1, 10)
        bar.mouse_handler(MouseEvent(Point(0, 1), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset()))
        # Release was delivered to a different control, so the bar only sees hover.
        bar.mouse_handler(MouseEvent(Point(0, 8), MouseEventType.MOUSE_MOVE, MouseButton.NONE, frozenset()))
        self.assertEqual(view.top, 0)


if __name__ == "__main__":
    unittest.main()
