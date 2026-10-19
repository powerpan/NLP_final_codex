"""Offline session/restart/ACL tests; no real model or external effects."""
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from maplatform.demo import tool
from maplatform.domain import PlatformError, Policy, dumps
from maplatform.provider import Completion, ScriptedProvider
from maplatform.runtime import Runtime
from maplatform.sessions import SessionService
from maplatform.store import Store
from maplatform.tools import MemorySandbox, ToolGateway


def final(text="Done", artifacts=()):
    return {"role": "assistant", "content": dumps({"summary": text, "artifact_ids": list(artifacts)}), "reasoning_content": "private reasoning"}


def calling(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls), "reasoning_content": "private reasoning"}


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "sessions.sqlite"
        self.store = Store(self.path)
        self.sessions = SessionService(self.store)
        self.gateway = ToolGateway(self.store, MemorySandbox(), Policy())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def run_workflow(self, wf, provider):
        return await Runtime(self.store, provider, self.gateway).run_until_idle(workflow_ids=[wf["id"]], max_seconds=3)

    def assert_code(self, code, operation, *args, **kwargs):
        with self.assertRaises(PlatformError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.sessions = SessionService(self.store)
        self.gateway = ToolGateway(self.store, MemorySandbox(), Policy())

    async def test_two_turns_survive_restart_and_only_public_history_is_injected(self):
        session = self.sessions.create("Conversation")
        first = self.sessions.submit(session["id"], "Remember this exact requirement")
        await self.run_workflow(first, ScriptedProvider([final("Public answer")]))
        self.reopen()
        second = self.sessions.submit(session["id"], "Continue")
        provider = ScriptedProvider([final("Follow-up")])
        await self.run_workflow(second, provider)
        context = json.loads(provider.calls[0]["messages"][1]["content"])
        self.assertEqual([m["content"] for m in context["session"]["previous_turns"]], ["Remember this exact requirement", "Public answer"])
        self.assertNotIn("private reasoning", dumps(context))
        self.assertEqual([e["content"] for e in self.sessions.history(session["id"])], ["Remember this exact requirement", "Public answer", "Continue", "Follow-up"])
        self.assertEqual(self.sessions.materialize(), 0)
        self.assertEqual(self.sessions.get(session["id"])["turn_count"], 2)

    async def test_one_open_turn_and_owner_access_are_enforced_atomically(self):
        session = self.sessions.create(user_id="alice")
        first = self.sessions.submit(session["id"], "First", user_id="alice")
        self.assert_code("SESSION_BUSY", self.sessions.submit, session["id"], "Second", user_id="alice")
        for fn in (self.sessions.get, self.sessions.history, self.sessions.active_workflows):
            self.assert_code("SESSION_NOT_FOUND", fn, session["id"], user_id="bob")
        self.assert_code("SESSION_NOT_FOUND", self.sessions.submit, session["id"], "Attack", user_id="bob")
        self.assertEqual(self.sessions.list(user_id="bob"), [])
        self.assertEqual(len(self.store.all("SELECT * FROM workflows")), 1)
        self.store.fail(first["root_task_id"], "Human cancelled", status="CANCELLED")
        self.sessions.submit(session["id"], "Next", user_id="alice")
        self.assertEqual(self.sessions.history(session["id"], user_id="alice")[0]["status"], "CANCELLED")

    async def test_context_limit_preserves_full_history_without_creating_a_workflow(self):
        session = self.sessions.create()
        first = self.sessions.submit(session["id"], "A" * 2000)
        await self.run_workflow(first, ScriptedProvider([final("B" * 2000)]))
        self.assert_code("CONTEXT_LIMIT", self.sessions.submit, session["id"], "Continue", policy=Policy(max_context_chars=1000))
        self.assertEqual(len(self.store.all("SELECT * FROM workflows")), 1)
        self.assertEqual(self.sessions.history(session["id"])[0]["content"], "A" * 2000)
        self.assertEqual(self.sessions.history(session["id"])[1]["content"], "B" * 2000)

    async def test_server_execution_counts_survive_restart_without_private_child_context(self):
        session = self.sessions.create()
        wf = self.sessions.submit(session["id"], "Find recent news")
        self.store.route(wf["root_task_id"], "planner")
        root = self.store.claim(wf["root_task_id"])
        wait = self.store.defer(root["id"], root["active_attempt_id"], "child", "CHILD_TASK",
                                {"goal": "CHILD_PRIVATE_GOAL", "target_agent": "researcher", "required_capabilities": ["research"], "acceptance": []}, Policy())
        child = self.store.one("SELECT * FROM tasks WHERE id=?", (wait["ref_id"],))
        self.store.route(child["id"], "researcher")
        child = self.store.claim(child["id"])
        self.store.save_snapshot(child["id"], child["active_attempt_id"], [final("CHILD_PRIVATE_CONTENT")])
        for index, (name, state, error) in enumerate([
            ("search_web", "SUCCEEDED", None), ("fetch_web", "FAILED", "WEB_TIMEOUT"),
            ("fetch_web", "UNKNOWN", "TOOL_OUTCOME_UNKNOWN"),
        ]):
            op, _ = self.store.begin_operation(child["id"], child["active_attempt_id"], str(index), name, {"query": "CHILD_PRIVATE_QUERY"})
            self.store.finish_operation(op["id"], {"private": "CHILD_PRIVATE_TOOL_RESULT"}, error=error, status=state)
        self.store.fail(child["id"], "CONTEXT_LIMIT")
        self.store.verify(root["id"], root["active_attempt_id"], {"summary": "No network was available", "artifact_ids": []})
        await self.run_workflow(wf, ScriptedProvider([]))
        self.reopen()
        summary = self.sessions.execution_summary(session["id"], wf["id"])
        self.assertEqual(summary["lifecycle_status"], "COMPLETED")
        self.assertEqual(summary["tasks"]["completed"], 1)
        self.assertEqual(summary["tasks"]["failed_children"], 1)
        self.assertEqual(summary["web"], {"total": 3, "succeeded": 1, "failed": 1, "unknown": 1, "running": 0})
        self.assertEqual(summary["error_codes"], ["CONTEXT_LIMIT", "TOOL_OUTCOME_UNKNOWN", "WEB_TIMEOUT"])
        self.assertIn("does not establish", summary["verification_boundary"])
        history = self.sessions.history(session["id"])
        self.assertEqual(history[-1]["content"], "No network was available")
        self.assertEqual(history[-1]["execution_summary"], summary)
        second = self.sessions.submit(session["id"], "Explain the previous failure")
        context = self.sessions.context_for(self.store.one("SELECT * FROM tasks WHERE id=?", (second["root_task_id"],)))
        self.assertEqual(context["previous_turns"][-1]["execution_summary"], summary)
        self.assertNotIn("CHILD_PRIVATE", dumps(context))
        self.assertNotIn("private reasoning", dumps(context))

    async def test_execution_summary_is_session_scoped_and_failure_history_remains_available(self):
        session = self.sessions.create(user_id="alice")
        other = self.sessions.create(user_id="bob")
        wf = self.sessions.submit(session["id"], "My failed request", user_id="alice")
        secret = self.sessions.submit(other["id"], "Other request", user_id="bob")
        self.store.fail(wf["root_task_id"], "PROVIDER_AUTH")
        self.store.fail(secret["root_task_id"], "OTHER_SESSION_FAILURE")
        self.assert_code("SESSION_NOT_FOUND", self.sessions.execution_summary, session["id"], wf["id"], user_id="bob")
        self.assert_code("SESSION_NOT_FOUND", self.sessions.execution_summary, session["id"], secret["id"], user_id="alice")
        history = self.sessions.history(session["id"], user_id="alice")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["execution_summary"]["error_codes"], ["PROVIDER_AUTH"])
        self.assertEqual(history[0]["execution_summary"]["tasks"]["failed"], 1)
        self.assertNotIn("OTHER_SESSION_FAILURE", dumps(history))

    async def test_execution_summary_does_not_infer_goal_success_from_model_text(self):
        session = self.sessions.create()
        wf = self.sessions.submit(session["id"], "A requested result")
        await self.run_workflow(wf, ScriptedProvider([final("成功。All requirements verified!")]))
        before = self.sessions.execution_summary(session["id"], wf["id"])
        with self.store.tx() as conn:
            conn.execute("UPDATE session_turns SET assistant_text='Unable to fulfill the request' WHERE workflow_id=?", (wf["id"],))
        after = self.sessions.execution_summary(session["id"], wf["id"])
        self.assertEqual(before, after)
        self.assertIn("does not establish", after["verification_boundary"])

    async def test_two_connections_cannot_submit_overlapping_turns(self):
        session = self.sessions.create()
        barrier = threading.Barrier(2)
        def submit_from_connection(text):
            store = Store(self.path)
            try:
                barrier.wait(timeout=3)
                try:
                    SessionService(store).submit(session["id"], text)
                    return "CREATED"
                except PlatformError as exc:
                    return exc.code
            finally:
                store.close()
        outcomes = await asyncio.gather(*(asyncio.to_thread(submit_from_connection, text) for text in ("One", "Two")))
        self.assertCountEqual(outcomes, ["CREATED", "SESSION_BUSY"])
        self.assertEqual(len(self.store.all("SELECT * FROM workflows")), 1)

    async def test_execution_mode_is_durable_before_scheduling_and_atomic_with_turn(self):
        session = self.sessions.create()
        self.assert_code("INVALID_ARGUMENT", self.sessions.submit, session["id"], "Invalid mode", execution_mode="unknown")
        event = self.store.event
        def interrupted(conn, workflow_id, task_id, kind, payload):
            if kind == "ExecutionMode":
                raise RuntimeError("Failure before mode marker commits")
            return event(conn, workflow_id, task_id, kind, payload)
        with patch.object(self.store, "event", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.sessions.submit(session["id"], "Interrupted", execution_mode="fixture")
        self.assertEqual(self.store.all("SELECT * FROM workflows"), [])
        self.assertEqual(self.store.all("SELECT * FROM session_turns"), [])
        self.assertEqual(self.store.all("SELECT * FROM events"), [])
        wf = self.sessions.submit(session["id"], "Persist before runner", execution_mode="fixture")
        self.reopen()
        marker = self.store.one("SELECT * FROM events WHERE workflow_id=? AND type='ExecutionMode'", (wf["id"],))
        self.assertEqual(json.loads(marker["payload_json"]), {"mode": "fixture"})
        self.assertIsNone(self.store.one("SELECT * FROM events WHERE workflow_id=? AND type='RunConfiguration'", (wf["id"],)))

    async def test_no_cross_session_memory_or_private_peer_context(self):
        a, b = self.sessions.create("A"), self.sessions.create("B")
        old = self.sessions.submit(a["id"], "SESSION A SECRET")
        await self.run_workflow(old, ScriptedProvider([final("SESSION A ANSWER")]))
        wf = self.sessions.submit(b["id"], "Session B")
        root = self.store.one("SELECT * FROM tasks WHERE id=?", (wf["root_task_id"],))
        with self.store.tx() as conn:
            conn.execute("INSERT INTO memories VALUES('peer-memory','coder','human:local',?,'CODER PRIVATE SECRET',?)", (wf["id"], time.time()))
        runtime = Runtime(self.store, None, self.gateway)
        context = dumps(runtime.initial_messages(root))
        self.assertNotIn("SESSION A", context)
        self.assertNotIn("CODER PRIVATE SECRET", context)
        self.store.route(root["id"], "planner")
        root = self.store.claim(root["id"])
        wait = self.store.defer(root["id"], root["active_attempt_id"], "child", "CHILD_TASK", {"goal": "Bounded delegated task", "required_capabilities": ["research"], "target_agent": "researcher", "acceptance": []}, Policy())
        child = self.store.one("SELECT * FROM tasks WHERE id=?", (wait["ref_id"],))
        child_context = json.loads(runtime.initial_messages(child)[1]["content"])
        self.assertNotIn("session", child_context)
        legacy = self.store.create_workflow("Legacy command")
        legacy_task = self.store.one("SELECT * FROM tasks WHERE id=?", (legacy["root_task_id"],))
        self.assertNotIn("SESSION A", dumps(runtime.initial_messages(legacy_task)))

    async def test_version_one_database_is_migrated_without_changing_existing_work(self):
        wf = self.store.create_workflow("Legacy workflow")
        self.store.close()
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TABLE session_turns")
        conn.execute("DROP TABLE sessions")
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()
        self.store = Store(self.path)
        self.sessions = SessionService(self.store)
        self.assertEqual(self.store.one("SELECT * FROM workflows WHERE id=?", (wf["id"],))["status"], "OPEN")
        self.assertEqual(self.store.one("PRAGMA user_version")["user_version"], 2)
        self.assertEqual(self.sessions.create()["turn_count"], 0)

    async def test_public_artifact_copy_has_new_id_checksum_and_usable_grants(self):
        session = self.sessions.create(owner="researcher")
        first = self.sessions.submit(session["id"], "Write source")
        def script(messages, tools, **kwargs):
            outputs = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            if not outputs:
                return calling(tool("write", "write_file", {"path": "source.py", "content": "VALUE = 7\n"}))
            if len(outputs) == 1:
                return calling(tool("publish", "publish_artifact", {"path": "source.py", "name": "source.py", "grantees": ["planner", "coder"]}))
            return final("Published", [outputs[-1]["artifact_id"]])
        await self.run_workflow(first, ScriptedProvider(script))
        source_id = self.sessions.history(session["id"])[-1]["artifact_ids"][0]
        second = self.sessions.submit(session["id"], "Delegate a source review", owner="planner")
        root = self.store.one("SELECT * FROM tasks WHERE id=?", (second["root_task_id"],))
        new_id = json.loads(root["input_artifacts_json"])[0]
        self.assertNotEqual(new_id, source_id)
        copied = self.store.one("SELECT * FROM artifacts WHERE id=?", (new_id,))
        self.assertEqual(copied["owner"], "planner")
        self.assertEqual(copied["sha256"], hashlib.sha256(b"VALUE = 7\n").hexdigest())
        self.assertIn("coder", json.loads(copied["grantees_json"]))
        self.store.route(root["id"], "planner")
        root = self.store.claim(root["id"])
        wait = self.store.defer(root["id"], root["active_attempt_id"], "delegate", "CHILD_TASK", {"goal": "Use source", "required_capabilities": ["coding"], "target_agent": "coder", "input_artifact_ids": [new_id], "acceptance": []}, Policy())
        child = self.store.one("SELECT * FROM tasks WHERE id=?", (wait["ref_id"],))
        self.store.route(child["id"], "coder")
        child = self.store.claim(child["id"])
        result = await self.gateway.execute(child, "import_artifact", {"artifact_id": new_id, "path": "source.py"})
        self.assertEqual(result["artifact_id"], new_id)
        self.assertEqual(await self.gateway.sandbox.read(child, "source.py"), "VALUE = 7\n")
        provenance = self.store.one("SELECT payload_json FROM events WHERE workflow_id=? AND type='SessionArtifactCopied'", (second["id"],))
        self.assertEqual(json.loads(provenance["payload_json"])["source_artifact_id"], source_id)

    async def test_artifact_checksum_failure_rolls_back_new_turn(self):
        session = self.sessions.create(owner="researcher")
        first = self.sessions.submit(session["id"], "Source")
        self.store.route(first["root_task_id"], "researcher")
        root = self.store.claim(first["root_task_id"])
        await self.gateway.sandbox.write(root, "a.txt", "good")
        artifact = await self.gateway.execute(root, "publish_artifact", {"path": "a.txt", "name": "a.txt"})
        self.store.verify(root["id"], root["active_attempt_id"], {"summary": "Published", "artifact_ids": [artifact["artifact_id"]]})
        self.store.complete(root["id"])
        with self.store.tx() as conn:
            conn.execute("UPDATE artifacts SET content=? WHERE id=?", (b"tampered", artifact["artifact_id"]))
        self.assert_code("ARTIFACT_CORRUPT", self.sessions.submit, session["id"], "Continue")
        self.assertEqual(len(self.store.all("SELECT * FROM workflows")), 1)

    async def test_parent_multiple_children_restart_human_answer_and_aggregation(self):
        session = self.sessions.create()
        wf = self.sessions.submit(session["id"], "Research two parts and aggregate")
        def script(messages, tools, *, task, **kwargs):
            replies = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m["role"] == "tool"}
            if task["owner"] == "planner":
                if not replies:
                    return calling(*[tool(f"part-{i}", "delegate_task", {"goal": f"Part {i}", "required_capabilities": ["research"], "target_agent": "researcher", "acceptance": [{"kind": "nonempty"}]}) for i in (1, 2)])
                return final(" + ".join(replies[f"part-{i}"]["result"]["summary"] for i in (1, 2)))
            if not replies:
                return calling(tool("question", "ask_human", {"question": f"Confirm {task['goal']}?"}))
            return final(task["goal"] + ": " + replies["question"]["answer"])
        await self.run_workflow(wf, ScriptedProvider(script))
        self.assertEqual(len(self.store.all("SELECT * FROM tasks WHERE status='WAITING'")), 3)
        self.reopen()
        for approval in self.store.all("SELECT * FROM approvals WHERE status='OPEN'"):
            self.store.answer(approval["id"], "answer", "confirmed")
        await self.run_workflow(wf, ScriptedProvider(script))
        self.assertEqual(self.sessions.history(session["id"])[-1]["content"], "Part 1: confirmed + Part 2: confirmed")
        self.assertEqual(len(self.store.all("SELECT * FROM tasks WHERE status='COMPLETED'")), 3)
        self.assertEqual(len([e for e in self.sessions.history(session["id"]) if e.get("kind") == "clarification"]), 2)

    async def test_cancelling_parent_cancels_descendants_and_expires_questions(self):
        session = self.sessions.create()
        wf = self.sessions.submit(session["id"], "Start child")
        def script(messages, tools, *, task, **kwargs):
            if task["owner"] == "planner":
                return calling(tool("delegate", "delegate_task", {"goal": "Ask human", "required_capabilities": ["research"], "target_agent": "researcher", "acceptance": []}))
            return calling(tool("question", "ask_human", {"question": "Proceed?"}))
        await self.run_workflow(wf, ScriptedProvider(script))
        self.store.fail(wf["root_task_id"], "User cancelled", status="CANCELLED")
        self.assertEqual({r["status"] for r in self.store.all("SELECT * FROM tasks")}, {"CANCELLED"})
        self.assertEqual(self.store.one("SELECT * FROM approvals")["status"], "EXPIRED")
        await self.run_workflow(wf, ScriptedProvider([]))
        self.assertEqual(self.sessions.active_workflows(session["id"]), [])

    async def test_scoped_recovery_never_touches_an_unrelated_active_worker(self):
        unrelated = self.store.create_workflow("Another terminal owns this")
        self.store.route(unrelated["root_task_id"], "planner")
        other = self.store.claim(unrelated["root_task_id"])
        call_id = self.store.reserve_call(other["id"], other["active_attempt_id"], "AGENT", "hash")
        op, _ = self.store.begin_operation(other["id"], other["active_attempt_id"], "remember", "remember", {"text": "fact"})
        expired = self.store.create_workflow("Unrelated expired question")
        self.store.route(expired["root_task_id"], "planner")
        waiting = self.store.claim(expired["root_task_id"])
        wait = self.store.defer(waiting["id"], waiting["active_attempt_id"], "question", "HUMAN_CLARIFY", {"question": "Confirm?"}, Policy())
        self.store.suspend(waiting["id"], waiting["active_attempt_id"])
        with self.store.tx() as conn:
            conn.execute("UPDATE workflows SET deadline=0 WHERE id=?", (expired["id"],))
            conn.execute("UPDATE waits SET deadline=0 WHERE id=?", (wait["id"],))
        selected = self.sessions.submit(self.sessions.create()["id"], "Run only this")
        before_task = self.store.one("SELECT * FROM tasks WHERE id=?", (other["id"],))
        result = await self.run_workflow(selected, ScriptedProvider([final()]))
        self.assertEqual([w["id"] for w in result], [selected["id"]])
        self.assertEqual(self.store.one("SELECT * FROM tasks WHERE id=?", (other["id"],)), before_task)
        self.assertEqual(self.store.one("SELECT status FROM llm_calls WHERE id=?", (call_id,))["status"], "STARTED")
        self.assertEqual(self.store.one("SELECT status FROM operations WHERE id=?", (op["id"],))["status"], "RUNNING")
        self.assertIsNone(self.store.one("SELECT * FROM events WHERE workflow_id=? AND type='RunConfiguration'", (unrelated["id"],)))
        self.assertEqual(self.store.one("SELECT status FROM workflows WHERE id=?", (expired["id"],))["status"], "OPEN")
        self.assertEqual(self.store.one("SELECT status FROM waits WHERE id=?", (wait["id"],))["status"], "OPEN")

    async def test_empty_scope_is_noop_even_for_expired_workflow_and_wait(self):
        wf = self.store.create_workflow("Expire")
        self.store.route(wf["root_task_id"], "planner")
        root = self.store.claim(wf["root_task_id"])
        wait = self.store.defer(root["id"], root["active_attempt_id"], "ask", "HUMAN_CLARIFY", {"question": "Wait"}, Policy())
        self.store.suspend(root["id"], root["active_attempt_id"])
        with self.store.tx() as conn:
            conn.execute("UPDATE waits SET deadline=0")
            conn.execute("UPDATE workflows SET deadline=0")
        result = await Runtime(self.store, ScriptedProvider([]), self.gateway).run_until_idle(workflow_ids=[])
        self.assertEqual(result, [])
        self.assertEqual(self.store.one("SELECT status FROM waits WHERE id=?", (wait["id"],))["status"], "OPEN")
        self.assertEqual(self.store.one("SELECT status FROM workflows WHERE id=?", (wf["id"],))["status"], "OPEN")


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    setUp = SessionTests.setUp
    tearDown = SessionTests.tearDown

    async def test_stream_callbacks_hide_reasoning_and_finish_after_accounting(self):
        session = self.sessions.create()
        wf = self.sessions.submit(session["id"], "Stream")
        events = []
        class Streaming:
            async def stream_complete(self, messages, tools, *, on_delta, **kwargs):
                on_delta({"kind": "reasoning", "text": "private reasoning"})
                on_delta({"kind": "content", "text": "public"})
                return Completion(final("public"), {"total_tokens": 9})
            async def complete(self, *args, **kwargs):
                raise AssertionError("Must stream when observed")
        def observer(event):
            if event["type"] == "ModelFinished":
                self.assertEqual(self.store.one("SELECT status FROM llm_calls WHERE id=?", (event["call_id"],))["status"], "SUCCEEDED")
            events.append(event)
        await Runtime(self.store, Streaming(), self.gateway, observer=observer).run_until_idle(workflow_ids=[wf["id"]])
        self.assertEqual([e["type"] for e in events], ["ModelStarted", "ModelDelta", "ModelDelta", "ModelFinished"])
        self.assertEqual(events[1]["delta"], {"kind": "reasoning", "chars": 17})
        self.assertNotIn("private reasoning", dumps(events))
        self.assertEqual(events[-1]["usage"], {"total_tokens": 9})

    async def test_observer_failure_does_not_change_durable_result(self):
        wf = self.sessions.submit(self.sessions.create()["id"], "Observer fails")
        def broken(event):
            raise RuntimeError("UI failure")
        await Runtime(self.store, ScriptedProvider([final()]), self.gateway, observer=broken).run_until_idle(workflow_ids=[wf["id"]])
        self.assertEqual(self.store.one("SELECT status FROM workflows WHERE id=?", (wf["id"],))["status"], "COMPLETED")

    async def test_stream_error_keeps_failure_code_without_leaking_raw_error(self):
        wf = self.sessions.submit(self.sessions.create()["id"], "Stream error", policy=Policy(max_retries=0))
        events = []
        class Streaming:
            async def stream_complete(self, *args, on_delta, **kwargs):
                on_delta({"kind": "content", "text": "partial"})
                raise PlatformError("PROVIDER_PROTOCOL", "private provider detail")
        await Runtime(self.store, Streaming(), self.gateway, observer=events.append).run_until_idle(workflow_ids=[wf["id"]])
        self.assertEqual(events[-1]["status"], "FAILED")
        self.assertEqual(events[-1]["error"], "PROVIDER_PROTOCOL")
        self.assertNotIn("private provider detail", dumps(events))

    async def test_cancelled_stream_retains_known_usage_and_unknown_outcome(self):
        wf = self.sessions.submit(self.sessions.create()["id"], "Cancel stream")
        class Streaming:
            async def stream_complete(self, *args, **kwargs):
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError as exc:
                    exc.usage = {"total_tokens": 11}
                    raise
        events = []
        await Runtime(self.store, Streaming(), self.gateway, observer=events.append).run_until_idle(workflow_ids=[wf["id"]], max_seconds=0.1)
        call = self.store.one("SELECT * FROM llm_calls WHERE workflow_id=?", (wf["id"],))
        self.assertEqual(call["status"], "UNKNOWN")
        self.assertEqual(json.loads(call["usage_json"]), {"total_tokens": 11})
        self.assertEqual(events[-1]["status"], "UNKNOWN")
