import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest

from maplatform.comms import CommunicationService
from maplatform.demo import DemoProvider, tool
from maplatform.domain import Policy, PlatformError, dumps
from maplatform.provider import Completion, ScriptedProvider
from maplatform.runtime import Runtime, function
from maplatform.store import Store
from maplatform.tools import MemorySandbox, ToolGateway


def final(summary="accepted", ids=None):
    return {"role": "assistant", "content": dumps({"summary": summary, "artifact_ids": ids or []}), "reasoning_content": ""}


def calling(*calls):
    return {"role": "assistant", "content": "", "reasoning_content": "", "tool_calls": list(calls)}


class GitFixtureGateway(ToolGateway):
    """No Git operation: counts invocations to test the approval boundary only."""
    def __init__(self, *args):
        super().__init__(*args)
        self.executions = 0

    def schemas(self, agent, kind="WORK"):
        return super().schemas(agent, kind) + ([function("git_push", "Test fixture", {"branch": {"type": "string"}}, ("branch",))] if kind == "WORK" else [])

    async def execute(self, task, name, args):
        if name == "git_push":
            self.executions += 1
            return {"fixture": True, "branch": args["branch"]}
        return await super().execute(task, name, args)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.sqlite"
        self.store = Store(self.path)
        self.policy = Policy()
        self.sandbox = MemorySandbox()
        self.gateway = ToolGateway(self.store, self.sandbox, self.policy)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def wf(self, **kwargs):
        return self.store.create_workflow("fixture", policy=self.policy, **kwargs)

    def task(self, wf):
        return self.store.one("SELECT * FROM tasks WHERE id=?", (wf["root_task_id"],))

    async def drive(self, provider, gateway=None):
        await Runtime(self.store, provider, gateway or self.gateway, self.policy).run_until_idle(max_seconds=3)

    async def test_scripted_collaboration_wait_survives_reopen_and_child_artifact_can_be_returned(self):
        wf = self.wf(acceptance=[{"kind": "artifact", "name": "add.py"}])
        await self.drive(DemoProvider())
        self.assertEqual(self.task(wf)["status"], "WAITING")
        self.assertEqual(len(self.store.all("SELECT * FROM bids")), 2)
        pending = self.store.one("SELECT * FROM approvals WHERE tool_name='ask_human'")
        self.store.close()
        self.store = Store(self.path)
        self.gateway = ToolGateway(self.store, MemorySandbox(), self.policy)
        self.store.answer(pending["id"], "answer", "yes")
        await self.drive(DemoProvider())
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        result = json.loads(self.task(wf)["result_json"])
        self.assertEqual(len(result["artifact_ids"]), 1)
        self.assertTrue(self.store.one("SELECT * FROM messages WHERE kind='INFO_REPLY'"))

    async def test_repair_uses_new_response_instead_of_reverifying_old_final(self):
        wf = self.wf(acceptance=[{"kind": "contains", "text": "fixed"}])
        provider = ScriptedProvider([final("wrong"), final("fixed")])
        await self.drive(provider)
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        self.assertEqual(self.task(wf)["repairs"], 1)
        self.assertEqual(len(provider.calls), 2)

    async def test_format_repair_reuses_artifacts_and_cannot_repeat_tools(self):
        wf = self.wf(owner="coder", acceptance=[{"kind": "artifact", "name": "counter.py"},
                     {"kind": "command", "argv": ["python", "-m", "unittest"]}])
        acceptance_runs = []
        async def fixture_run(task, argv, timeout):
            acceptance_runs.append(task["id"])
            return {"exit_code": 0, "stdout": "fixture", "stderr": ""}
        self.sandbox.run = fixture_run
        repair_calls = 0
        artifact_id = None
        def script(messages, tools, *, task, purpose):
            nonlocal repair_calls, artifact_id
            replies = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m["role"] == "tool"}
            if purpose == "FINAL_REPAIR":
                repair_calls += 1
                self.assertEqual(tools, [])
                feedback = next(m["content"] for m in messages if m["role"] == "user" and m["content"].startswith("Verification feedback:"))
                self.assertIn("FINAL_FORMAT_INVALID", feedback)
                self.assertIn(artifact_id, feedback)
                self.assertNotIn("Missing required artifact", feedback)
                if repair_calls == 1:
                    # Even a provider ignoring the empty tool list cannot redo IO.
                    return calling(tool("forbidden-repeat", "write_file", {"path": "counter.py", "content": "overwritten"}))
                self.assertEqual(replies["forbidden-repeat"]["error"]["code"], "TOOL_DENIED")
                return final("The existing work is complete", [artifact_id])
            if "write" not in replies:
                return calling(tool("write", "write_file", {"path": "counter.py", "content": "original"}))
            if "publish" not in replies:
                return calling(tool("publish", "publish_artifact", {"path": "counter.py", "name": "counter.py", "grantees": ["planner"]}))
            artifact_id = replies["publish"]["artifact_id"]
            # Real-run regression: valid-looking JSON after a prose preamble.
            return {"role": "assistant", "content": "All work is complete.\n" + final("Done", [artifact_id])["content"]}
        provider = ScriptedProvider(script)
        await self.drive(provider)
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        self.assertEqual(self.task(wf)["repairs"], 1)
        self.assertEqual(await self.sandbox.read(self.task(wf), "counter.py"), "original")
        self.assertEqual(len(self.store.all("SELECT * FROM operations")), 2)
        self.assertEqual(acceptance_runs, [wf["root_task_id"]])
        self.assertEqual(json.loads(self.task(wf)["result_json"])["artifact_ids"], [artifact_id])

    async def test_format_repair_survives_restart_without_tools(self):
        wf = self.wf(owner="coder")
        self.store.route(wf["root_task_id"], "coder")
        runtime = Runtime(self.store, ScriptedProvider([{"role": "assistant", "content": "Done. " + final()["content"]}]), self.gateway)
        await runtime.run_task(self.task(wf))
        await runtime.verify_task(self.task(wf))
        self.assertEqual(self.task(wf)["status"], "READY")
        self.store.close()
        self.store = Store(self.path)
        self.gateway = ToolGateway(self.store, self.sandbox, self.policy)
        provider = ScriptedProvider([final()])
        await self.drive(provider)
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        self.assertEqual(provider.calls[0]["tools"], [])
        self.assertEqual(provider.calls[0]["purpose"], "FINAL_REPAIR")

    async def test_interrupted_format_repair_remains_tool_free_after_recovery(self):
        wf = self.wf(owner="coder")
        entered = asyncio.Event()
        async def script(messages, tools, *, task, purpose):
            if purpose == "FINAL_REPAIR":
                self.assertEqual(tools, [])
                entered.set()
                await asyncio.Future()
            return {"role": "assistant", "content": "Done. " + final()["content"]}
        runtime = Runtime(self.store, ScriptedProvider(script), self.gateway)
        running = asyncio.create_task(runtime.run_until_idle())
        await asyncio.wait_for(entered.wait(), 2)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.store.close()
        self.store = Store(self.path)
        self.gateway = ToolGateway(self.store, self.sandbox, self.policy)
        provider = ScriptedProvider([final()])
        await self.drive(provider)
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        self.assertEqual(provider.calls[0]["task"]["error"], "PROCESS_RECOVERY")
        self.assertEqual(provider.calls[0]["tools"], [])
        self.assertEqual(provider.calls[0]["purpose"], "FINAL_REPAIR")

    async def test_transient_format_repair_retry_does_not_restore_tools(self):
        wf = self.wf(owner="coder")
        attempts = 0
        def script(messages, tools, *, task, purpose):
            nonlocal attempts
            if purpose != "FINAL_REPAIR":
                return {"role": "assistant", "content": "Done. " + final()["content"]}
            attempts += 1
            self.assertEqual(tools, [])
            if attempts == 1:
                raise PlatformError("PROVIDER_TRANSIENT", "fixture network interruption")
            self.assertEqual(task["error"], "PROVIDER_TRANSIENT")
            return final()
        await self.drive(ScriptedProvider(script))
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        self.assertEqual(self.task(wf)["retries"], 1)
        self.assertEqual(attempts, 2)

    async def test_malformed_final_is_never_extracted_or_accepted(self):
        invalid = [
            'Done. {"summary":"complete","artifact_ids":[]}',
            '```json\n{"summary":"complete","artifact_ids":[]}\n```',
            r'{"summary":"quoted \\"word\\"","artifact_ids":[]}',
            '{"summary":"first","summary":"second","artifact_ids":[]}',
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                wf = self.wf(acceptance=[{"kind": "command", "argv": ["python", "-m", "unittest"]}])
                provider = ScriptedProvider([{"role": "assistant", "content": raw}] * 2)
                await self.drive(provider)
                self.assertEqual(self.task(wf)["status"], "FAILED")
                self.assertEqual(self.task(wf)["error"], "REPAIRS_EXHAUSTED")
                self.assertEqual(provider.calls[1]["tools"], [])
                self.assertIsNone(self.store.one("SELECT * FROM events WHERE workflow_id=? AND type='AcceptanceCommand'", (wf["id"],)))

    async def test_actual_acceptance_failure_keeps_tools_for_code_repair(self):
        wf = self.wf(owner="coder", acceptance=[{"kind": "command", "argv": ["python", "-m", "unittest"]}])
        acceptance_runs = []
        async def fixture_run(task, argv, timeout):
            content = await self.sandbox.read(task, "counter.py")
            acceptance_runs.append(content)
            return {"exit_code": 0 if content == "fixed" else 1, "stdout": "fixture", "stderr": ""}
        self.sandbox.run = fixture_run
        provider = ScriptedProvider([
            calling(tool("initial", "write_file", {"path": "counter.py", "content": "broken"})), final("ready"),
            calling(tool("fix", "write_file", {"path": "counter.py", "content": "fixed"})), final("fixed"),
        ])
        await self.drive(provider)
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        self.assertEqual(acceptance_runs, ["broken", "fixed"])
        self.assertEqual(provider.calls[2]["purpose"], "AGENT")
        self.assertIn("write_file", [t["function"]["name"] for t in provider.calls[2]["tools"]])

    async def test_acceptance_repair_after_format_repair_restores_tools(self):
        self.policy = Policy(max_repairs=2)
        wf = self.wf(owner="coder", acceptance=[{"kind": "command", "argv": ["python", "-m", "unittest"]}])
        checks = []
        async def fixture_run(task, argv, timeout):
            content = await self.sandbox.read(task, "counter.py")
            checks.append(content)
            return {"exit_code": int(content != "fixed"), "stdout": "fixture", "stderr": ""}
        self.sandbox.run = fixture_run
        provider = ScriptedProvider([
            calling(tool("initial", "write_file", {"path": "counter.py", "content": "broken"})),
            {"role": "assistant", "content": "Done. " + final()["content"]},
            final("formatted only"),
            calling(tool("fix", "write_file", {"path": "counter.py", "content": "fixed"})), final("fixed"),
        ])
        await self.drive(provider)
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        self.assertEqual(checks, ["broken", "fixed"])
        self.assertEqual(provider.calls[2]["purpose"], "FINAL_REPAIR")
        self.assertEqual(provider.calls[2]["tools"], [])
        self.assertEqual(provider.calls[3]["purpose"], "AGENT")
        self.assertIn("write_file", [t["function"]["name"] for t in provider.calls[3]["tools"]])

    async def test_final_message_with_explicit_null_tool_calls_is_accepted(self):
        wf = self.wf()
        message = final()
        message["tool_calls"] = None
        await self.drive(ScriptedProvider([message]))
        self.assertEqual(self.task(wf)["status"], "COMPLETED")

    async def test_fenced_old_worker_cannot_fail_new_attempt(self):
        wf = self.wf()
        self.store.route(wf["root_task_id"], "planner")
        replacement = None
        async def interrupted(messages, tools, **kwargs):
            nonlocal replacement
            self.store.recover(self.policy)
            replacement = self.store.claim(wf["root_task_id"])
            raise PlatformError("STALE_ATTEMPT", "Old worker was fenced")
        runtime = Runtime(self.store, ScriptedProvider(interrupted), self.gateway)
        await runtime.run_task(self.task(wf))
        current = self.task(wf)
        self.assertEqual(current["status"], "RUNNING")
        self.assertEqual(current["active_attempt_id"], replacement["active_attempt_id"])

    async def test_recovery_discovers_created_and_verifying_tasks(self):
        created = self.wf()
        verifying = self.wf()
        self.store.route(verifying["root_task_id"], "planner")
        task = self.store.claim(verifying["root_task_id"])
        self.store.verify(task["id"], task["active_attempt_id"], {"summary": "already generated", "artifact_ids": []})
        provider = ScriptedProvider([final()])
        await self.drive(provider)
        self.assertEqual(self.task(created)["status"], "COMPLETED")
        self.assertEqual(self.task(verifying)["status"], "COMPLETED")
        self.assertEqual(len(provider.calls), 1)

    async def test_routing_filters_artifact_grants_before_spending_bid_calls(self):
        wf = self.wf(owner="coder")
        self.store.route(wf["root_task_id"], "coder")
        parent = self.store.claim(wf["root_task_id"])
        await self.sandbox.write(parent, "source.txt", "Review this source")
        artifact = await self.gateway.execute(parent, "publish_artifact", {"path": "source.txt", "name": "source.txt", "grantees": ["reviewer"]})
        wait = self.store.defer(parent["id"], parent["active_attempt_id"], "delegate", "CHILD_TASK", {
            "goal": "Review source", "required_capabilities": ["writing"], "input_artifact_ids": [artifact["artifact_id"]], "acceptance": []}, self.policy)
        child = self.store.one("SELECT * FROM tasks WHERE id=?", (wait["ref_id"],))
        provider = ScriptedProvider([])
        await Runtime(self.store, provider, self.gateway).route(child)
        assigned = self.store.one("SELECT * FROM tasks WHERE id=?", (child["id"],))
        self.assertEqual(assigned["owner"], "reviewer")
        self.assertEqual(assigned["status"], "READY")
        self.assertEqual(provider.calls, [])
        with self.store.tx() as conn:
            conn.execute("UPDATE artifacts SET grantees_json=? WHERE id=?", (dumps(["planner"]), artifact["artifact_id"]))
        wait = self.store.defer(parent["id"], parent["active_attempt_id"], "unroutable", "CHILD_TASK", {
            "goal": "No authorized writer", "required_capabilities": ["writing"], "input_artifact_ids": [artifact["artifact_id"]], "acceptance": []}, self.policy)
        child = self.store.one("SELECT * FROM tasks WHERE id=?", (wait["ref_id"],))
        with self.assertRaises(PlatformError) as caught:
            await Runtime(self.store, provider, self.gateway).route(child)
        self.assertEqual(caught.exception.code, "NO_CANDIDATE")
        self.assertEqual(provider.calls, [])

    async def test_completed_tool_result_is_replayed_without_new_effect(self):
        wf = self.wf()
        self.store.route(wf["root_task_id"], "planner")
        task = self.store.claim(wf["root_task_id"])
        messages = Runtime(self.store, None, self.gateway).initial_messages(task)
        call = tool("remember-once", "remember", {"text": "one durable fact"})
        messages.append(calling(call))
        self.store.save_snapshot(task["id"], task["active_attempt_id"], messages)
        op, _ = self.store.begin_operation(task["id"], task["active_attempt_id"], call["id"], "remember", {"text": "one durable fact"})
        result = await self.gateway.execute(task, "remember", {"text": "one durable fact"})
        self.store.finish_operation(op["id"], result)
        provider = ScriptedProvider([final()])
        await self.drive(provider)
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        self.assertEqual(len(self.store.all("SELECT * FROM memories WHERE text='one durable fact'")), 1)
        self.assertEqual(provider.calls[0]["messages"][-1]["role"], "tool")

    async def test_unknown_operation_is_observation_and_never_reexecuted(self):
        wf = self.wf()
        self.store.route(wf["root_task_id"], "planner")
        task = self.store.claim(wf["root_task_id"])
        call = tool("unknown", "remember", {"text": "not actually applied"})
        messages = Runtime(self.store, None, self.gateway).initial_messages(task) + [calling(call)]
        self.store.save_snapshot(task["id"], task["active_attempt_id"], messages)
        self.store.begin_operation(task["id"], task["active_attempt_id"], call["id"], "remember", {"text": "not actually applied"})
        provider = ScriptedProvider([final("Unknown prior operation was reported, not replayed")])
        await self.drive(provider)
        self.assertEqual(len(self.store.all("SELECT * FROM memories WHERE text='not actually applied'")), 0)
        observation = json.loads(provider.calls[0]["messages"][-1]["content"])
        self.assertEqual(observation["error"]["code"], "TOOL_OUTCOME_UNKNOWN")

    async def test_denied_approval_returns_tool_error_and_continues(self):
        wf = self.wf(owner="coder")
        gateway = GitFixtureGateway(self.store, self.sandbox, self.policy)
        provider = ScriptedProvider([calling(tool("push", "git_push", {"branch": "demo"})), final("Human denied the fixture operation")])
        await self.drive(provider, gateway)
        approval = self.store.one("SELECT * FROM approvals")
        self.assertEqual(self.task(wf)["status"], "WAITING")
        self.store.answer(approval["id"], "deny")
        await self.drive(provider, gateway)
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        self.assertEqual(gateway.executions, 0)
        self.assertEqual(json.loads(provider.calls[-1]["messages"][-1]["content"])["error"]["code"], "TOOL_DENIED")

    async def test_approved_exact_args_execute_once(self):
        wf = self.wf(owner="coder")
        gateway = GitFixtureGateway(self.store, self.sandbox, self.policy)
        provider = ScriptedProvider([calling(tool("push", "git_push", {"branch": "demo"})), final()])
        await self.drive(provider, gateway)
        approval = self.store.one("SELECT * FROM approvals")
        self.store.answer(approval["id"], "approve")
        await self.drive(provider, gateway)
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        self.assertEqual(gateway.executions, 1)
        await self.drive(provider, gateway)
        self.assertEqual(gateway.executions, 1)

    async def test_uncertain_git_push_stays_unknown_for_remote_reconciliation(self):
        class UncertainGit(GitFixtureGateway):
            async def execute(self, task, name, args):
                if name == "git_push":
                    self.executions += 1
                    raise PlatformError("EFFECT_UNKNOWN", "Push confirmation lost")
                return await super().execute(task, name, args)
        wf = self.wf(owner="coder")
        gateway = UncertainGit(self.store, self.sandbox, self.policy)
        provider = ScriptedProvider([calling(tool("push", "git_push", {"branch": "demo"})), final("Remote outcome requires reconciliation")])
        await self.drive(provider, gateway)
        approval = self.store.one("SELECT * FROM approvals")
        self.store.answer(approval["id"], "approve")
        await self.drive(provider, gateway)
        self.assertEqual(self.store.one("SELECT * FROM operations WHERE tool_name='git_push'")["status"], "UNKNOWN")
        self.assertEqual(gateway.executions, 1)
        await self.drive(provider, gateway)
        self.assertEqual(gateway.executions, 1)

    async def test_reply_cannot_start_agent_chain(self):
        comms = CommunicationService(self.store)
        wf = comms.create("question", recipients=["reviewer"], policy=self.policy)["workflows"][0]
        provider = ScriptedProvider([calling(tool("chain", "ask_agent", {"target_agent": "coder", "question": "more?"})), final()])
        await self.drive(provider)
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        self.assertEqual(len(self.store.all("SELECT * FROM tasks")), 1)
        self.assertEqual(json.loads(provider.calls[-1]["messages"][-1]["content"])["error"]["code"], "TOOL_DENIED")

    async def test_spoofed_memory_actor_rejected(self):
        wf = self.wf()
        provider = ScriptedProvider([calling(tool("spoof", "remember", {"text": "secret", "agent_id": "reviewer"})), final()])
        await self.drive(provider)
        self.assertEqual(len(self.store.all("SELECT * FROM memories WHERE text='secret'")), 0)

    async def test_deadline_during_verification_does_not_emit_successful_chat_reply(self):
        service = CommunicationService(self.store)
        wf = service.create("question", recipients=["reviewer"], policy=self.policy)["workflows"][0]
        self.store.route(wf["root_task_id"], "reviewer")
        task = self.store.claim(wf["root_task_id"])
        self.store.verify(task["id"], task["active_attempt_id"], {"summary": "late", "artifact_ids": []})
        with self.store.tx() as conn:
            conn.execute("UPDATE workflows SET deadline=? WHERE id=?", (time.time() - 1, wf["id"]))
        await Runtime(self.store, None, self.gateway).verify_task(self.task(wf))
        self.assertEqual(self.task(wf)["status"], "TIMED_OUT")
        self.assertFalse(self.store.one("SELECT * FROM messages WHERE kind='REPLY'"))

    async def test_call_budget_counts_every_attempt_and_stops_loop(self):
        self.policy = Policy(max_calls=2)
        wf = self.wf()
        counter = 0
        def looping(messages, tools, **kwargs):
            nonlocal counter
            counter += 1
            return calling(tool(f"memory-{counter}", "search_memory", {"query": "x"}))
        await self.drive(ScriptedProvider(looping))
        self.assertEqual(self.task(wf)["status"], "FAILED")
        self.assertEqual(len(self.store.all("SELECT * FROM llm_calls")), 2)

    async def test_saved_workflow_output_caps_are_passed_per_call_without_mutating_provider(self):
        small = self.store.create_workflow("small", policy=Policy(max_output_tokens=128))
        large = self.store.create_workflow("large", policy=Policy(max_output_tokens=1024))
        provider = ScriptedProvider([final(), final()])
        provider.max_output_tokens = 2048
        # Resuming with a different startup policy must retain each saved cap.
        runtime = Runtime(self.store, provider, self.gateway, Policy(max_output_tokens=32))
        await runtime.run_until_idle(max_seconds=3)
        limits = {call["task"]["workflow_id"]: call["max_output_tokens"] for call in provider.calls}
        self.assertEqual(limits, {small["id"]: 128, large["id"]: 1024})
        self.assertEqual(provider.max_output_tokens, 2048)
        self.assertEqual(self.task(small)["status"], "COMPLETED")
        self.assertEqual(self.task(large)["status"], "COMPLETED")

    async def test_context_budget_counts_tool_schemas_before_reserving_a_call(self):
        wf = self.store.create_workflow("context", policy=Policy(max_context_chars=200))
        self.store.route(wf["root_task_id"], "planner")
        task = self.store.claim(wf["root_task_id"])
        provider = ScriptedProvider([final()])
        runtime = Runtime(self.store, provider, self.gateway)
        with self.assertRaises(PlatformError) as caught:
            await runtime.call_model(task, [{"role": "user", "content": "short"}],
                                     [function("large", "x" * 300, {})])
        self.assertEqual(caught.exception.code, "CONTEXT_LIMIT")
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.store.all("SELECT * FROM llm_calls"), [])

    async def test_cancelled_model_call_has_unknown_outcome_and_remains_budgeted(self):
        wf = self.wf()
        self.store.route(wf["root_task_id"], "planner")
        task = self.store.claim(wf["root_task_id"])
        entered = asyncio.Event()

        async def blocked(messages, tools, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        provider = ScriptedProvider(blocked)
        runtime = Runtime(self.store, provider, self.gateway)
        running = asyncio.create_task(runtime.call_model(task, [{"role": "user", "content": "wait"}], []))
        await asyncio.wait_for(entered.wait(), timeout=1)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        call = self.store.one("SELECT * FROM llm_calls")
        self.assertEqual(call["status"], "UNKNOWN")
        self.assertEqual(call["error"], "CANCELLED_OUTCOME_UNKNOWN")
        self.assertEqual(len(self.store.all("SELECT * FROM llm_calls")), 1)
        event = self.store.one("SELECT * FROM events WHERE type='ModelCallFinished'")
        self.assertEqual(json.loads(event["payload_json"])["status"], "UNKNOWN")

    async def test_failed_model_call_preserves_known_token_usage(self):
        wf = self.wf()

        def truncated(messages, tools, **kwargs):
            error = PlatformError("PROVIDER_TRUNCATED", "Output limit reached")
            error.usage = {"prompt_tokens": 15, "completion_tokens": 128, "total_tokens": 143}
            raise error

        await self.drive(ScriptedProvider(truncated))
        call = self.store.one("SELECT * FROM llm_calls")
        self.assertEqual(call["status"], "FAILED")
        self.assertEqual(json.loads(call["usage_json"])["completion_tokens"], 128)
        self.assertEqual(self.task(wf)["error"], "PROVIDER_TRUNCATED")

    async def test_rejected_notification_is_cached_as_failure_after_recovery(self):
        wf = self.store.create_workflow("notification", policy=Policy(max_a2a=0))
        self.store.route(wf["root_task_id"], "planner")
        task = self.store.claim(wf["root_task_id"])
        runtime = Runtime(self.store, None, self.gateway)
        call = tool("notify", "notify_agent", {"target_agent": "reviewer", "content": "notice"})
        first = await runtime.invoke(task, call)
        operation = self.store.one("SELECT * FROM operations")
        self.assertEqual(first["error"]["code"], "A2A_LIMIT")
        self.assertEqual(operation["status"], "FAILED")
        self.store.recover(self.policy)
        resumed = self.store.claim(task["id"])
        self.assertEqual(await runtime.invoke(resumed, call), first)
        self.assertEqual(self.store.all("SELECT * FROM messages"), [])

    async def test_group_history_shared_only_with_members_and_private_text_excluded(self):
        service = CommunicationService(self.store)
        result = service.create("group-visible", recipients=["coder", "reviewer"], group=True, policy=self.policy)
        cids = self.store.all("SELECT DISTINCT conversation_id FROM messages")
        self.assertEqual(len(cids), 1)
        cid = cids[0]["conversation_id"]
        self.assertEqual(len(service.history(cid, principal="coder")), 2)
        with self.assertRaises(PlatformError):
            service.history(cid, principal="researcher")
        private = service.create("private-secret", recipients=["researcher"], policy=self.policy)
        task = self.task(result["workflows"][0])
        context = dumps(Runtime(self.store, None, self.gateway).initial_messages(task))
        self.assertIn("group-visible", context)
        self.assertNotIn("private-secret", context)

    async def test_remote_transient_retry_is_owned_by_task_service(self):
        wf = self.wf()
        calls = 0
        def flaky(messages, tools, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PlatformError("PROVIDER_TRANSIENT", "fixture")
            return final()
        await self.drive(ScriptedProvider(flaky))
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        self.assertEqual(self.task(wf)["retries"], 1)
        self.assertEqual(len(self.store.all("SELECT * FROM llm_calls")), 2)


if __name__ == "__main__":
    unittest.main()
