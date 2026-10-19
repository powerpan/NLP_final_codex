"""Clipboard tests are mocked: never read or overwrite the user's clipboard."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from maplatform.domain import PlatformError
from maplatform.terminal_clipboard import copy_text


class ClipboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.process = Mock(returncode=0, communicate=AsyncMock(return_value=(None, None)),
                            wait=AsyncMock(return_value=0))
        self.start = AsyncMock(return_value=self.process)
        self.start_patch = patch("maplatform.terminal_clipboard.asyncio.create_subprocess_exec", self.start)
        self.start_patch.start()
        self.addCleanup(self.start_patch.stop)
        self.platform_patch = patch("maplatform.terminal_clipboard.sys.platform", "darwin")
        self.platform_patch.start()
        self.addCleanup(self.platform_patch.stop)

    async def test_macos_preserves_unicode_newlines_and_shell_characters(self):
        text = "第一行 **bold**\n第二行：$(do-not-run) `literal` 🧑‍💻"
        with patch.dict("os.environ", {"LC_ALL": "C"}):
            await copy_text(text)
        self.assertEqual(self.start.call_args.args, ("/usr/bin/pbcopy",))
        kwargs = self.start.call_args.kwargs
        self.assertEqual(kwargs["stdin"], asyncio.subprocess.PIPE)
        self.assertEqual(kwargs["stdout"], asyncio.subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], asyncio.subprocess.DEVNULL)
        self.assertEqual(kwargs["env"]["LC_ALL"], "en_US.UTF-8")
        self.assertNotIn("shell", kwargs)
        self.process.communicate.assert_awaited_once_with(text.encode("utf-8"))
        self.process.kill.assert_not_called()

    async def test_empty_selection_does_not_clear_clipboard(self):
        with self.assertRaises(PlatformError) as error:
            await copy_text("")
        self.assertEqual(error.exception.code, "CLIPBOARD_EMPTY")
        self.start.assert_not_awaited()

    async def test_launch_error_is_safe_and_actionable(self):
        self.start.side_effect = OSError("sensitive subprocess diagnostics")
        with self.assertRaises(PlatformError) as error:
            await copy_text("private selected text")
        self.assertEqual(error.exception.code, "CLIPBOARD_UNAVAILABLE")
        self.assertNotIn("sensitive", str(error.exception))
        self.assertNotIn("private", str(error.exception))

    async def test_nonzero_exit_does_not_report_success(self):
        self.process.returncode = 1
        with self.assertRaises(PlatformError) as error:
            await copy_text("private selected text")
        self.assertEqual(error.exception.code, "CLIPBOARD_FAILED")

    async def test_timeout_kills_and_reaps_subprocess(self):
        self.process.returncode = None
        self.process.communicate.side_effect = asyncio.TimeoutError
        with self.assertRaises(PlatformError) as error:
            await copy_text("private selected text")
        self.assertEqual(error.exception.code, "CLIPBOARD_TIMEOUT")
        self.process.kill.assert_called_once()
        self.process.wait.assert_awaited_once()

    async def test_cancellation_kills_and_reaps_subprocess(self):
        self.process.returncode = None
        self.process.communicate.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await copy_text("private selected text")
        self.process.kill.assert_called_once()
        self.process.wait.assert_awaited_once()

    async def test_write_error_kills_and_reaps_subprocess(self):
        self.process.returncode = None
        self.process.communicate.side_effect = BrokenPipeError("private detail")
        with self.assertRaises(PlatformError) as error:
            await copy_text("private selected text")
        self.assertEqual(error.exception.code, "CLIPBOARD_FAILED")
        self.assertNotIn("private", str(error.exception))
        self.process.kill.assert_called_once()
        self.process.wait.assert_awaited_once()

    async def test_unavailable_display_does_not_start_subprocess(self):
        with patch("maplatform.terminal_clipboard.sys.platform", "linux"), patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(PlatformError) as error:
                await copy_text("private selected text")
        self.assertEqual(error.exception.code, "CLIPBOARD_UNAVAILABLE")
        self.start.assert_not_awaited()

    async def test_linux_and_windows_local_tools(self):
        for platform, environment, available, command, encoding in (
            ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, "wl-copy",
             ("/usr/bin/wl-copy", "--type", "text/plain;charset=utf-8"), "utf-8"),
            ("linux", {"DISPLAY": ":0"}, "xclip",
             ("/usr/bin/xclip", "-selection", "clipboard"), "utf-8"),
            ("linux", {"DISPLAY": ":0"}, "xsel",
             ("/usr/bin/xsel", "--clipboard", "--input"), "utf-8"),
            ("win32", {}, "clip", ("/usr/bin/clip",), "utf-16-le"),
        ):
            with self.subTest(platform=platform, tool=available):
                lookup = lambda name: f"/usr/bin/{name}" if name == available else None
                with patch("maplatform.terminal_clipboard.sys.platform", platform), \
                        patch.dict("os.environ", environment, clear=True), \
                        patch("maplatform.terminal_clipboard.shutil.which", side_effect=lookup):
                    await copy_text("繁體中文\n多行")
                self.assertEqual(self.start.call_args.args, command)
                self.assertEqual(self.process.communicate.call_args.args, ("繁體中文\n多行".encode(encoding),))


if __name__ == "__main__":
    unittest.main()
