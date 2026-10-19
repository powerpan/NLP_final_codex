"""Offline controller and keyboard checks; no live API or external Git effects."""
import asyncio
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from maplatform.demo import tool
from maplatform.domain import PlatformError, Policy, dumps
from maplatform.provider import ScriptedProvider
from maplatform.runtime import function
from maplatform.sessions import SessionService
from maplatform.store import Store
from maplatform.terminal import TerminalController, run_tui, safe_text, summary_draft
from maplatform.tools import MemorySandbox, ToolGateway

try:
    import markdown_it
    from prompt_toolkit import Application
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput
except ImportError:
    Application = None


def final(summary="Completed"):
    return {"role": "assistant", "content": dumps({"summary": summary, "artifact_ids": []}), "reasoning_content": "private test reasoning"}


def calling(*calls):
    return {"role": "assistant", "content": "", "reasoning_content": "private test reasoning", "tool_calls": list(calls)}


def replies(messages):
    return {message["tool_call_id"]: json.loads(message["content"]) for message in messages if message["role"] == "tool"}


async def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Offline terminal condition did not become true")
        await asyncio.sleep(0.005)


class ApprovalGateway(ToolGateway):
    """Expose the real approval path while recording, never performing, push."""

    def __init__(self, store, policy):
        super().__init__(store, MemorySandbox(), policy)
        self.executions = []

    def schemas(self, agent, kind="WORK"):
        result = super().schemas(agent, kind)
        if agent.id == "coder" and kind == "WORK":
            result += [function("git_push", "Offline approval fixture", {"remote": {"type": "string"}, "branch": {"type": "string"}}, ("remote", "branch"))]
        return result

    async def execute(self, task, name, args):
        if name == "git_push":
            self.executions.append({"workflow_id": task["workflow_id"], "arguments": args})
            return {"fixture": True, "pushed": False}
        return await super().execute(task, name, args)


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "terminal.sqlite"
        self.store = Store(self.path)
        self.policy = Policy(deadline_seconds=20)
        self.gateway = ApprovalGateway(self.store, self.policy)
        self.controllers = []

    async def asyncTearDown(self):
        for controller in self.controllers:
            await controller.close()
        self.store.close()
        self.temp.cleanup()

    def controller(self, provider=None, **kwargs):
        controller = TerminalController(self.store, provider or ScriptedProvider([]), self.gateway,
                                        policy=self.policy, demo=True, **kwargs)
        self.controllers.append(controller)
        return controller

    async def idle(self, controller):
        if controller.runner:
            await asyncio.wait_for(asyncio.shield(controller.runner), timeout=3)

    async def test_multiturn_public_context_history_and_clear(self):
        provider = ScriptedProvider([final("First answer"), final("Second answer")])
        controller = self.controller(provider)
        self.assertTrue(await controller.submit("First request"))
        await self.idle(controller)
        self.assertTrue(await controller.submit("Follow up"))
        await self.idle(controller)
        history = controller.sessions.history(controller.session["id"])
        self.assertEqual([item["content"] for item in history], ["First request", "First answer", "Follow up", "Second answer"])
        context = json.loads(provider.calls[1]["messages"][1]["content"])
        self.assertEqual([item["content"] for item in context["session"]["previous_turns"]], ["First request", "First answer"])
        self.assertNotIn("private test reasoning", dumps(context))
        self.assertIn("Second answer", controller.transcript())
        self.assertTrue(await controller.submit("/clear"))
        self.assertEqual(controller.sessions.history(controller.session["id"]), history)
        self.assertEqual(len(controller.workflows()), 2)

    async def test_finished_workflow_displays_child_failure_without_claiming_goal_success(self):
        def script(messages, tools, *, task, **kwargs):
            if task["owner"] == "researcher":
                raise PlatformError("CONTEXT_LIMIT", "private child details")
            if not replies(messages):
                return calling(tool("delegate", "delegate_task", {"goal": "Research sources", "target_agent": "researcher", "required_capabilities": ["research"], "acceptance": []}))
            return final("No news could be delivered")
        controller = self.controller(ScriptedProvider(script))
        await controller.submit("Find three news stories")
        await self.idle(controller)
        self.assertEqual(controller.workflows()[-1]["status"], "COMPLETED")
        status, transcript, tree = controller.status_text(), controller.transcript(), controller.tree_text()
        for rendered in (status, transcript):
            self.assertIn("流程結束", rendered)
            self.assertIn("含失敗子任務／請核對交付", rendered)
            self.assertIn("CONTEXT_LIMIT", rendered)
            self.assertNotIn("private child details", rendered)
        self.assertIn("流程結束不等於需求驗證通過", transcript)
        self.assertIn("網頁請求成功 0 · 失敗 0 · 結果未知 0", transcript)
        self.assertNotIn("✓", tree)
        self.assertIn("No news could be delivered", transcript)

    async def test_completed_tasks_do_not_mark_general_goal_as_verified(self):
        controller = self.controller(ScriptedProvider([final("Everything is definitely verified")]))
        await controller.submit("An ordinary request")
        await self.idle(controller)
        self.assertIn("未判定需求達成", controller.status_text())
        self.assertIn("流程結束不等於需求驗證通過", controller.transcript())
        self.assertNotIn("含失敗子任務", controller.status_text())

    async def test_active_turn_rejects_new_text_without_new_workflow(self):
        entered = asyncio.Event()

        async def script(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        controller = self.controller(ScriptedProvider(script))
        self.assertTrue(await controller.submit("Current request"))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertFalse(await controller.submit("Keep this unsent text"))
        self.assertIn("TURN_ACTIVE", controller.notices[-1])
        self.assertIn("輸入已保留", controller.notices[-1])
        self.assertEqual(len(controller.workflows()), 1)
        self.assertEqual([m["content"] for m in controller.sessions.history(controller.session["id"])], ["Current request"])
        session_id = controller.session["id"]
        self.assertFalse(await controller.submit("/new During work"))
        self.assertEqual(controller.session["id"], session_id)
        self.assertTrue(await controller.submit("/cancel"))
        self.assertFalse(controller.busy)
        self.assertEqual(controller.workflows()[-1]["status"], "CANCELLED")
        self.assertEqual(self.store.one("SELECT status FROM llm_calls")["status"], "UNKNOWN")

    async def test_new_turn_is_not_orphaned_while_previous_scheduler_is_settling(self):
        controller = self.controller(ScriptedProvider([final("First answer")]))
        await controller.submit("First")
        await self.idle(controller)
        # Task completion can become visible before the old scheduler's final
        # cleanup returns. Accepting here would create a turn that start skips.
        settling = asyncio.Event()
        controller.runner = asyncio.create_task(settling.wait())
        self.assertFalse(await controller.submit("Next request"))
        self.assertEqual(len(controller.workflows()), 1)
        settling.set()
        await controller.runner

    async def test_child_question_can_be_answered_in_same_interface_with_plain_text(self):
        def script(messages, tools, *, task, **kwargs):
            seen = replies(messages)
            if task["owner"] == "planner":
                if "delegate" not in seen:
                    return calling(tool("delegate", "delegate_task", {"goal": "Ask about format", "target_agent": "researcher", "required_capabilities": ["research"], "acceptance": []}))
                return final("Parent received: " + seen["delegate"]["result"]["summary"])
            if "question" not in seen:
                return calling(tool("question", "ask_human", {"question": "輸出要用哪種語言？"}))
            return final("Use " + seen["question"]["answer"])

        controller = self.controller(ScriptedProvider(script))
        await controller.submit("Delegate a question")
        await self.idle(controller)
        pending = controller.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["owner"], "researcher")
        self.assertIn("輸出要用哪種語言？", controller.pending_text())
        self.assertTrue(await controller.submit("繁體中文"))
        await self.idle(controller)
        self.assertEqual(controller.pending(), [])
        history = controller.sessions.history(controller.session["id"])
        self.assertEqual(history[-1]["content"], "Parent received: Use 繁體中文")
        self.assertTrue(any(item.get("kind") == "clarification" and "繁體中文" in item["content"] for item in history))
        self.assertEqual({row["status"] for row in controller.tasks()}, {"COMPLETED"})

    async def test_multiple_child_questions_require_id_until_only_one_remains(self):
        def script(messages, tools, *, task, **kwargs):
            seen = replies(messages)
            if task["owner"] == "planner":
                if not seen:
                    return calling(*[tool(str(index), "delegate_task", {"goal": f"Part {index}", "target_agent": "researcher", "required_capabilities": ["research"], "acceptance": []}) for index in (1, 2)])
                return final("All child answers received")
            if not seen:
                return calling(tool("question", "ask_human", {"question": task["goal"] + "?"}))
            return final(seen["question"]["answer"])

        controller = self.controller(ScriptedProvider(script))
        await controller.submit("Two questions")
        await self.idle(controller)
        pending = controller.pending()
        self.assertEqual(len(pending), 2)
        self.assertFalse(await controller.submit("Ambiguous answer"))
        self.assertEqual(len(controller.pending()), 2)
        self.assertTrue(await controller.submit(f"/answer {pending[0]['id']} First answer"))
        await self.idle(controller)
        self.assertEqual(len(controller.pending()), 1)
        self.assertTrue(await controller.submit("Second answer"))
        await self.idle(controller)
        self.assertEqual(controller.workflows()[-1]["status"], "COMPLETED")

    async def test_approval_requires_named_command_and_executes_exact_arguments_once(self):
        args = {"remote": "origin", "branch": "fixture-only"}
        controller = self.controller(ScriptedProvider([calling(tool("push", "git_push", args)), final("Approval processed")]), owner="coder")
        await controller.submit("Prepare proposed push")
        await self.idle(controller)
        approval = controller.pending()[0]
        self.assertIn('"branch": "fixture-only"', controller.pending_text())
        for text in ("yes", "approve", f"/answer {approval['id']} yes", "/approve", "/approve approval_unknown"):
            self.assertFalse(await controller.submit(text))
            self.assertEqual(self.gateway.executions, [])
            self.assertEqual(controller.pending()[0]["status"], "OPEN")
        self.assertTrue(await controller.submit(f"/approve {approval['id']}"))
        await self.idle(controller)
        self.assertEqual(self.gateway.executions, [{"workflow_id": approval["workflow_id"], "arguments": args}])
        self.assertEqual(controller.workflows()[-1]["status"], "COMPLETED")
        self.assertFalse(await controller.submit(f"/approve {approval['id']}"))
        self.assertEqual(len(self.gateway.executions), 1)

    async def test_deny_does_not_execute_the_proposed_operation(self):
        controller = self.controller(ScriptedProvider([calling(tool("push", "git_push", {"remote": "origin", "branch": "fixture"})), final("Denied push respected")]), owner="coder")
        await controller.submit("Propose push")
        await self.idle(controller)
        approval = controller.pending()[0]
        self.assertTrue(await controller.submit(f"/deny {approval['id']}"))
        await self.idle(controller)
        self.assertEqual(self.gateway.executions, [])
        self.assertEqual(self.store.one("SELECT status FROM approvals WHERE id=?", (approval["id"],))["status"], "DENIED")
        self.assertEqual(controller.workflows()[-1]["status"], "COMPLETED")

    async def test_switch_approval_and_cancel_are_scoped_to_current_session(self):
        def script(messages, tools, *, task, **kwargs):
            if not replies(messages):
                return calling(tool("push", "git_push", {"remote": "origin", "branch": task["goal"]}))
            return final("Approved " + task["goal"])

        controller = self.controller(ScriptedProvider(script), owner="coder")
        first_session = controller.session["id"]
        await controller.submit("A")
        await self.idle(controller)
        first_approval = controller.pending()[0]
        first_task = controller.tasks()[0]["id"]
        self.assertTrue(await controller.submit("/new Session B"))
        second_session = controller.session["id"]
        await controller.submit("B")
        await self.idle(controller)
        second_approval = controller.pending()[0]
        for text in (f"/approve {first_approval['id']}", f"/deny {first_approval['id']}", f"/cancel {first_task}"):
            self.assertFalse(await controller.submit(text))
        self.assertEqual(self.gateway.executions, [])
        self.assertEqual(self.store.one("SELECT status FROM approvals WHERE id=?", (first_approval["id"],))["status"], "OPEN")
        foreign = SessionService(self.store).create("Other human", user_id="another-human")
        self.assertFalse(await controller.submit(f"/switch {foreign['id']}"))
        self.assertEqual(controller.session["id"], second_session)
        self.assertTrue(await controller.submit(f"/switch {first_session}"))
        self.assertEqual([item["id"] for item in controller.pending()], [first_approval["id"]])
        self.assertTrue(await controller.submit(f"/approve {first_approval['id']}"))
        await self.idle(controller)
        self.assertEqual([item["workflow_id"] for item in self.gateway.executions], [first_approval["workflow_id"]])
        self.assertEqual(self.store.one("SELECT status FROM approvals WHERE id=?", (second_approval["id"],))["status"], "OPEN")
        self.assertTrue(await controller.submit(f"/switch {second_session}"))
        self.assertTrue(await controller.submit(f"/deny {second_approval['id']}"))
        await self.idle(controller)
        self.assertEqual(len(self.gateway.executions), 1)

    async def test_restart_restores_nested_question_and_cancel_expires_entire_tree(self):
        def script(messages, tools, *, task, **kwargs):
            if task["owner"] in ("planner", "coder"):
                target = "coder" if task["owner"] == "planner" else "reviewer"
                return calling(tool("delegate", "delegate_task", {"goal": "Nested work", "target_agent": target, "required_capabilities": [], "acceptance": []}))
            return calling(tool("question", "ask_human", {"question": "Grandchild needs a decision"}))

        controller = self.controller(ScriptedProvider(script))
        await controller.submit("Start nested tasks")
        await self.idle(controller)
        session_id = controller.session["id"]
        approval_id = controller.pending()[0]["id"]
        self.assertEqual(sorted(task["depth"] for task in controller.tasks()), [0, 1, 2])
        self.assertIn("reviewer", controller.tree_text())
        await controller.close()
        self.controllers.remove(controller)
        self.store.close()
        self.store = Store(self.path)
        self.gateway = ApprovalGateway(self.store, self.policy)
        restored = self.controller(ScriptedProvider([final("New turn after cancellation")]), session_id=session_id)
        self.assertEqual([item["id"] for item in restored.pending()], [approval_id])
        self.assertTrue(any("未完成" in notice for notice in restored.notices))
        self.assertTrue(await restored.submit("/cancel"))
        self.assertEqual({task["status"] for task in restored.tasks()}, {"CANCELLED"})
        self.assertEqual(restored.pending(), [])
        self.assertEqual(self.store.one("SELECT status FROM approvals WHERE id=?", (approval_id,))["status"], "EXPIRED")
        self.assertTrue(await restored.submit("/resume"))
        self.assertFalse(restored.busy)
        self.assertTrue(await restored.submit("Fresh request"))
        await self.idle(restored)
        history = restored.sessions.history(session_id)
        self.assertEqual(history[0]["status"], "CANCELLED")
        self.assertEqual(history[-1]["content"], "New turn after cancellation")

    async def test_execution_mode_mismatch_blocks_init_and_switch_before_state_change(self):
        controller = self.controller(ScriptedProvider([final("Fixture result")]))
        await controller.submit("Offline task")
        await self.idle(controller)
        fixture_session = controller.session["id"]
        with self.assertRaises(PlatformError) as caught:
            TerminalController(self.store, ScriptedProvider([]), self.gateway, session_id=fixture_session, demo=False)
        self.assertEqual(caught.exception.code, "SESSION_MODE")
        live_session = controller.sessions.create("Live session")
        controller.sessions.submit(live_session["id"], "Live task", execution_mode="live")
        self.assertFalse(await controller.submit(f"/switch {live_session['id']}"))
        self.assertEqual(controller.session["id"], fixture_session)
        self.assertIn("SESSION_MODE", controller.notices[-1])

    async def test_unknown_mode_approval_is_not_mutated_and_requires_cancel(self):
        sessions = SessionService(self.store)
        session = sessions.create("Unmarked interrupted session", owner="coder")
        wf = sessions.submit(session["id"], "Unmarked work")
        self.store.route(wf["root_task_id"], "coder")
        task = self.store.claim(wf["root_task_id"])
        wait = self.store.defer(task["id"], task["active_attempt_id"], "push", "HUMAN_APPROVAL",
                                {"tool_name": "git_push", "arguments": {"remote": "origin", "branch": "fixture"}}, self.policy)
        self.store.suspend(task["id"], task["active_attempt_id"])
        controller = self.controller(session_id=session["id"])
        for command in (f"/approve {wait['ref_id']}", "/resume"):
            self.assertFalse(await controller.submit(command))
            self.assertIn("SESSION_MODE_UNKNOWN", controller.notices[-1])
        self.assertEqual(self.store.one("SELECT status FROM approvals WHERE id=?", (wait["ref_id"],))["status"], "OPEN")
        self.assertEqual(self.gateway.executions, [])
        self.assertFalse(controller.busy)
        self.assertTrue(await controller.submit("/cancel"))
        self.assertEqual(self.store.one("SELECT status FROM approvals WHERE id=?", (wait["ref_id"],))["status"], "EXPIRED")

    async def test_supplied_demo_completes_parent_children_grandchild_and_approval(self):
        from maplatform.terminal_demo import DemoGitGateway, TerminalDemoProvider
        self.gateway = ToolGateway(self.store, MemorySandbox(), self.policy, git=DemoGitGateway())
        controller = self.controller(TerminalDemoProvider())
        self.assertTrue(await controller.submit("協作"))
        await self.idle(controller)
        self.assertEqual(len(controller.tasks()), 5)
        self.assertEqual(max(task["depth"] for task in controller.tasks()), 2)
        self.assertEqual(controller.pending()[0]["tool_name"], "ask_human")
        self.assertTrue(await controller.submit("用作離線教學演示"))
        await self.idle(controller)
        pending = controller.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["tool_name"], "git_push")
        self.assertTrue(await controller.submit(f"/approve {pending[0]['id']}"))
        await self.idle(controller)
        self.assertEqual({task["status"] for task in controller.tasks()}, {"COMPLETED"})
        operation = self.store.one("SELECT * FROM operations WHERE tool_name='git_push'")
        result = json.loads(operation["result_json"])
        self.assertTrue(result["fixture"])
        self.assertFalse(result["performed"])
        self.assertIn("未呼叫 API", controller.sessions.history(controller.session["id"])[-1]["content"])

    async def test_stream_drafts_remain_ephemeral_and_never_show_raw_reasoning(self):
        class Streaming:
            async def stream_complete(self, messages, tools, *, on_delta, **kwargs):
                from maplatform.provider import Completion
                on_delta({"kind": "reasoning", "chars": 10, "text": "PRIVATE REASONING"})
                raw = dumps({"summary": "Draft then final", "artifact_ids": []})
                for char in raw:
                    on_delta({"kind": "content", "text": char})
                return Completion(final("Draft then final"), {"total_tokens": 9})

        controller = self.controller(Streaming())
        await controller.submit("Stream a response")
        await self.idle(controller)
        self.assertEqual(controller.sessions.history(controller.session["id"])[-1]["content"], "Draft then final")
        self.assertNotIn("PRIVATE REASONING", controller.transcript())
        self.assertNotIn("PRIVATE REASONING", repr(controller.drafts))
        self.assertIn("token 9", controller.status_text())
        self.assertNotIn("（草稿）", controller.transcript())

    @unittest.skipIf(Application is None, "Optional terminal dependency is not installed")
    async def test_tui_keyboard_preserves_rejected_composer_text_and_exits_cleanly(self):
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        controller = self.controller(ScriptedProvider(blocked))
        apps = []
        def create_app(*args, **kwargs):
            app = Application(*args, **kwargs)
            apps.append(app)
            return app

        with create_pipe_input() as pipe, patch("prompt_toolkit.Application", side_effect=create_app):
            ui = asyncio.create_task(run_tui(controller, input=pipe, output=DummyOutput()))
            try:
                await until(lambda: apps and apps[0].is_running)
                pipe.send_text("Current request\r")
                await asyncio.wait_for(entered.wait(), 1)
                await until(lambda: apps[0].current_buffer.text == "")
                pipe.send_text("Preserve this text\r")
                await until(lambda: any("TURN_ACTIVE" in notice for notice in controller.notices))
                self.assertEqual(apps[0].current_buffer.text, "Preserve this text")
                self.assertEqual(len(controller.workflows()), 1)
                pipe.send_text("\x04")
                await asyncio.wait_for(ui, 2)
                self.assertTrue(controller.closed)
                self.assertFalse(controller.busy)
                self.assertEqual(self.store.one("SELECT status FROM llm_calls")["status"], "UNKNOWN")
            finally:
                if not ui.done():
                    ui.cancel()
                    await asyncio.gather(ui, return_exceptions=True)

    @unittest.skipIf(Application is None, "Optional terminal dependency is not installed")
    async def test_tui_multiline_keyboard_and_human_reply_share_one_session(self):
        controller = self.controller(ScriptedProvider([calling(tool("question", "ask_human", {"question": "Continue?"})), final("Reply received")]))
        apps = []
        def create_app(*args, **kwargs):
            app = Application(*args, **kwargs)
            apps.append(app)
            return app

        with create_pipe_input() as pipe, patch("prompt_toolkit.Application", side_effect=create_app):
            ui = asyncio.create_task(run_tui(controller, input=pipe, output=DummyOutput()))
            try:
                await until(lambda: apps and apps[0].is_running)
                pipe.send_text("First line\x1b\rSecond line\r")
                await until(lambda: len(controller.pending()) == 1 and not controller.busy)
                self.assertEqual(controller.sessions.history(controller.session["id"])[0]["content"], "First line\nSecond line")
                pipe.send_text("Yes, proceed\r")
                await until(lambda: controller.workflows()[-1]["status"] == "COMPLETED")
                self.assertEqual(controller.sessions.history(controller.session["id"])[-1]["content"], "Reply received")
                pipe.send_text("\x04")
                await asyncio.wait_for(ui, 2)
                self.assertTrue(controller.closed)
            finally:
                if not ui.done():
                    ui.cancel()
                    await asyncio.gather(ui, return_exceptions=True)


