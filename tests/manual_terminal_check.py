"""Real PTY walkthrough, entirely offline. Run with .venv/bin/python.

Uses a temporary database and demo adapters; no keys, network, Docker or Git IO.
This complements pipe-input unit tests by exercising CLI startup and resize.
"""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time


def main():
    evidence = {}
    with tempfile.TemporaryDirectory() as folder:
        db = str(Path(folder) / "terminal.sqlite")
        master, slave = pty.openpty()
        raw = bytearray()

        def size(columns, rows):
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

        def drain(seconds=.05):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                if select.select([master], [], [], .01)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    raw.extend(chunk)
                    if b"\x1b[6n" in chunk:
                        os.write(master, b"\x1b[1;1R")

        def query(sql):
            if not Path(db).exists():
                return []
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                try:
                    return [dict(r) for r in conn.execute(sql)]
                except sqlite3.OperationalError:
                    return []

        def wait(predicate, timeout=8):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                drain()
                if predicate():
                    return
                if process.poll() is not None:
                    raise AssertionError("Terminal exited unexpectedly: " + raw[-2000:].decode(errors="replace"))
            raise AssertionError("PTY condition timed out: " + raw[-2000:].decode(errors="replace"))

        def send(text):
            os.write(master, text.encode())

        size(120, 36)
        env = {**os.environ, "TERM": "xterm-256color", "PROMPT_TOOLKIT_NO_CPR": "1"}
        process = subprocess.Popen([sys.executable, "-m", "maplatform", "shell", "--demo", "--db", db],
                                   stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        try:
            wait(lambda: b"MULTI AGENT" in raw)
            send("協作\r")
            wait(lambda: query("SELECT id FROM approvals WHERE tool_name='ask_human' AND status='OPEN'"))
            size(78, 24)
            process.send_signal(signal.SIGWINCH)
            send("用於測試父子任務\r")
            wait(lambda: query("SELECT id FROM approvals WHERE tool_name='git_push' AND status='OPEN'"))
            approval = query("SELECT id FROM approvals WHERE tool_name='git_push' AND status='OPEN'")[0]["id"]
            send("/approve " + approval + "\r")
            wait(lambda: query("SELECT id FROM workflows WHERE status='COMPLETED'"))
            evidence["nested_collaboration"] = query("SELECT owner,kind,depth,status FROM tasks ORDER BY created,id")
            assert len(evidence["nested_collaboration"]) == 5
            assert all(r["status"] == "COMPLETED" for r in evidence["nested_collaboration"])
            assert max(r["depth"] for r in evidence["nested_collaboration"]) == 2
            evidence["approval"] = query("SELECT tool_name,status FROM approvals ORDER BY created")
            size(120, 36)
            process.send_signal(signal.SIGWINCH)
            task_id = query("SELECT id FROM tasks WHERE owner='researcher' AND kind='WORK' LIMIT 1")[0]["id"]
            send("/focus " + task_id + "\r")
            drain(.2)
            send("/focus all\r")
            drain(.2)
            checkpoint = len(raw)
            send("/copy\r")
            drain(.3)
            assert b"\x1b[?1000l" in raw[checkpoint:], "Copy mode must release terminal mouse reporting"
            checkpoint = len(raw)
            send("/copy\r")
            drain(.3)
            assert b"\x1b[?1000h" in raw[checkpoint:], "Leaving copy mode must restore scrolling"
            evidence["copy_mode_mouse_reporting_toggled"] = True
            send("接續上輪內容\r")
            wait(lambda: len(query("SELECT id FROM workflows WHERE status='COMPLETED'")) == 2)
            # Workflow completion precedes session-history materialization.
            wait(lambda: bool(query("SELECT assistant_text FROM session_turns ORDER BY ordinal DESC LIMIT 1")[0]["assistant_text"]))
            second = query("SELECT assistant_text FROM session_turns ORDER BY ordinal DESC LIMIT 1")[0]["assistant_text"]
            assert "3 則歷史訊息" in second
            send("/exit\r")
            wait(lambda: process.poll() is not None)
            evidence["exit_code"] = process.wait(timeout=2)
            assert evidence["exit_code"] == 0
            # Reopen same SQLite through a real terminal; no model calls on open.
            before = query("SELECT COUNT(*) n FROM llm_calls")[0]["n"]
            os.close(master)
            master, slave = pty.openpty()
            size(120, 36)
            process = subprocess.Popen([sys.executable, "-m", "maplatform", "shell", "--demo", "--db", db],
                                       stdin=slave, stdout=slave, stderr=slave, env=env)
            os.close(slave)
            checkpoint = len(raw)
            wait(lambda: b"MULTI AGENT" in raw[checkpoint:])
            drain(.3)
            assert query("SELECT COUNT(*) n FROM llm_calls")[0]["n"] == before
            send("\x04")  # Ctrl+D exits in raw TUI input.
            wait(lambda: process.poll() is not None)
            assert process.wait(timeout=2) == 0
            evidence.update(resize_widths=[120, 78, 120], history_restored=True, opening_did_not_call_model=True,
                            screen_tree_seen="任務樹".encode() in raw,
                            no_traceback=b"Traceback" not in raw,
                            scope="Offline PTY demo only; no live model/web requests or actual Git push")
            assert evidence["screen_tree_seen"] and evidence["no_traceback"]
            print(json.dumps(evidence, ensure_ascii=False, indent=2))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            os.close(master)


if __name__ == "__main__":
    main()
