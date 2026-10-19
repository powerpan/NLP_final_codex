"""State and persistence regression checks; these make no live-provider claims."""
import json
from pathlib import Path
import tempfile
import unittest

from maplatform.domain import PlatformError, Policy, digest
from maplatform.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite"
        self.store = Store(self.path)
        self.policy = Policy()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def workflow(self, **kwargs):
        return self.store.create_workflow("Test goal", policy=kwargs.pop("policy", self.policy), **kwargs)

    def start(self, wf=None, owner="planner"):
        wf = wf or self.workflow(owner=owner)
        self.store.route(wf["root_task_id"], owner)
        return self.store.claim(wf["root_task_id"])

    def child(self, parent, tool_id="delegate", **extra):
        args = {"goal": "Write code", "required_capabilities": ["coding"], "target_agent": "coder", **extra}
        return self.store.defer(parent["id"], parent["active_attempt_id"], tool_id, "CHILD_TASK", args, self.policy)

    def assertCode(self, code, fn, *args, **kwargs):
        with self.assertRaises(PlatformError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_creation_route_and_atomic_claim(self):
        wf = self.workflow()
        root = self.store.one("SELECT * FROM tasks WHERE id=?", (wf["root_task_id"],))
        self.assertEqual(root["status"], "CREATED")
        self.assertEqual(root["next_kind"], "FIRST")
        self.assertEqual(wf["status"], "OPEN")
        self.assertIsNone(self.store.claim(root["id"]))
        self.store.route(root["id"], "planner")
        task = self.store.claim(root["id"])
        self.assertEqual(task["status"], "RUNNING")
        self.assertIsNone(self.store.claim(root["id"]))
        self.assertCode("STALE_ATTEMPT", self.store.save_snapshot, task["id"], "forged", [])
        self.store.save_snapshot(task["id"], task["active_attempt_id"], [{"role": "user", "content": "hello"}])
        self.assertEqual(self.store.snapshot(task["id"])[0]["content"], "hello")

    def test_web_budget_counts_failed_and_unknown_reservations_without_charging_replay(self):
        wf = self.workflow(policy=Policy(max_web_requests=2))
        root = self.start(wf)
        first, execute = self.store.begin_operation(root["id"], root["active_attempt_id"], "search", "search_web", {"query": "one"})
        self.assertTrue(execute)
        self.store.finish_operation(first["id"], {"error": {"code": "WEB_HTTP"}}, error="WEB_HTTP")
        second, execute = self.store.begin_operation(root["id"], root["active_attempt_id"], "fetch", "fetch_web", {"url": "https://example.com"})
        self.assertTrue(execute)
        self.store.finish_operation(second["id"], {"error": {"code": "OUTCOME_UNKNOWN"}}, status="UNKNOWN")
        replay, execute = self.store.begin_operation(root["id"], root["active_attempt_id"], "search", "search_web", {"query": "one"})
        self.assertEqual(replay["id"], first["id"])
        self.assertFalse(execute)
        self.assertCode("WEB_BUDGET_EXHAUSTED", self.store.begin_operation, root["id"], root["active_attempt_id"], "more", "search_web", {"query": "two"})
        self.assertEqual(len(self.store.all("SELECT * FROM operations")), 2)

    def test_successful_web_cache_is_shared_within_workflow_without_new_budget(self):
        wf = self.workflow(owner="researcher", policy=Policy(max_web_requests=1))
        parent = self.start(wf, owner="researcher")
        first, _ = self.store.begin_operation(parent["id"], parent["active_attempt_id"], "first", "search_web", {"query": "same"}, web_cache_key="provider-a-request")
        self.store.finish_operation(first["id"], {"results": []})
        wait = self.store.defer(parent["id"], parent["active_attempt_id"], "child", "CHILD_TASK", {
            "goal": "Continue research", "target_agent": "researcher", "required_capabilities": ["research"], "acceptance": []}, self.policy)
        self.store.route(wait["ref_id"], "researcher")
        child = self.store.claim(wait["ref_id"])
        reused, execute = self.store.begin_operation(child["id"], child["active_attempt_id"], "reuse", "search_web", {"query": "same", "limit": 5}, web_cache_key="provider-a-request")
        self.assertFalse(execute)
        self.assertEqual(reused["id"], first["id"])
        replay, execute = self.store.begin_operation(child["id"], child["active_attempt_id"], "reuse", "search_web", {"query": "same", "limit": 5}, web_cache_key="provider-a-request")
        self.assertFalse(execute)
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM operations")["n"], 1)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM events WHERE type='OperationReused'")["n"], 1)
        self.assertCode("WEB_BUDGET_EXHAUSTED", self.store.begin_operation, child["id"], child["active_attempt_id"], "new-provider", "search_web", {"query": "same"}, web_cache_key="provider-b-request")
        other = self.start(self.workflow(owner="researcher"), owner="researcher")
        separate, execute = self.store.begin_operation(other["id"], other["active_attempt_id"], "separate", "search_web", {"query": "same"}, web_cache_key="provider-a-request")
        self.assertTrue(execute)
        self.assertNotEqual(separate["id"], first["id"])

    def test_web_cache_never_reuses_failed_unknown_or_inflight_operations(self):
        for status in ("FAILED", "UNKNOWN", "RUNNING"):
            with self.subTest(status=status):
                task = self.start(self.workflow(owner="researcher"), owner="researcher")
                first, _ = self.store.begin_operation(task["id"], task["active_attempt_id"], "first", "search_web", {"query": "same"}, web_cache_key="provider-request")
                if status != "RUNNING":
                    self.store.finish_operation(first["id"], {"error": {"code": "fixture"}}, error="fixture", status=status)
                second, execute = self.store.begin_operation(task["id"], task["active_attempt_id"], "new-call", "search_web", {"query": "same"}, web_cache_key="provider-request")
                self.assertTrue(execute)
                self.assertNotEqual(first["id"], second["id"])
                old, execute = self.store.begin_operation(task["id"], task["active_attempt_id"], "first", "search_web", {"query": "same"}, web_cache_key="provider-request")
                self.assertFalse(execute)
                self.assertEqual(old["id"], first["id"])

    def test_empty_successful_fetch_is_not_cached_and_nonweb_cache_is_rejected(self):
        task = self.start(self.workflow(owner="researcher"), owner="researcher")
        first, _ = self.store.begin_operation(task["id"], task["active_attempt_id"], "blank", "fetch_web", {"url": "https://example.com"}, web_cache_key="fetch-key")
        self.store.finish_operation(first["id"], {"page": {"content": " \n\t\u2003"}})
        second, execute = self.store.begin_operation(task["id"], task["active_attempt_id"], "again", "fetch_web", {"url": "https://example.com"}, web_cache_key="fetch-key")
        self.assertTrue(execute)
        self.assertNotEqual(first["id"], second["id"])
        self.assertCode("INVALID_ARGUMENT", self.store.begin_operation, task["id"], task["active_attempt_id"], "write", "write_file", {}, web_cache_key="not-public-web")

    def test_route_enforces_registry_capability(self):
        wf = self.workflow(owner=None, required=["coding"])
        self.assertCode("CAPABILITY_MISMATCH", self.store.route, wf["root_task_id"], "planner")
        self.assertCode("UNKNOWN_AGENT", self.store.route, wf["root_task_id"], "invented")
        self.store.route(wf["root_task_id"], "coder")

    def test_child_wait_atomic_idempotent_and_resumes_parent_on_failure(self):
        parent = self.start()
        wait = self.child(parent)
        same = self.child(parent)
        self.assertEqual(wait["id"], same["id"])
        self.assertEqual(len(self.store.all("SELECT * FROM tasks")), 2)
        self.assertCode("IDEMPOTENCY_CONFLICT", self.child, parent, goal="Different")
        suspended = self.store.suspend(parent["id"], parent["active_attempt_id"])
        self.assertEqual(suspended["status"], "WAITING")
        self.store.fail(wait["ref_id"], "No candidate")
        resumed = self.store.one("SELECT * FROM tasks WHERE id=?", (parent["id"],))
        self.assertEqual(resumed["status"], "READY")
        self.assertEqual(resumed["next_kind"], "RESUME")
        self.assertEqual(resumed["resumes"], 1)
        self.assertEqual(json.loads(self.store.one("SELECT * FROM waits WHERE id=?", (wait["id"],))["result_json"])["status"], "FAILED")
        self.store.resolve_wait(wait["id"], {"different": "ignored"})
        self.assertEqual(self.store.one("SELECT resumes FROM tasks WHERE id=?", (parent["id"],))["resumes"], 1)

    def test_multiple_waits_and_resolve_before_suspend(self):
        parent = self.start()
        one = self.store.defer(parent["id"], parent["active_attempt_id"], "a", "HUMAN_CLARIFY", {"question": "A?"}, self.policy)
        two = self.store.defer(parent["id"], parent["active_attempt_id"], "b", "HUMAN_CLARIFY", {"question": "B?"}, self.policy)
        self.store.answer(one["ref_id"], "ANSWER", "First")
        self.assertEqual(self.store.suspend(parent["id"], parent["active_attempt_id"])["status"], "WAITING")
        self.store.answer(two["ref_id"], "ANSWER", "Second")
        self.assertEqual(self.store.one("SELECT status FROM tasks WHERE id=?", (parent["id"],))["status"], "READY")

    def test_reply_has_private_membership_and_cannot_chain(self):
        parent = self.start()
        wait = self.store.defer(parent["id"], parent["active_attempt_id"], "ask", "AGENT_REPLY", {"target_agent": "reviewer", "question": "Review?"}, self.policy)
        reply = self.store.claim(wait["ref_id"])
        conversation = self.store.one("SELECT * FROM conversations WHERE id=?", (reply["conversation_id"],))
        self.assertEqual(conversation["visibility"], "PRIVATE")
        self.assertEqual(set(json.loads(conversation["members_json"])), {"planner", "reviewer"})
        rejected = self.store.defer(reply["id"], reply["active_attempt_id"], "chain", "AGENT_REPLY", {"target_agent": "coder", "question": "Chain?"}, self.policy)
        self.assertEqual(rejected["status"], "RESOLVED")
        self.assertEqual(json.loads(rejected["result_json"])["error"]["code"], "REPLY_TOOL_FORBIDDEN")
        self.store.suspend(parent["id"], parent["active_attempt_id"])
        self.store.verify(reply["id"], reply["active_attempt_id"], {"answer": "Looks sound"})
        self.store.complete(reply["id"])
        message = self.store.one("SELECT * FROM messages WHERE kind='INFO_REPLY'")
        self.assertEqual(message["content"], "Looks sound")
        self.assertEqual(message["sender"], "reviewer")
        self.assertEqual(message["recipient"], "planner")
        self.assertEqual(message["visibility"], "PRIVATE")
        self.assertEqual(self.store.one("SELECT status FROM tasks WHERE id=?", (parent["id"],))["status"], "READY")

    def test_model_budget_counts_bid_failure_recovery_and_reply_calls(self):
        wf = self.workflow(policy=Policy(max_calls=2))
        first = self.store.reserve_call(wf["root_task_id"], None, "BID", "hash1")
        self.store.finish_call(first, error="provider rejected")
        task = self.start(wf)
        second = self.store.reserve_call(task["id"], task["active_attempt_id"], "AGENT", "hash2")
        self.store.recover(self.policy)
        self.assertEqual(self.store.one("SELECT status FROM llm_calls WHERE id=?", (second,))["status"], "UNKNOWN")
        retried = self.store.claim(task["id"])
        self.assertCode("BUDGET_EXCEEDED", self.store.reserve_call, task["id"], retried["active_attempt_id"], "AGENT", "hash3")
        self.assertCode("STALE_ATTEMPT", self.store.reserve_call, task["id"], None, "AGENT", "hash3")
        fresh = self.start(self.workflow(policy=Policy(max_reply_calls=1)))
        wait = self.store.defer(fresh["id"], fresh["active_attempt_id"], "ask", "AGENT_REPLY", {"target_agent": "reviewer", "question": "Q?"}, self.policy)
        reply = self.store.claim(wait["ref_id"])
        self.store.reserve_call(reply["id"], reply["active_attempt_id"], "AGENT", "reply1")
        self.assertCode("BUDGET_EXCEEDED", self.store.reserve_call, reply["id"], reply["active_attempt_id"], "AGENT", "reply2")

    def test_deferral_limits_are_durable_rejections_without_child_leak(self):
        policy = Policy(max_tasks=1, max_resumes=1)
        parent = self.start(self.workflow(policy=policy))
        rejected = self.child(parent)
        self.assertEqual(rejected["status"], "RESOLVED")
        self.assertEqual(json.loads(rejected["result_json"])["error"]["code"], "TASK_BUDGET_EXCEEDED")
        self.assertEqual(len(self.store.all("SELECT * FROM tasks")), 1)
        parent = self.store.suspend(parent["id"], parent["active_attempt_id"])
        self.assertEqual(parent["status"], "READY")
        parent = self.store.claim(parent["id"])
        self.child(parent, "again")
        parent = self.store.suspend(parent["id"], parent["active_attempt_id"])
        self.assertEqual(parent["status"], "FAILED")
        self.assertEqual(parent["error"], "RESUMES_EXHAUSTED")
        self.assertEqual(self.store.one("SELECT status FROM workflows WHERE id=?", (parent["workflow_id"],))["status"], "FAILED")

    def test_depth_child_and_a2a_limits(self):
        for policy, expected in ((Policy(max_depth=0), "DEPTH_EXCEEDED"), (Policy(max_children=0), "CHILDREN_EXCEEDED")):
            parent = self.start(self.workflow(policy=policy))
            wait = self.child(parent)
            self.assertEqual(json.loads(wait["result_json"])["error"]["code"], expected)
        parent = self.start(self.workflow(policy=Policy(max_a2a=0)))
        wait = self.store.defer(parent["id"], parent["active_attempt_id"], "ask", "AGENT_REPLY", {"target_agent": "reviewer", "question": "Q?"}, self.policy)
        self.assertEqual(json.loads(wait["result_json"])["error"]["code"], "A2A_BUDGET_EXCEEDED")
        self.assertEqual(self.store.all("SELECT * FROM messages WHERE workflow_id=?", (parent["workflow_id"],)), [])

    def test_approval_owner_hash_idempotence_and_denial(self):
        parent = self.start(owner="coder")
        args = {"tool_name": "git_push", "arguments": {"remote": "origin", "branch": "work"}}
        wait = self.store.defer(parent["id"], parent["active_attempt_id"], "push", "HUMAN_APPROVAL", args, self.policy)
        self.assertCode("APPROVAL_FORBIDDEN", self.store.answer, wait["ref_id"], "APPROVED", user_id="other")
        with self.store.tx() as conn:
            conn.execute("UPDATE approvals SET args_json=? WHERE id=?", ('{"branch":"forged"}', wait["ref_id"]))
        self.assertCode("APPROVAL_HASH_MISMATCH", self.store.answer, wait["ref_id"], "APPROVED")
        with self.store.tx() as conn:
            conn.execute("UPDATE approvals SET args_json=? WHERE id=?", (json.dumps(args["arguments"]), wait["ref_id"]))
        self.store.suspend(parent["id"], parent["active_attempt_id"])
        answer = self.store.answer(wait["ref_id"], "DENIED", "Use a draft patch")
        self.assertEqual(answer["status"], "DENIED")
        self.assertEqual(self.store.answer(wait["ref_id"], "DENIED", "Use a draft patch")["id"], answer["id"])
        self.assertCode("APPROVAL_ALREADY_ANSWERED", self.store.answer, wait["ref_id"], "APPROVED")
        result = json.loads(self.store.one("SELECT result_json FROM waits WHERE id=?", (wait["id"],))["result_json"])
        self.assertEqual(result["args_hash"], digest(args["arguments"]))
        self.assertEqual(result["arguments"], args["arguments"])
        self.assertEqual(self.store.one("SELECT status FROM tasks WHERE id=?", (parent["id"],))["status"], "READY")

    def test_operation_reserved_before_io_dedup_and_parameter_binding(self):
        task = self.start()
        op, execute = self.store.begin_operation(task["id"], task["active_attempt_id"], "write1", "write_file", {"path": "x"}, "effect:x")
        self.assertTrue(execute)
        other, execute = self.store.begin_operation(task["id"], task["active_attempt_id"], "write2", "write_file", {"path": "x"}, "effect:x")
        self.assertFalse(execute)
        self.assertEqual(other["id"], op["id"])
        self.assertCode("IDEMPOTENCY_CONFLICT", self.store.begin_operation, task["id"], task["active_attempt_id"], "write2", "write_file", {"path": "y"}, "effect:y")
        self.assertCode("IDEMPOTENCY_CONFLICT", self.store.begin_operation, task["id"], task["active_attempt_id"], "write2", "write_file", {"path": "x"}, None)
        self.assertCode("IDEMPOTENCY_CONFLICT", self.store.begin_operation, task["id"], task["active_attempt_id"], "write1", "write_file", {"path": "y"}, "effect:x")
        self.store.finish_operation(op["id"], {"written": True})
        cached, execute = self.store.begin_operation(task["id"], task["active_attempt_id"], "write1", "write_file", {"path": "x"}, "effect:x")
        self.assertFalse(execute)
        self.assertEqual(json.loads(cached["result_json"]), {"written": True})
        self.assertCode("OPERATION_ALREADY_FINISHED", self.store.finish_operation, op["id"], {"written": False})

    def test_crash_recovery_fences_and_preserves_created_verifying_unknown(self):
        task = self.start()
        op, _ = self.store.begin_operation(task["id"], task["active_attempt_id"], "op", "tool", {"a": 1}, "effect")
        created = self.workflow()
        verifying = self.start()
        self.store.verify(verifying["id"], verifying["active_attempt_id"], {"summary": "done"})
        old_attempt = task["active_attempt_id"]
        self.store.close()
        self.store = Store(self.path)
        self.store.recover(self.policy)
        recovered = self.store.claim(task["id"])
        self.assertEqual(self.store.one("SELECT kind FROM attempts WHERE id=?", (recovered["active_attempt_id"],))["kind"], "RETRY")
        self.assertCode("STALE_ATTEMPT", self.store.save_snapshot, task["id"], old_attempt, [])
        found, execute = self.store.begin_operation(task["id"], recovered["active_attempt_id"], "op", "tool", {"a": 1}, "effect")
        self.assertFalse(execute)
        self.assertEqual(found["status"], "UNKNOWN")
        self.assertEqual(self.store.one("SELECT status FROM tasks WHERE id=?", (created["root_task_id"],))["status"], "CREATED")
        self.assertEqual(self.store.one("SELECT status FROM tasks WHERE id=?", (verifying["id"],))["status"], "VERIFYING")
        self.store.complete(verifying["id"])
        self.assertEqual(self.store.one("SELECT status FROM workflows WHERE id=?", (verifying["workflow_id"],))["status"], "COMPLETED")

    def test_terminal_cascade_fences_descendants_and_projects_root(self):
        parent = self.start()
        wait = self.child(parent)
        self.store.route(wait["ref_id"], "coder")
        child = self.store.claim(wait["ref_id"])
        nested = self.store.defer(child["id"], child["active_attempt_id"], "question", "HUMAN_CLARIFY", {"question": "Q?"}, self.policy)
        self.store.fail(parent["id"], "Stop", status="CANCELLED")
        self.assertEqual(self.store.one("SELECT status FROM tasks WHERE id=?", (child["id"],))["status"], "CANCELLED")
        self.assertEqual(self.store.one("SELECT status FROM workflows WHERE id=?", (parent["workflow_id"],))["status"], "CANCELLED")
        self.assertEqual(self.store.one("SELECT status FROM approvals WHERE id=?", (nested["ref_id"],))["status"], "EXPIRED")
        self.assertCode("STALE_ATTEMPT", self.store.verify, child["id"], child["active_attempt_id"], {})
        self.assertEqual(self.store.all("SELECT * FROM waits WHERE status='OPEN'"), [])

    def test_success_does_not_cancel_descendants_and_cannot_skip_verification(self):
        parent = self.start()
        self.assertCode("ILLEGAL_TRANSITION", self.store.complete, parent["id"])
        wait = self.child(parent)
        # A trusted timeout resolver may close a wait before its child finishes.
        self.store.resolve_wait(wait["id"], {"error": "timeout"})
        self.store.verify(parent["id"], parent["active_attempt_id"], {"summary": "premature"})
        self.assertCode("CHILDREN_ACTIVE", self.store.complete, parent["id"])
        self.assertEqual(self.store.one("SELECT status FROM tasks WHERE id=?", (wait["ref_id"],))["status"], "CREATED")
        self.store.fail(wait["ref_id"], "finished late")
        self.store.complete(parent["id"])
        self.assertEqual(self.store.one("SELECT status FROM tasks WHERE id=?", (parent["id"],))["status"], "COMPLETED")

    def test_retry_repair_and_deadline_bounds(self):
        policy = Policy(max_retries=0, max_repairs=0)
        task = self.start(self.workflow(policy=policy))
        self.assertEqual(self.store.retry(task["id"], task["active_attempt_id"], "transient", policy)["status"], "FAILED")
        task = self.start(self.workflow(policy=policy))
        self.store.verify(task["id"], task["active_attempt_id"], {})
        self.assertEqual(self.store.repair(task["id"], "bad", policy)["error"], "REPAIRS_EXHAUSTED")
        task = self.start()
        wait = self.child(task)
        with self.store.tx() as conn:
            conn.execute("UPDATE workflows SET deadline=0 WHERE id=?", (task["workflow_id"],))
        self.store.recover(self.policy)
        self.assertEqual(self.store.one("SELECT status FROM tasks WHERE id=?", (task["id"],))["status"], "TIMED_OUT")
        self.assertEqual(self.store.one("SELECT status FROM tasks WHERE id=?", (wait["ref_id"],))["status"], "CANCELLED")

    def test_expired_wait_resolves_and_wakes_without_failing_parent(self):
        task = self.start()
        wait = self.store.defer(task["id"], task["active_attempt_id"], "human", "HUMAN_CLARIFY", {"question": "Q?"}, self.policy)
        self.store.suspend(task["id"], task["active_attempt_id"])
        with self.store.tx() as conn:
            conn.execute("UPDATE waits SET deadline=0 WHERE id=?", (wait["id"],))
        self.store.recover(self.policy)
        self.assertEqual(self.store.one("SELECT status FROM tasks WHERE id=?", (task["id"],))["status"], "READY")
        self.assertEqual(self.store.one("SELECT status FROM approvals WHERE id=?", (wait["ref_id"],))["status"], "EXPIRED")

    def test_objective_experience_is_terminal_idempotent_and_scoped(self):
        task = self.start(self.workflow(user_id="human:alice"))
        self.store.verify(task["id"], task["active_attempt_id"], {"summary": "Verified delivery", "artifact_ids": []})
        self.assertEqual(self.store.all("SELECT * FROM memories"), [])
        self.store.complete(task["id"])
        self.store.complete(task["id"])
        memory = self.store.one("SELECT * FROM memories WHERE id=?", ("experience:" + task["id"],))
        self.assertEqual(memory["agent_id"], "planner")
        self.assertEqual(memory["user_id"], "human:alice")
        self.assertEqual(memory["workflow_id"], task["workflow_id"])
        objective = json.loads(memory["text"])
        self.assertEqual(objective["type"], "objective_experience")
        self.assertEqual(objective["outcome"], "COMPLETED")
        self.assertEqual(objective["summary"], "Verified delivery")
        self.assertEqual(len(self.store.all("SELECT * FROM memories")), 1)
        self.assertEqual(self.store.all("SELECT * FROM memories WHERE agent_id='planner' AND user_id='human:bob'"), [])

    def test_artifact_input_requires_task_access_and_explicit_target_grant(self):
        task = self.start(owner="coder")
        other = self.start(owner="coder")
        with self.store.tx() as conn:
            conn.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?)", ("private", task["workflow_id"], task["id"], "coder", "x", "hash", "text/plain", b"x", "[]", 0))
            conn.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?)", ("other_task", task["workflow_id"], other["id"], "coder", "x", "hash", "text/plain", b"x", "[]", 0))
        denied = self.child(task, input_artifact_ids=["private"])
        self.assertEqual(json.loads(denied["result_json"])["error"]["code"], "ARTIFACT_FORBIDDEN")
        denied = self.child(task, "different-task", input_artifact_ids=["other_task"])
        self.assertEqual(json.loads(denied["result_json"])["error"]["code"], "ARTIFACT_FORBIDDEN")
        with self.store.tx() as conn:
            conn.execute("UPDATE artifacts SET grantees_json='[\"coder\"]' WHERE id='private'")
        allowed = self.child(task, "granted", input_artifact_ids=["private"])
        self.assertEqual(allowed["status"], "OPEN")
        self.store.route(allowed["ref_id"], "coder")

    def test_generic_wait_resolution_expires_unused_approval(self):
        task = self.start(owner="coder")
        wait = self.store.defer(task["id"], task["active_attempt_id"], "push", "HUMAN_APPROVAL", {"tool_name": "git_push", "arguments": {"branch": "work"}}, self.policy)
        self.store.resolve_wait(wait["id"], {"error": {"code": "WAIT_TIMEOUT"}})
        self.assertEqual(self.store.one("SELECT status FROM approvals WHERE id=?", (wait["ref_id"],))["status"], "EXPIRED")
        self.assertCode("APPROVAL_EXPIRED", self.store.answer, wait["ref_id"], "APPROVED")

    def test_transaction_rollback_keeps_no_partial_event(self):
        wf = self.workflow()
        before = len(self.store.all("SELECT * FROM events"))
        with self.assertRaises(RuntimeError):
            with self.store.tx() as conn:
                self.store.event(conn, wf["id"], wf["root_task_id"], "WillRollback", {})
                raise RuntimeError("rollback")
        self.assertEqual(len(self.store.all("SELECT * FROM events")), before)


if __name__ == "__main__":
    unittest.main()
