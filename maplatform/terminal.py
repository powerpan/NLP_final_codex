"""Interactive presentation over the durable scheduler; no model-owned UI state.

The controller also runs without a TTY/dependencies for accessibility and tests.
Only accepted public results become session history. Live drafts are ephemeral.
"""
from __future__ import annotations

import asyncio
from collections import deque
import contextlib
import io
import json
import os
import re
import signal
import stat
import sys
import time

from .domain import AGENTS, TERMINAL, PlatformError, Policy
from .runtime import Runtime
from .sessions import SessionService
from .accounting import summarize_usage


def safe_text(value):
    """Never let remote text drive terminal escape sequences or bidi controls."""
    text = str(value)
    text = re.sub(r"\x1b\](?:(?!\x07|\x1b\\).)*(?:\x07|\x1b\\|$)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b.", "", text, flags=re.DOTALL)
    return "".join(c for c in text if (c in "\n\t" or ord(c) >= 32)
                   and not 0x7f <= ord(c) <= 0x9f and not 0x202a <= ord(c) <= 0x202e
                   and not 0x2066 <= ord(c) <= 0x2069 and c not in "\u061c\u200e\u200f" and not 0xd800 <= ord(c) <= 0xdfff)


def summary_draft(raw):
    """Decode only the public summary string, including incomplete JSON escapes.

    This is a display preview, never a completion validator or a tool parser.
    """
    match = re.search(r'"summary"\s*:\s*"', raw)
    if not match:
        return ""
    start, escaped, end = match.end(), False, len(raw)
    for i in range(start, len(raw)):
        c = raw[i]
        if escaped:
            escaped = False
        elif c == "\\":
            escaped = True
        elif c == '"':
            end = i
            break
    body = raw[start:end]
    # At most a surrogate pair escape can be incomplete at the chunk boundary.
    for trim in range(min(12, len(body)) + 1):
        try:
            value = json.loads('"' + (body[:-trim] if trim else body) + '"')
            return safe_text(value)
        except (ValueError, UnicodeError):
            continue
    return ""


def lifecycle_text(status):
    return "流程結束（COMPLETED）" if status == "COMPLETED" else status


def execution_summary_text(summary):
    """Render server counts without interpreting an agent's natural-language answer."""
    tasks, web = summary["tasks"], summary["web"]
    lines = [f"執行記錄 · {lifecycle_text(summary['lifecycle_status'])}",
             f"任務完成 {tasks['completed']}/{tasks['total']} · 失敗 {tasks['failed']} · 逾時 {tasks['timed_out']} · 取消 {tasks['cancelled']}",
             f"網頁請求成功 {web['succeeded']} · 失敗 {web['failed']} · 結果未知 {web['unknown']} · 執行中 {web['running']}"]
    if tasks["failed_children"]:
        lines.append("含失敗子任務／請核對交付。")
    if tasks["cancelled_children"]:
        lines.append("含取消子任務／請核對交付。")
    if summary["error_codes"]:
        lines.append("記錄的錯誤碼：" + ", ".join(summary["error_codes"]))
    lines.append("流程結束不等於需求驗證通過；網頁請求成功也不代表內容符合需求，請依交付與來源核對。")
    return "\n".join(lines)


HELP = """Enter 送出 · Alt+Enter 換行 · Tab 補全 · F2 切換 Agent 詳情
PageUp / PageDown 捲動 · Ctrl+C 暫停 · Ctrl+D 離開
/new [標題]       建立會話         /sessions          會話清單
/switch ID        切換會話         /agent NAME        選擇下一輪主 Agent
/tree             父子任務樹       /focus ID|all       查看 Agent 的工作
/pending          問題與審批       /answer ID 文字     回答指定問題
/approve ID       批准指定操作     /deny ID            拒絕指定操作
/resume           恢復當前會話     /cancel [TASK_ID]   取消任務及其子任務
/status           預算與狀態       /clear              清除提示 / 返回對話
/help             指令說明         /exit               保存會話並離開
只有一個待答問題時，可直接輸入答案。審批必須使用 /approve 或 /deny。
執行中可回答問題、審批、查看狀態；下一輪請等本輪完成。
"""


