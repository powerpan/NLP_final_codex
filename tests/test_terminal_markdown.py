"""Presentation-only checks; no keys, models, network or source mutation."""
import asyncio
import copy
import importlib.util
import json
import unittest
from unittest.mock import patch

from maplatform.domain import Policy, dumps
from maplatform.provider import ScriptedProvider
from maplatform.store import Store
from maplatform.terminal import TerminalController, run_tui
from maplatform.terminal_markdown import clip_fragments, render_markdown
from maplatform.tools import MemorySandbox, ToolGateway


MARKDOWN = importlib.util.find_spec("markdown_it") is not None
TUI = MARKDOWN and importlib.util.find_spec("prompt_toolkit") is not None


def visible(fragments):
    return "".join(text for _, text in fragments)


@unittest.skipUnless(MARKDOWN, "Optional Markdown dependency is not installed")
class MarkdownTests(unittest.TestCase):
    def test_screenshot_ordered_list_bold_and_code(self):
        source = "1. **planner（我） ** — 規劃\n2. **researcher** — `read_web_result`\n3. **coder**\n4. **reviewer**"
        fragments = render_markdown(source)
        text = visible(fragments)
        self.assertNotIn("**", text)
        self.assertNotIn("`", text)
        self.assertIn("1. planner（我）", text)
        for role in ("planner", "researcher", "coder", "reviewer"):
            self.assertTrue(any("md.bold" in style and role in text for style, text in fragments))
        self.assertTrue(any("md.code" in style and text == "read_web_result" for style, text in fragments))

    def test_code_escaped_delimiters_paths_and_math_remain_literal(self):
        source = '\\*\\*literal\\*\\* and file_name.py; 2 ** 3\n\n`**inline **`\n\n```python\nprint("**code **")\nx = 2 ** 3\n```'
        text = visible(render_markdown(source))
        for literal in ("**literal**", "file_name.py", "2 ** 3", "**inline **", 'print("**code **")'):
            self.assertIn(literal, text)
        self.assertNotIn("```", text)
        self.assertFalse(any("md.bold" in style for style, _ in render_markdown(source)))

    def test_nested_emphasis_quotes_lists_and_table_are_readable(self):
        source = "## 標題\n\n> ***重要***\n\n- 項目\n  - 子項目\n\n| Agent | 工作 |\n|---|---|\n| **coder** | `測試` |\n\n~~過期~~"
        fragments = render_markdown(source)
        text = visible(fragments)
        for expected in ("標題", "│ 重要", "• 項目", "  • 子項目", "Agent │ 工作", "coder │ 測試", "過期"):
            self.assertIn(expected, text)
        self.assertTrue(any("md.bold" in s and "md.italic" in s and t == "重要" for s, t in fragments))
        self.assertTrue(any("md.strike" in s and t == "過期" for s, t in fragments))

    def test_relaxed_bold_never_consumes_delimiters_inside_code_or_links(self):
        for source in ('**prefix `a ** b` suffix **', '**prefix ``a ** b`` suffix **'):
            fragments = render_markdown(source)
            self.assertTrue(any("md.code" in style and text == "a ** b" for style, text in fragments))
        fragments = render_markdown('**prefix [source ** ](https://example.org) suffix **')
        self.assertIn("https://example.org", visible(fragments))
        self.assertIn("source **", visible(fragments))

    def test_links_images_and_html_do_not_execute_or_hide_destinations(self):
        source = '[來源](https://example.org/a?q=1&b=2)\n\n![描述](https://example.org/image.png)\n\n<script>alert(1)</script>'
        text = visible(render_markdown(source))
        self.assertIn("來源 (https://example.org/a?q=1&b=2)", text)
        self.assertIn("[圖片：描述] (https://example.org/image.png)", text)
        self.assertIn("<script>alert(1)</script>", text)

    def test_controls_removed_before_and_after_entity_decoding(self):
        text = visible(render_markdown('**safe**\x1b[31m red\x1b[0m\x1b]52;c;secret\x07 &#x202e;evil &#x1b;[2J'))
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\u202e", text)
        self.assertNotIn("secret", text)
        self.assertIn("safe", text)

    def test_every_stream_prefix_is_renderable_without_dropping_words(self):
        source = "**完整粗體** 與 `程式碼`\n\n```python\nprint('🙂')\n```"
        for index in range(len(source) + 1):
            self.assertIsInstance(render_markdown(source[:index]), tuple)
        self.assertEqual(visible(render_markdown("**尚未閉合")), "**尚未閉合")
        self.assertIn("完整粗體", visible(render_markdown(source)))
        self.assertIn("print('🙂')", visible(render_markdown(source)))

    def test_clipping_keeps_text_and_styles_aligned(self):
        fragments = (("class:md.bold", "前文\n"), ("class:md.code", "代碼🙂\n"), ("", "尾文"))
        for limit in range(1, len(visible(fragments)) + 1):
            clipped = clip_fragments(fragments, limit)
            self.assertEqual(visible(clipped), visible(fragments)[-limit:])
            for style, text in clipped:
                self.assertTrue(any(style == original_style and original_text.endswith(text)
                                    for original_style, original_text in fragments))


