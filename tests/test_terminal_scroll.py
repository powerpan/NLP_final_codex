"""Exercise rendered viewports and terminal input without model or network calls."""
import asyncio
import importlib.util
import unittest
from unittest.mock import patch

from maplatform.domain import Policy
from maplatform.provider import ScriptedProvider
from maplatform.store import Store
from maplatform.terminal import TerminalController, run_tui
from maplatform.tools import MemorySandbox, ToolGateway


TUI = all(importlib.util.find_spec(name) for name in ("prompt_toolkit", "markdown_it"))
LONG_REPLY = "planner\n" + "".join(f"第{i:03d}段中文回覆內容。" for i in range(240)) + "END_OF_REPLY"


@unittest.skipUnless(TUI, "Optional terminal dependencies are not installed")
class ScrollIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from prompt_toolkit import Application
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from prompt_toolkit.data_structures import Size

        class Output(DummyOutput):
            columns = 120
            rows = 30
            mouse_enabled = False

            def enable_mouse_support(self):
                self.mouse_enabled = True

            def disable_mouse_support(self):
                self.mouse_enabled = False

            def get_size(self):
                return Size(rows=self.rows, columns=self.columns)

        self.output = Output()
        self.store = Store(":memory:")
        self.controller = TerminalController(self.store, ScriptedProvider([]),
            ToolGateway(self.store, MemorySandbox(), Policy()), demo=True)
        self.controller.panel = LONG_REPLY
        self.pipe_context = create_pipe_input()
        self.pipe = self.pipe_context.__enter__()
        self.app = None

        def create_app(*args, **kwargs):
            self.app = Application(*args, **kwargs)
            return self.app

        self.app_patch = patch("prompt_toolkit.Application", side_effect=create_app)
        self.app_patch.start()
        self.ui = asyncio.create_task(run_tui(self.controller, input=self.pipe, output=self.output))
        await self.until(lambda: self.app and self.app.is_running and self.pane() and self.pane().render_info)
        await self.until(lambda: "第" in self.screen_text())

    async def asyncTearDown(self):
        if not self.ui.done():
            self.pipe.send_text("\x04")
            await asyncio.wait_for(self.ui, 2)
        self.app_patch.stop()
        self.pipe_context.__exit__(None, None, None)
        await self.controller.close()
        self.store.close()

    async def until(self, predicate):
        for _ in range(150):
            if predicate():
                return
            await asyncio.sleep(.01)
        self.fail("TUI did not reach the expected render state")

    def pane(self):
        for window in self.app.layout.find_all_windows():
            control = window.content
            text = control.buffer.text if hasattr(control, "buffer") else getattr(control, "text", "")
            if isinstance(text, str) and "第000段" in text:
                return window

    def screen_text(self):
        """Read actual painted cells, not the unwrapped text behind the viewport."""
        window = self.pane()
        screen = self.app.renderer._last_screen
        if not window or not screen or window not in screen.visible_windows_to_write_positions:
            return ""
        pos = screen.visible_windows_to_write_positions[window]
        return "\n".join("".join(screen.data_buffer[y][x].char
            for x in range(pos.xpos, pos.xpos + pos.width))
            for y in range(pos.ypos, pos.ypos + pos.height))

    async def send(self, keys):
        counter = self.app.render_counter
        self.pipe.send_text(keys)
        await self.until(lambda: self.app.render_counter > counter)

    async def test_page_up_reaches_start_inside_one_long_paragraph(self):
        await self.send("\x1b[5~" * 30)
        self.assertIn("第000段", self.screen_text())
        self.assertNotIn("END_OF_REPLY", self.screen_text())

    async def test_initial_bottom_shows_complete_last_paragraph(self):
        self.assertIn("END_OF_REPLY", self.screen_text())

    async def test_wheel_reaches_end_after_scrolling_to_first_source_line(self):
        await self.send("\x1b[5~" * 30)
        # SGR wheel events over the conversation (the composer remains focused).
        await self.send("\x1b[<65;2;5M" * 100)
        self.assertIn("END_OF_REPLY", self.screen_text())

    async def test_each_page_is_reachable_without_skipping_wrapped_content(self):
        await self.send("\x1b[1;5H")  # Ctrl+Home
        seen = set()
        for _ in range(20):
            text = self.screen_text().replace("\n", "").replace(" ", "")
            seen.update(i for i in range(240) if f"第{i:03d}段" in text)
            if "END_OF_REPLY" in text:
                break
            await self.send("\x1b[6~")
        self.assertEqual(seen, set(range(240)))

    async def test_manual_scroll_survives_stream_and_resize_then_end_resumes_follow(self):
        control = self.pane().content
        composer = self.app.layout.current_control
        await self.send("尚未送出的問題")
        await self.send("\x1b[5~")
        self.assertFalse(control.follow)
        anchor_text = "".join(t for _, t in control.lines[control.top])[:15]
        top = control.top
        draft = [("class:md.bold", LONG_REPLY + " 新內容" * 160)]
        # Drive the same refresh path used by streamed drafts, with no API call.
        self.controller.transcript_fragments = lambda: tuple(draft)
        await self.until(lambda: control.text.endswith("新內容"))
        await self.send("\x1b[C")  # Render the update while keeping input intact.
        self.assertEqual(control.top, top)
        self.assertIn(anchor_text, self.screen_text().replace("\n", "").replace(" ", ""))
        for columns, rows in ((78, 24), (120, 30)):
            self.output.columns, self.output.rows = columns, rows
            counter = self.app.render_counter
            self.app.invalidate()
            await self.until(lambda: self.app.render_counter > counter)
            self.assertFalse(control.follow)
            self.assertIn(anchor_text, self.screen_text().replace("\n", "").replace(" ", ""))
        self.assertIs(self.app.layout.current_control, composer)
        self.assertEqual(composer.buffer.text, "尚未送出的問題")
        await self.send("\x1b[1;5F")  # Ctrl+End
        self.assertTrue(control.follow)
        draft.append(("", "\nSTREAM_FINISHED"))
        await self.until(lambda: "STREAM_FINISHED" in self.screen_text())

    async def test_scrollbar_drag_moves_to_both_ends(self):
        from prompt_toolkit.data_structures import Point
        from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
        from maplatform.terminal_viewport import ViewportScrollbar

        control = self.pane().content
        bar = next(w for w in self.app.layout.find_all_windows()
                   if isinstance(w.content, ViewportScrollbar) and w.content.viewport is control)
        pos = self.app.renderer._last_screen.visible_windows_to_write_positions[bar]

        async def drag_to(row):
            thumb_row = bar.content.thumb_top + 1
            x, y = pos.xpos, pos.ypos + thumb_row
            handler = self.app.renderer.mouse_handlers.mouse_handlers[y][x]
            for kind, target in ((MouseEventType.MOUSE_DOWN, y),
                                 (MouseEventType.MOUSE_MOVE, pos.ypos + row),
                                 (MouseEventType.MOUSE_UP, pos.ypos + row)):
                handler(MouseEvent(Point(x=x, y=target), kind, MouseButton.LEFT, frozenset()))
            counter = self.app.render_counter
            self.app.invalidate()
            await self.until(lambda: self.app.render_counter > counter)

        await drag_to(1)
        self.assertIn("第000段", self.screen_text())
        self.assertFalse(control.follow)
        await drag_to(pos.height - 2)
        self.assertIn("END_OF_REPLY", self.screen_text())
        self.assertTrue(control.follow)

    async def test_copy_mode_releases_mouse_and_freezes_stream_until_exit(self):
        control = self.pane().content
        composer = self.app.layout.current_control
        await self.send("\x1b[5~")
        top = control.top
        await self.send("/copy\r")
        await self.send("保留輸入草稿")
        self.assertFalse(self.app.mouse_support())
        self.assertFalse(self.output.mouse_enabled)
        screen_before = self.screen_text()
        counter = self.app.render_counter
        self.controller.transcript_fragments = lambda: (("", LONG_REPLY + "\nNEW_STREAM_TEXT"),)
        self.controller.status_text = lambda: "UPDATED_STATUS"
        self.controller.on_change()
        await asyncio.sleep(.25)  # Two refresh intervals, including pending invalidations.
        self.assertEqual(control.text, LONG_REPLY)
        self.assertEqual(self.screen_text(), screen_before)
        self.assertEqual(self.app.render_counter, counter)
        # Escape exits without submitting or discarding the input draft.
        self.pipe.send_text("\x1b")
        await self.until(lambda: self.output.mouse_enabled)
        self.assertTrue(self.app.mouse_support())
        self.assertTrue(self.output.mouse_enabled)
        await self.until(lambda: control.text.endswith("NEW_STREAM_TEXT"))
        self.assertEqual(control.top, top)
        self.assertIs(self.app.layout.current_control, composer)
        self.assertEqual(composer.buffer.text, "保留輸入草稿")

    async def test_copy_command_toggles_without_starting_a_model_task(self):
        await self.send("/copy\r")
        self.assertFalse(self.app.mouse_support())
        self.assertEqual(self.controller.workflows(), [])
        await self.send("/copy\r")
        self.assertTrue(self.app.mouse_support())
        self.assertEqual(self.controller.workflows(), [])


if __name__ == "__main__":
    unittest.main()