class TerminalController:
    def __init__(self, store, provider, gateway, *, policy=None, session_id=None,
                 new=False, owner="planner", demo=False, parallelism=4):
        self.store, self.provider, self.gateway = store, provider, gateway
        self.policy, self.demo = policy or Policy(), demo
        self.parallelism = parallelism
        self.sessions = SessionService(store)
        candidates = self.sessions.list()
        if session_id:
            self.session = self.sessions.get(self.resolve(session_id, candidates)["id"])
        elif candidates and not new:
            self.session = candidates[0]
        else:
            self.session = self.sessions.create(owner=owner)
        self.owner = self.session["owner"]
        self.runner = None
        self.runtime = None
        self.drafts = {}
        self.notices = deque(maxlen=12)
        self.focus = None
        self.panel = ""
        self.closed = False
        self.on_change = lambda: None
        self.on_stream = lambda event: None
        self.started = time.monotonic()
        self._check_mode([w["id"] for w in self.workflows()], require_known=False)
        self.notice("離線示範：不呼叫模型，不執行真實命令。輸入「協作」體驗父子 Agent、問題和審批。" if demo
                    else "輸入任務開始。模型與網頁工具會產生 API 費用；/help 查看指令。")
        if self.sessions.active_workflows(self.session["id"]):
            self.notice("這個會話有未完成工作。輸入 /resume 繼續，或直接回答待處理問題。")

    @staticmethod
    def resolve(token, rows):
        exact = [r for r in rows if r["id"] == token]
        hits = exact or [r for r in rows if len(token) >= 6 and r["id"].startswith(token)]
        if len(hits) != 1:
            raise PlatformError("AMBIGUOUS_ID", "請使用畫面中的唯一 ID（至少 6 個字元）。")
        return hits[0]

    @property
    def busy(self):
        return self.runner is not None and not self.runner.done()

    def notice(self, text):
        self.notices.append(safe_text(text))
        self.on_change()

    def workflows(self):
        return self.store.all("SELECT w.* FROM workflows w JOIN session_turns t ON t.workflow_id=w.id WHERE t.session_id=? ORDER BY t.ordinal", (self.session["id"],))

    def tasks(self):
        workflows = self.workflows()
        return self.store.all("SELECT * FROM tasks WHERE workflow_id=? ORDER BY created,id", (workflows[-1]["id"],)) if workflows else []

    def pending(self):
        return self.store.all("""SELECT a.*,t.owner FROM approvals a JOIN tasks t ON t.id=a.task_id
            JOIN session_turns s ON s.workflow_id=a.workflow_id WHERE s.session_id=?
            AND a.status='OPEN' ORDER BY a.created,a.id""", (self.session["id"],))

    def observe(self, event):
        tid = event["task_id"]
        if event["type"] == "ModelStarted":
            self.drafts[tid] = {"raw": "", "text": "", "reasoning": 0, "state": "串流中", "call_id": event["call_id"]}
        elif event["type"] == "ModelDelta":
            draft = self.drafts.get(tid)
            if draft and draft["call_id"] == event["call_id"]:
                delta = event["delta"]
                if delta["kind"] == "content":
                    draft["raw"] = (draft["raw"] + delta["text"])[:128000]
                    draft["text"] = summary_draft(draft["raw"])
                else:
                    draft["reasoning"] += delta["chars"]
        elif event["type"] == "ModelFinished" and tid in self.drafts:
            self.drafts[tid]["state"] = "等待驗證" if event["status"] == "SUCCEEDED" else event["status"]
        self.on_stream(event)
        self.on_change()

    def _check_mode(self, ids, *, require_known=True):
        for wid in ids:
            mode = self.store.one("SELECT payload_json FROM events WHERE workflow_id=? AND type='ExecutionMode' ORDER BY id LIMIT 1", (wid,))
            if mode:
                fixture = json.loads(mode["payload_json"])["mode"] == "fixture"
                if fixture != self.demo:
                    raise PlatformError("SESSION_MODE", "此會話的真實／示範模式不同；請以對應模式啟動，或使用 --new。")
                continue
            row = self.store.one("SELECT payload_json FROM events WHERE workflow_id=? AND type='RunConfiguration' ORDER BY id LIMIT 1", (wid,))
            if row:
                config = json.loads(row["payload_json"])
                fixture = config.get("sandbox_class") == "MemorySandbox"
                if fixture != self.demo:
                    raise PlatformError("SESSION_MODE", "此會話的真實／示範模式不同；請使用對應模式或 /new。")
            elif require_known:
                raise PlatformError("SESSION_MODE_UNKNOWN", "未完成輪次缺少執行模式記錄，不能自動恢復或審批。請 /cancel 後建立新輪次。")

    def start(self):
        if self.busy:
            return
        ids = [w["id"] for w in self.sessions.active_workflows(self.session["id"])]
        if not ids:
            self.notice("沒有待恢復工作。")
            return
        self._check_mode(ids)
        self.runtime = Runtime(self.store, self.provider, self.gateway, self.policy,
                               parallelism=self.parallelism, observer=self.observe)
        async def run():
            try:
                await self.runtime.run_until_idle(workflow_ids=ids, max_seconds=self.policy.deadline_seconds + 1)
            except asyncio.CancelledError:
                raise
            except PlatformError as exc:
                self.notice(f"{exc.code}: {exc}")
            except Exception:
                self.notice("RUNTIME_ERROR：執行中斷；/status 檢查，/resume 恢復。")
            finally:
                self.sessions.materialize(ids)
                self.on_change()
        self.runner = asyncio.create_task(run())

    async def pause(self):
        if self.busy:
            self.runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.runner
            self.notice("已暫停。/resume 恢復；/cancel 取消整輪。已送出的 API 請求可能仍有費用。")
        else:
            self.notice("目前沒有執行中的工作。/exit 離開。")

    async def close(self):
        if self.busy:
            await self.pause()
        self.closed = True
        self.on_change()

    async def answer(self, token, decision, text=None):
        item = self.resolve(token, self.pending())
        self._check_mode([item["workflow_id"]])
        if (item["tool_name"] == "ask_human") != (decision == "answer"):
            raise PlatformError("RESPONSE_KIND", "問題請用 /answer；操作審批請用 /approve 或 /deny。")
        result = self.store.answer(item["id"], decision, text)
        self.notice(f"{item['owner']} · {item['id'][:16]} · {result['status']}")
        self.panel = ""
        self.start()

    async def submit(self, text):
        text = text.strip()
        if not text:
            return True
        try:
            if text.startswith("/"):
                await self.command(text)
            else:
                questions = [p for p in self.pending() if p["tool_name"] == "ask_human"]
                if len(questions) == 1:
                    await self.answer(questions[0]["id"], "answer", text)
                elif self.busy or self.sessions.active_workflows(self.session["id"]):
                    raise PlatformError("TURN_ACTIVE", "本輪尚未結束。/pending 查看待答問題；/resume 恢復，/cancel 取消。輸入已保留。")
                else:
                    self.panel, self.focus = "", None
                    self.drafts.clear()
                    self._check_mode([w["id"] for w in self.workflows()], require_known=False)
                    self.sessions.submit(self.session["id"], text, owner=self.owner, policy=self.policy,
                                         execution_mode="fixture" if self.demo else "live")
                    self.start()
            self.on_change()
            return True
        except PlatformError as exc:
            self.notice(f"{exc.code}: {exc}")
            return False

    async def command(self, text):
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()
        if cmd in ("/exit", "/quit"):
            await self.close()
        elif cmd == "/help":
            self.panel = HELP
        elif cmd in ("/new", "/switch"):
            if self.busy:
                raise PlatformError("BUSY", "先用 Ctrl+C 暫停當前工作，再切換會話。")
            if cmd == "/new":
                self.session = self.sessions.create(title=arg or "New session", owner=self.owner)
            else:
                candidate = self.sessions.get(self.resolve(arg, self.sessions.list())["id"])
                ids = [r["workflow_id"] for r in self.store.all("SELECT workflow_id FROM session_turns WHERE session_id=?", (candidate["id"],))]
                self._check_mode(ids, require_known=False)
                self.session = candidate
            self.owner, self.focus, self.panel = self.session["owner"], None, ""
            self.drafts.clear()
        elif cmd == "/sessions":
            self.panel = "會話 · /switch ID\n\n" + "\n".join(f"{s['id']}  {s['turn_count']} 輪  {s['title']}" for s in self.sessions.list())
        elif cmd == "/agent":
            if arg not in AGENTS:
                raise PlatformError("UNKNOWN_AGENT", "可選：" + ", ".join(AGENTS))
            self.owner = arg
            self.notice(f"下一輪主 Agent：{arg}。執行中的任務保持原身份。")
        elif cmd == "/focus":
            self.focus = None if arg == "all" else self.resolve(arg, self.tasks())["id"]
            self.panel = ""
        elif cmd == "/tree":
            self.panel = self.tree_text
        elif cmd == "/pending":
            self.panel = lambda: self.pending_text() or "沒有待處理問題或審批。"
        elif cmd in ("/answer", "/approve", "/deny"):
            token, _, answer = arg.partition(" ")
            await self.answer(token, cmd[1:], answer if cmd == "/answer" else None)
        elif cmd == "/resume":
            self.start()
        elif cmd == "/cancel":
            rows = self.tasks()
            item = self.resolve(arg, rows) if arg else next((r for r in rows if r["parent_id"] is None), None)
            if not item or item["status"] in TERMINAL:
                raise PlatformError("NO_ACTIVE_TASK", "指定任務已結束或不存在。")
            self.store.fail(item["id"], "USER_CANCELLED", status="CANCELLED")
            if item["parent_id"] is None and self.busy:
                await self.pause()
            self.sessions.materialize([item["workflow_id"]])
            self.notice(f"已取消 {item['id'][:16]} 及其未完成子任務。")
            if item["parent_id"] is not None:
                self.start()
        elif cmd == "/status":
            self.panel = lambda: self.status_text() + "\n\n" + self.tree_text()
        elif cmd == "/clear":
            self.panel, self.focus = "", None
            self.notices.clear()
        else:
            raise PlatformError("UNKNOWN_COMMAND", "未知指令。/help 查看說明。")

    def status_text(self):
        workflows = self.workflows()
        if not workflows:
            return f"{self.session['id']} · 下一輪 {self.owner} · 尚未開始"
        wf = workflows[-1]
        calls = self.store.all("SELECT usage_json,status FROM llm_calls WHERE workflow_id=?", (wf["id"],))
        usage = summarize_usage(calls)
        tokens = usage["reported_totals"]["total_tokens"]
        known = usage["calls_reporting"]["total_tokens"]
        token_detail = ""
        prompt, completion = (usage["reported_totals"][k] for k in ("prompt_tokens", "completion_tokens"))
        if prompt is not None and completion is not None:
            token_detail = f"（輸入 {prompt}／輸出 {completion}）"
        cache = usage["cache"]
        rate = cache["hit_rate_on_reported_prompt"]
        cache_detail = (f" · 輸入快取 {rate:.1%}（已回報 {cache['calls_with_consistent_breakdown']}/{len(calls)} 次）"
                        if rate is not None else " · 輸入快取未知" if calls else "")
        policy = json.loads(wf["policy_json"])
        execution = self.sessions.execution_summary(self.session["id"], wf["id"])
        web, task_counts = execution["web"], execution["tasks"]
        warning = (" · 含失敗子任務／請核對交付" if task_counts["failed_children"] else
                   " · 含取消子任務／請核對交付" if task_counts["cancelled_children"] else
                   " · 未判定需求達成" if wf["status"] == "COMPLETED" else "")
        return (f"{lifecycle_text(wf['status'])}{warning} · 模型 {len(calls)}/{wf['max_calls']} 次 · 任務 {task_counts['total']}/{wf['max_tasks']}（完成 {task_counts['completed']}／失敗 {task_counts['failed']}／逾時 {task_counts['timed_out']}／取消 {task_counts['cancelled']}）"
                f" · 網頁 {web['total']}/{policy.get('max_web_requests', 20)} 次（成功 {web['succeeded']}／失敗 {web['failed']}／未知 {web['unknown']}） · token {tokens if tokens is not None else '未知'}" + token_detail + cache_detail
                + (f"（{len(calls)-known} 次用量未知）" if known < len(calls) else "")
                + (" · 錯誤碼 " + ", ".join(execution["error_codes"]) if execution["error_codes"] else "")
                + (f" · 剩餘 {max(0, int(wf['deadline']-time.time()))} 秒" if wf["status"] == "OPEN" else ""))

    def tree_text(self):
        rows = self.tasks()
        result = ["AGENTS · /focus ID 查看詳情", "狀態表示執行進度，交付仍需核對。"]
        children = {}
        for row in rows:
            children.setdefault(row["parent_id"], []).append(row)
        def visit(parent, prefix=""):
            for task in children.get(parent, []):
                mark = {"COMPLETED": "■", "FAILED": "!", "CANCELLED": "×", "TIMED_OUT": "!", "WAITING": "◌", "RUNNING": "●"}.get(task["status"], "·")
                result.append(f"{prefix}{mark} {task['owner'] or 'routing'} · {task['kind']} · {lifecycle_text(task['status'])}\n{prefix}  {task['id'][:16]}")
                operation = self.store.one("SELECT tool_name,status FROM operations WHERE task_id=? ORDER BY created DESC,id DESC LIMIT 1", (task["id"],))
                if operation:
                    result.append(f"{prefix}  {operation['tool_name']} · {operation['status']}")
                waits = self.store.all("SELECT kind FROM waits WHERE task_id=? AND status='OPEN'", (task["id"],))
                if waits:
                    result.append(f"{prefix}  等待：" + ", ".join(dict.fromkeys(w['kind'] for w in waits)))
                if task.get("error"):
                    result.append(f"{prefix}  {task['error']}")
                visit(task["id"], prefix + "  └─")
        visit(None)
        return safe_text("\n".join(result))

    def pending_text(self):
        lines = []
        for item in self.pending():
            args = json.loads(item["args_json"])
            ref = item["id"][:20]
            lines.append(f"{item['owner']} · {item['task_id'][:16]} · {ref}")
            if item["tool_name"] == "ask_human":
                lines.extend([args["question"], f"/answer {ref} 你的答案"])
            else:
                lines.extend(["審批 " + item["tool_name"], json.dumps(args, ensure_ascii=False, indent=2),
                              f"/approve {ref}  或  /deny {ref}"])
            lines.append("")
        return safe_text("\n".join(lines))

    def transcript(self):
        if self.panel:
            return safe_text(self.panel() if callable(self.panel) else self.panel)
        lines = []
        if not self.focus:
            for entry in self.sessions.history(self.session["id"]):
                label = "你" if entry["role"] == "user" else entry["owner"]
                lines.extend([f"{label} ───────────────────", entry["content"], ""])
                if entry["artifact_ids"]:
                    lines.append("產物：" + ", ".join(entry["artifact_ids"]))
                if entry.get("execution_summary"):
                    lines.extend([execution_summary_text(entry["execution_summary"]), ""])
        for task in self.tasks():
            if self.focus and self.focus != task["id"]:
                continue
            if self.focus:
                lines.extend([f"{task['owner']} · {task['kind']} · {lifecycle_text(task['status'])}", task["id"],
                              f"父任務：{task['parent_id'] or 'root'}", "任務：" + task["goal"], ""])
                ops = self.store.all("SELECT tool_name,status,error FROM operations WHERE task_id=? ORDER BY created", (task["id"],))
                lines.extend(f"  {op['tool_name']} · {op['status']} {op['error'] or ''}" for op in ops)
                if task["result_json"]:
                    result = json.loads(task["result_json"])
                    lines.extend(["", result.get("summary", ""), "產物：" + ", ".join(result.get("artifact_ids", []))])
                if task["parent_id"] is None:
                    lines.extend(["", execution_summary_text(self.sessions.execution_summary(self.session["id"], task["workflow_id"]))])
            draft = self.drafts.get(task["id"])
            if draft and task["status"] not in TERMINAL and (draft["text"] or draft["state"] == "串流中"):
                lines.extend([f"{task['owner']} · {task['id'][:16]} · {draft['state']}（草稿）",
                              draft["text"] or ("正在思考…" if draft["reasoning"] else "等待模型輸出…"), ""])
            if task["status"] in ("FAILED", "CANCELLED", "TIMED_OUT"):
                lines.append(f"{task['owner']} · {task['status']} · {task['error'] or ''}")
        if not lines:
            lines = ["MULTI AGENT", "", "在一個終端裡規劃、委派、審查和交付。", "",
                     "主對話在左側，父子任務樹在右側。", "Agent 提問和操作審批會顯示在輸入框上方。", "", "輸入 /help 查看指令。"]
        return safe_text("\n".join(lines))