@unittest.skipIf(os.name == "nt", "Unix terminal signal test")
class PlainProcessTests(unittest.TestCase):
    def test_interrupt_pauses_and_exit_does_not_wait_for_stdin_eof(self):
        with tempfile.TemporaryDirectory() as folder:
            process = subprocess.Popen([sys.executable, "-m", "maplatform", "shell", "--demo", "--plain", "--db", str(Path(folder) / "plain.sqlite")],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                data = b""
                deadline = time.monotonic() + 5
                while b"/help" not in data and time.monotonic() < deadline:
                    if select.select([process.stdout], [], [], .02)[0]:
                        data += os.read(process.stdout.fileno(), 65536)
                self.assertIn(b"/help", data)
                process.send_signal(signal.SIGINT)
                time.sleep(.1)
                self.assertIsNone(process.poll())
                process.stdin.write(b"/exit\n")
                process.stdin.flush()
                self.assertEqual(process.wait(timeout=3), 0)
                self.assertEqual(process.stderr.read(), b"")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    pipe.close()


class TerminalTextTests(unittest.TestCase):
    def test_summary_draft_decodes_every_fragment_without_exposing_json_escapes(self):
        expected = '你好 "quoted" \\ slash\n第二行 🚀'
        for ensure_ascii in (False, True):
            raw = json.dumps({"summary": expected, "artifact_ids": []}, ensure_ascii=ensure_ascii)
            for index in range(len(raw) + 1):
                with self.subTest(ensure_ascii=ensure_ascii, index=index):
                    draft = summary_draft(raw[:index])
                    self.assertTrue(expected.startswith(draft), repr(draft))
                    self.assertFalse(any(0xD800 <= ord(char) <= 0xDFFF for char in draft))
            self.assertEqual(summary_draft(raw), expected)
        self.assertEqual(summary_draft('{"tool_calls": [{"arguments": "private"}]}'), "")
        self.assertEqual(summary_draft('{"summary": "prefix\\u4f'), "prefix")

    def test_safe_text_strips_ansi_osc_and_keeps_text_after_terminators(self):
        self.assertEqual(safe_text("a\x1b[31mred\x1b[0mz"), "aredz")
        self.assertEqual(safe_text("before\x1b]0;title\x07after"), "beforeafter")
        self.assertEqual(safe_text("before\x1b]0;title\x1b\\after"), "beforeafter")
        self.assertEqual(safe_text("before\x1b]52;c;secret"), "before")
        self.assertEqual(safe_text("line\n\t中文\x00\x07\x7f\x9b\ud800"), "line\n\t中文")

    def test_safe_text_strips_all_directional_controls(self):
        controls = "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
        self.assertEqual(safe_text("before" + controls + "after"), "beforeafter")


if __name__ == "__main__":
    unittest.main()