@unittest.skipUnless(TUI, "Optional terminal dependencies are not installed")
class MarkdownControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.gateway = ToolGateway(self.store, MemorySandbox(), Policy())
        answer = {"role": "assistant", "content": dumps({"summary": "**researcher** 與 `工具`", "artifact_ids": []})}
        self.controller = TerminalController(self.store, ScriptedProvider([answer]), self.gateway, demo=True)

    async def asyncTearDown(self):
        await self.controller.close()
        self.store.close()

    async def complete(self):
        await self.controller.submit("保留 **原始輸入** 和 `字面指令`")
        await asyncio.wait_for(self.controller.runner, 2)

    async def test_render_history_focus_and_panels_without_changing_source(self):
        await self.complete()
        original = copy.deepcopy(self.controller.sessions.history(self.controller.session["id"]))
        fragments = self.controller.transcript_fragments()
        self.assertIn("**原始輸入**", visible(fragments))
        self.assertNotIn("**researcher**", visible(fragments))
        self.assertIn("**researcher**", self.controller.transcript())
        self.controller.focus = self.controller.tasks()[0]["id"]
        self.assertTrue(any("md.bold" in s and t == "researcher" for s, t in self.controller.transcript_fragments()))
        self.controller.panel = '{"command":"echo **keep**"}'
        self.assertEqual(visible(self.controller.transcript_fragments()), self.controller.panel)
        self.assertEqual(original, self.controller.sessions.history(self.controller.session["id"]))

    async def test_actual_tui_viewport_renders_styled_answer(self):
        from prompt_toolkit import Application
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        await self.complete()
        apps = []
        def create_app(*args, **kwargs):
            app = Application(*args, **kwargs)
            apps.append(app)
            return app
        async def until(predicate):
            for _ in range(100):
                if predicate():
                    return
                await asyncio.sleep(.02)
            self.fail("TUI did not refresh")
        with create_pipe_input() as pipe, patch("prompt_toolkit.Application", side_effect=create_app):
            ui = asyncio.create_task(run_tui(self.controller, input=pipe, output=DummyOutput()))
            try:
                await until(lambda: apps and apps[0].is_running)
                def transcript_control():
                    return next((c for c in apps[0].layout.find_all_controls()
                                 if hasattr(c, "fragments") and "researcher 與 工具" in c.text), None)
                await until(lambda: transcript_control() is not None)
                control = transcript_control()
                self.assertNotIn("**researcher**", control.text)
                styles = control.fragments
                self.assertTrue(any("md.bold" in s and t == "researcher" for s, t in styles))
                self.assertTrue(any("md.code" in s and t == "工具" for s, t in styles))
                pipe.send_text("\x1b[5~\x1b[6~\x04")  # PageUp/PageDown/Ctrl+D
                await asyncio.wait_for(ui, 2)
            finally:
                if not ui.done():
                    ui.cancel()
                    await asyncio.gather(ui, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