async def run_plain(controller):
    """Line-oriented fallback; same commands and exact approval semantics."""
    printed = set()
    last_stream = {}
    def stream(event):
        draft = controller.drafts.get(event["task_id"], {})
        text = draft.get("text", "")
        key = event["call_id"]
        previous = last_stream.get(key, "")
        if text.startswith(previous) and len(text) > len(previous):
            print(f"[{event['owner']} {event['task_id'][:16]} 草稿] " + text[len(previous):], flush=True)
            last_stream[key] = text
    controller.on_stream = stream
    loop = asyncio.get_running_loop()
    transport = None
    reader = None
    previous_signal = None
    # Unix terminals/pipes must be cancellable. A blocking executor readline
    # prevents asyncio.run from shutting down until a human enters another line.
    if os.name != "nt" and not isinstance(sys.stdin, io.StringIO):
        fd = sys.stdin.fileno()
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            reader = asyncio.StreamReader(limit=262144)
            pipe = os.fdopen(os.dup(fd), "rb", buffering=0)
            try:
                transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), pipe)
            except BaseException:
                pipe.close()
                raise
    async def interrupt():
        await controller.pause()
        print(controller.notices[-1], flush=True)
    if os.name != "nt":
        previous_signal = signal.getsignal(signal.SIGINT)
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(interrupt()))
    print("MULTI AGENT · " + ("OFFLINE DEMO" if controller.demo else "DEEPSEEK"), flush=True)
    print(HELP, flush=True)
    async def readline():
        if reader:
            return (await reader.readline()).decode(sys.stdin.encoding or "utf-8", errors="replace")
        if os.name != "nt" or isinstance(sys.stdin, io.StringIO):
            return sys.stdin.readline()  # regular local file / in-memory input
        # Windows console fallback uses a daemon rather than an executor worker:
        # a cancelled input cannot hold asyncio's executor shutdown open.
        import threading
        future = loop.create_future()
        def read():
            line = sys.stdin.readline()
            def deliver():
                if not future.done():
                    future.set_result(line)
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(deliver)
        threading.Thread(target=read, daemon=True).start()
        return await future
    async def monitor():
        last_state = None
        while not controller.closed:
            for entry in controller.sessions.history(controller.session["id"]):
                if entry["id"] not in printed and (entry["role"] == "assistant" or entry.get("execution_summary")):
                    if entry["role"] == "assistant":
                        print(f"\n[{entry['owner']} 流程結束] {safe_text(entry['content'])}", flush=True)
                    if entry.get("execution_summary"):
                        print(safe_text(execution_summary_text(entry["execution_summary"])), flush=True)
                    printed.add(entry["id"])
            state = controller.tree_text() + "\n" + controller.pending_text()
            if state != last_state:
                print(state, flush=True)
                last_state = state
            await asyncio.sleep(.1)
    watcher = asyncio.create_task(monitor())
    try:
        while not controller.closed:
            line = await readline()
            if not line:
                await controller.close()
                break
            accepted = await controller.submit(line)
            if controller.panel:
                print(controller.transcript(), flush=True)
            if controller.notices:
                print(controller.notices[-1], flush=True)
            if not accepted:
                print("輸入未送出：" + safe_text(line), flush=True)
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await controller.close()
        if transport:
            transport.close()
        if previous_signal is not None:
            loop.remove_signal_handler(signal.SIGINT)
            signal.signal(signal.SIGINT, previous_signal)


