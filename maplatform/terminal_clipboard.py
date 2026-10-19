"""Copy explicitly selected terminal text through a local clipboard program."""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys

from .domain import PlatformError


_COPY_TIMEOUT = 3.0
_REAP_TIMEOUT = 1.0


def _command():
    if sys.platform == "darwin":
        return ["/usr/bin/pbcopy"], "utf-8"
    if sys.platform == "win32":
        if executable := shutil.which("clip"):
            return [executable], "utf-16-le"
    elif sys.platform.startswith("linux"):
        candidates = []
        if os.environ.get("WAYLAND_DISPLAY"):
            candidates.append(("wl-copy", "--type", "text/plain;charset=utf-8"))
        if os.environ.get("DISPLAY"):
            candidates.extend((("xclip", "-selection", "clipboard"),
                               ("xsel", "--clipboard", "--input")))
        for name, *arguments in candidates:
            if executable := shutil.which(name):
                return [executable, *arguments], "utf-8"
    raise PlatformError("CLIPBOARD_UNAVAILABLE", "找不到可用的本機剪貼簿工具，請確認終端所在電腦支援剪貼簿。")


async def _stop(process):
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
        await asyncio.wait_for(process.wait(), _REAP_TIMEOUT)


async def copy_text(text: str) -> None:
    """Copy text only after an explicit UI action; never expose it in errors.

    Uses the clipboard on the machine running the CLI. No shell, temporary
    files, OSC sequences, or clipboard access occurs merely by selecting text.
    """
    if not text:
        raise PlatformError("CLIPBOARD_EMPTY", "請先在模型回答區域拖曳選取文字。")
    command, encoding = _command()
    try:
        payload = text.encode(encoding)
    except UnicodeError:
        raise PlatformError("CLIPBOARD_FAILED", "選取文字包含無法複製的字元，請重新選取。") from None
    environment = os.environ.copy()
    if sys.platform == "darwin":
        # pbcopy interprets stdin using the locale, including inherited LC_ALL.
        environment["LC_ALL"] = "en_US.UTF-8"
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            env=environment,
        )
    except OSError:
        raise PlatformError("CLIPBOARD_UNAVAILABLE", "無法啟動本機剪貼簿工具，請確認已安裝且可使用。") from None
    try:
        await asyncio.wait_for(process.communicate(payload), _COPY_TIMEOUT)
    except asyncio.TimeoutError:
        await _stop(process)
        raise PlatformError("CLIPBOARD_TIMEOUT", "複製逾時，請稍後重試。") from None
    except asyncio.CancelledError:
        await _stop(process)
        raise
    except OSError:
        await _stop(process)
        raise PlatformError("CLIPBOARD_FAILED", "複製失敗，請重新選取後重試。") from None
    if process.returncode:
        raise PlatformError("CLIPBOARD_FAILED", "剪貼簿工具未能完成複製，請確認本機剪貼簿可使用。")