async def run_tui(controller, *, input=None, output=None):
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.document import Document
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout import ConditionalContainer, HSplit, VSplit, Layout, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.styles import Style
        from prompt_toolkit.widgets import Frame, TextArea
    except ImportError as exc:
        raise PlatformError("TERMINAL_DEPENDENCY", "請先執行 python -m pip install -e '.[terminal]'，或使用 shell --plain。") from exc

    transcript = TextArea(read_only=True, scrollbar=True, wrap_lines=True)
    tree = TextArea(read_only=True, scrollbar=True, wrap_lines=True, width=34)
    pending = TextArea(read_only=True, scrollbar=True, wrap_lines=True, height=Dimension(min=3, max=10))
    commands = [word for line in HELP.splitlines() for word in line.split() if word.startswith("/")]
    composer = TextArea(height=3, prompt=" › ", multiline=True, wrap_lines=True,
                        history=InMemoryHistory(), completer=WordCompleter(sorted(set(commands))))
    kb = KeyBindings()
    sending = False
    @kb.add("enter", filter=Condition(lambda: app.layout.has_focus(composer)))
    async def submit(event):
        nonlocal sending
        if sending:
            return
        sending = True
        value = composer.text
        try:
            if await controller.submit(value):
                composer.buffer.append_to_history()
                composer.buffer.reset()
            if controller.closed:
                app.exit()
        finally:
            sending = False
    @kb.add("escape", "enter", filter=Condition(lambda: app.layout.has_focus(composer)))
    def newline(event):
        composer.buffer.insert_text("\n")
    @kb.add("c-c")
    async def pause(event):
        await controller.pause()
    @kb.add("c-d")
    async def leave(event):
        await controller.close()
        app.exit()
    @kb.add("f2")
    def cycle(event):
        ids = [None] + [t["id"] for t in controller.tasks()]
        controller.focus = ids[(ids.index(controller.focus) + 1) % len(ids)] if controller.focus in ids else None
        controller.panel = ""
        app.invalidate()
    @kb.add("pageup")
    def page_up(event):
        transcript.buffer.cursor_up(count=10)
    @kb.add("pagedown")
    def page_down(event):
        transcript.buffer.cursor_down(count=10)
    @kb.add("c-l")
    def clear(event):
        controller.panel = ""
        controller.focus = None
        app.invalidate()

    def header():
        mode = "OFFLINE DEMO" if controller.demo else getattr(controller.provider, "model", "DEEPSEEK")
        return [("class:brand", "  ◈ MULTI AGENT  "), ("class:muted", safe_text(f"{mode}  /  {controller.owner}  /  {controller.session['id'][:18]}"))]
    def footer():
        return [("class:muted", " Enter 送出 · Alt+Enter 換行 · F2 Agent · PgUp/PgDn 捲動 · Ctrl+C 暫停 · /help ")]
    wide = Condition(lambda: app.output.get_size().columns >= 92)
    body = VSplit([transcript, ConditionalContainer(Frame(tree, title="任務樹", style="class:tree"), wide)])
    root = HSplit([Window(FormattedTextControl(header), height=1),
                   Window(FormattedTextControl(lambda: [("class:muted", " " + controller.status_text())]), height=1),
                   body,
                   ConditionalContainer(Frame(pending, title="等待你的回覆", style="class:pending"), Condition(lambda: bool(controller.pending()))),
                   Window(FormattedTextControl(lambda: [("class:notice", " " + (controller.notices[-1] if controller.notices else ""))]), height=2, wrap_lines=True),
                   Frame(composer, title="對話 / 指令"), Window(FormattedTextControl(footer), height=1)])
    app = Application(layout=Layout(root, focused_element=composer), key_bindings=kb,
                      style=Style.from_dict({"brand": "bold #75dfca", "muted": "#9099a6", "tree": "#a6bacb",
                                             "pending": "#f5c878", "notice": "#b4c5d8", "frame.border": "#414b58"}),
                      full_screen=True, mouse_support=True, input=input, output=output,
                      min_redraw_interval=.05)
    controller.on_change = app.invalidate
    def update(area, text, follow=True):
        # Bound rendered history only; the SQLite history and model context are untouched.
        text = text[-100000:]
        if area.text != text:
            old_pos = area.buffer.cursor_position
            at_end = old_pos == len(area.text)
            pos = len(text) if follow and at_end else min(old_pos, len(text))
            area.buffer.set_document(Document(text, pos), bypass_readonly=True)
    async def refresh():
        previous = None
        while not controller.closed:
            state = (controller.transcript(), controller.tree_text(), controller.pending_text(), controller.status_text())
            if state != previous:
                update(transcript, state[0])
                update(tree, state[1], follow=False)
                update(pending, state[2], follow=False)
                app.invalidate()
                previous = state
            await asyncio.sleep(.1)
    watcher = asyncio.create_task(refresh())
    try:
        await app.run_async()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await controller.close()
        controller.on_change = lambda: None
