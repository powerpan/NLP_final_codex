import copy
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from maplatform.demo import tool
from maplatform.domain import Policy, PlatformError, digest, dumps
from maplatform.evaluation import _token_usage, evaluate_suite
from maplatform.provider import Completion, ScriptedProvider
from maplatform.store import Store
from maplatform.tools import MemorySandbox, ToolGateway


CONTENT = "Hello, multi-agent!\n"
SHA = hashlib.sha256(CONTENT.encode()).hexdigest()


def final(summary="contract delivered", ids=None):
    return {"role": "assistant", "content": dumps({"summary": summary, "artifact_ids": ids or []}), "reasoning_content": ""}


def calling(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls), "reasoning_content": ""}


def case(case_id="good", *, goal=None, owner="researcher"):
    return {
        "id": case_id, "goal": goal or case_id, "owner": owner,
        "acceptance": [{"kind": "artifact", "name": "note.txt"}],
        "assertions": [
            {"kind": "summary_contains", "text": "contract delivered"},
            {"kind": "artifact_contains", "name": "note.txt", "text": CONTENT},
            {"kind": "artifact_sha256", "name": "note.txt", "sha256": SHA},
        ],
    }


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "evaluation.sqlite")
        self.policy = Policy(max_repairs=0, deadline_seconds=3)
        self.sandbox = MemorySandbox()
        self.gateway = ToolGateway(self.store, self.sandbox, self.policy)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_text_smoke_requires_real_peer_completion_not_summary_claim(self):
        suite = json.loads((Path(__file__).parents[1] / "examples/text_smoke.json").read_text())
        report = await evaluate_suite(self.store, ScriptedProvider([final("peer reply received")]), self.gateway, suite, self.policy)
        self.assertEqual(report["cases"][0]["status"], "ASSERTION_FAILED")
        self.assertEqual(report["cases"][0]["assertion_failures"][0]["reason"], "COMPLETED_TASK_COUNT_MISMATCH")

        def respond(messages, tools, *, task, purpose):
            if task["kind"] == "REPLY":
                message = final("離線測試驗證程式契約，真實模型品質仍需實測。")
            elif any(m["role"] == "tool" for m in messages):
                message = final("peer reply received；離線測試不等於真實模型品質驗證。")
            else:
                message = calling(tool("peer", "ask_agent", {"target_agent": "reviewer", "question": "Explain offline validation limits."}))
            return Completion(message, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

        report = await evaluate_suite(self.store, ScriptedProvider(respond), self.gateway, suite, self.policy)
        self.assertEqual(report["summary"]["passed"], 1)
        self.assertEqual(report["summary"]["call_count"], 3)
        self.assertTrue(report["summary"]["token_usage"]["complete"])
        self.assertEqual(report["summary"]["token_usage"]["reported_totals"]["total_tokens"], 45)

    def test_missing_usage_is_not_reported_as_zero_cost(self):
        usage = _token_usage([{"usage_json": '{}'}, {"usage_json": '{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}'}])
        self.assertFalse(usage["complete"])
        self.assertEqual(usage["reported_totals"]["total_tokens"], 10)
        self.assertEqual(usage["calls_reporting"]["total_tokens"], 1)
        self.assertEqual(usage["total_calls"], 2)
        self.assertIsNone(_token_usage([{"usage_json": '{}'}])["reported_totals"]["total_tokens"])

    async def test_text_smoke_rejects_extra_tool_calls_despite_successful_peer_reply(self):
        suite = json.loads((Path(__file__).parents[1] / "examples/text_smoke.json").read_text())

        def respond(messages, tools, *, task, purpose):
            if task["kind"] == "REPLY":
                return final("Scripted tests do not call a real model.")
            if any(m["role"] == "tool" for m in messages):
                return final("peer reply received")
            return calling(tool("peer", "ask_agent", {"target_agent": "reviewer", "question": "Explain scripted validation limits."}),
                           tool("unneeded", "search_memory", {"query": "unnecessary query"}))

        report = await evaluate_suite(self.store, ScriptedProvider(respond), self.gateway, suite, self.policy)
        case_report = report["cases"][0]
        self.assertEqual(case_report["workflow_status"], "COMPLETED")
        self.assertEqual(case_report["status"], "ASSERTION_FAILED")
        self.assertEqual([item["reason"] for item in case_report["assertion_failures"]], ["TOOL_CALLS_MISMATCH"])

    def fixture(self, *, mutate=None):
        def scripted(messages, tools, *, task, purpose):
            if mutate:
                mutate()
            if purpose == "BID":
                return {"role": "assistant", "content": dumps({"confidence": 0.9, "plan": "Publish the fixture file."})}
            goal = task["goal"]
            if goal == "failure":
                raise PlatformError("PROVIDER_HTTP", "Offline failure fixture")
            if goal == "timeout":
                with self.store.tx() as conn:
                    conn.execute("UPDATE workflows SET deadline=? WHERE id=?", (time.time() - 1, task["workflow_id"]))
                return final()
            if goal == "cancelled":
                self.store.fail(task["id"], "fixture cancellation", status="CANCELLED")
                return final()
            responses = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m["role"] == "tool"}
            if goal == "waiting":
                return calling(tool("human", "ask_human", {"question": "Choose a fixture value."}))
            if goal == "delegate":
                if "delegate" not in responses:
                    return calling(tool("delegate", "delegate_task", {"goal": "good", "required_capabilities": ["writing"], "acceptance": [{"kind": "artifact", "name": "note.txt"}]}))
                child = responses["delegate"].get("result", {})
                return final(ids=child.get("artifact_ids", []))
            if "write" not in responses:
                return calling(tool("write", "write_file", {"path": "note.txt", "content": "wrong\n" if goal == "wrong" else CONTENT}))
            if "publish" not in responses:
                return calling(tool("publish", "publish_artifact", {"path": "note.txt", "name": "note.txt"}))
            aid = responses["publish"].get("artifact_id")
            return final(ids=[aid] if aid else [])
        return ScriptedProvider(scripted)

    async def test_fixed_external_assertions_and_trace_wall_time(self):
        suite = {"id": "fixed-v1", "cases": [case()]}
        original = copy.deepcopy(suite)
        report = await evaluate_suite(self.store, self.fixture(), self.gateway, suite, self.policy)
        self.assertEqual(report["suite_hash"], digest(original))
        self.assertEqual(report["summary"]["tcr"], 1)
        self.assertEqual(report["summary"]["total"], 1)
        self.assertTrue(report["complete"])
        self.assertTrue(report["provider"]["fixture_only"])
        self.assertIn("do not measure model quality", report["interpretation"])
        self.assertTrue(report["tested_at"].endswith("Z"))
        result = report["cases"][0]
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["call_count"], 3)
        self.assertEqual(result["purpose_counts"], {"AGENT": 3})
        self.assertEqual(result["assertion_failures"], [])
        self.assertEqual(len(result["artifact_ids"]), 1)
        trace = result["trace"]
        wf = self.store.one("SELECT * FROM workflows WHERE id=?", (result["workflow_id"],))
        event = self.store.one("SELECT * FROM events WHERE id=?", (trace["terminal_event_id"],))
        self.assertEqual(result["wall_time_seconds"], event["created"] - wf["created"])
        self.assertEqual(trace["llm_call_ids"], [c["id"] for c in self.store.all("SELECT id FROM llm_calls WHERE workflow_id=? ORDER BY created,id", (wf["id"],))])
        registration = self.store.one("SELECT * FROM events WHERE id=?", (trace["registration_event_id"],))
        first_call = self.store.one("SELECT MIN(created) AS created FROM llm_calls WHERE workflow_id=?", (wf["id"],))
        self.assertLessEqual(registration["created"], first_call["created"])
        self.assertEqual(json.loads(registration["payload_json"])["suite_hash"], report["suite_hash"])
        self.assertEqual(suite, original)

    async def test_denominator_includes_failed_timedout_cancelled_and_waiting_cases(self):
        suite = {"id": "fixed-denominator", "cases": [case("good"), case("wrong"), case("failure"), case("timeout"), case("cancelled"), case("waiting")]}
        report = await evaluate_suite(self.store, self.fixture(), self.gateway, suite, self.policy)
        summary = report["summary"]
        self.assertEqual(summary["total"], 6)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["failed"], 5)
        self.assertEqual(summary["tcr_denominator"], 6)
        self.assertEqual(summary["tcr"], 1 / 6)
        self.assertEqual(summary["blocked_human"], 1)
        self.assertFalse(report["complete"])
        results = {c["case_id"]: c for c in report["cases"]}
        self.assertEqual(results["wrong"]["status"], "ASSERTION_FAILED")
        self.assertEqual(results["wrong"]["workflow_status"], "COMPLETED")
        self.assertEqual(len(results["wrong"]["assertion_failures"]), 2)
        self.assertEqual(results["failure"]["status"], "WORKFLOW_FAILED")
        self.assertEqual(results["timeout"]["status"], "TIMED_OUT")
        self.assertEqual(results["cancelled"]["status"], "CANCELLED")
        self.assertEqual(results["waiting"]["status"], "BLOCKED_HUMAN")
        self.assertIsNone(results["waiting"]["wall_time_seconds"])
        self.assertIsNone(results["waiting"]["trace"]["terminal_event_id"])
        self.assertTrue(self.store.one("SELECT * FROM approvals WHERE status='OPEN'"))
        self.assertEqual(len(self.store.all("SELECT * FROM workflows")), 6)

    async def test_assertions_remain_frozen_if_caller_mutates_suite_during_calls(self):
        suite = {"id": "immutable", "cases": [case()]}
        original_hash = digest(suite)
        def mutate():
            suite["cases"][0]["assertions"][0]["text"] = "new impossible text"
            suite["cases"][0]["goal"] = "failure"
        report = await evaluate_suite(self.store, self.fixture(mutate=mutate), self.gateway, suite, self.policy)
        self.assertEqual(report["suite_hash"], original_hash)
        self.assertEqual(report["summary"]["passed"], 1)
        self.assertNotEqual(digest(suite), original_hash)

    async def test_child_grant_is_authorized_and_negotiation_calls_count(self):
        suite = {"id": "collaboration", "cases": [case(goal="delegate", owner="planner")]}
        report = await evaluate_suite(self.store, self.fixture(), self.gateway, suite, self.policy)
        result = report["cases"][0]
        self.assertTrue(result["passed"])
        self.assertEqual(result["purpose_counts"], {"AGENT": 5, "BID": 2})
        self.assertEqual(result["call_count"], 7)
        self.assertEqual(len(result["trace"]["task_ids"]), 2)
        artifact = self.store.one("SELECT * FROM artifacts WHERE id=?", (result["artifact_ids"][0],))
        self.assertIn("planner", json.loads(artifact["grantees_json"]))
        self.assertNotEqual(artifact["task_id"], result["root_task_id"])

    async def test_unauthorized_root_artifact_content_is_never_extracted(self):
        suite = {"id": "acl", "cases": [case()]}
        original_all = self.store.one
        content_reads = []
        def guarded_read(sql, params=()):
            if "SELECT content FROM artifacts" in sql:
                content_reads.append(params)
                self.fail("Unauthorized artifact content must not be extracted")
            return original_all(sql, params)
        async def tamper_then_idle(runtime, **kwargs):
            wf = self.store.one("SELECT * FROM workflows")
            self.store.route(wf["root_task_id"], "researcher")
            task = self.store.claim(wf["root_task_id"])
            with self.store.tx() as conn:
                conn.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?)", ("unauthorized", wf["id"], "different-task", "coder", "note.txt", SHA, "text/plain", CONTENT.encode(), "[]", time.time()))
            self.store.verify(task["id"], task["active_attempt_id"], {"summary": "contract delivered", "artifact_ids": ["unauthorized"]})
            self.store.complete(task["id"])
        with patch("maplatform.evaluation.Runtime.run_until_idle", new=tamper_then_idle), patch.object(self.store, "one", side_effect=guarded_read):
            report = await evaluate_suite(self.store, self.fixture(), self.gateway, suite, self.policy)
        self.assertEqual(content_reads, [])
        result = report["cases"][0]
        self.assertFalse(result["passed"])
        self.assertIn("FINAL_ARTIFACT_NOT_AUTHORIZED", result["output_failures"])
        self.assertEqual(result["artifact_ids"], [])

    async def test_existing_scoped_memory_reported_without_claiming_ablation(self):
        with self.store.tx() as conn:
            conn.execute("INSERT INTO memories VALUES(?,?,?,?,?,?)", ("m1", "researcher", "human:local", "old-wf", "prior experience", time.time(),))
        report = await evaluate_suite(self.store, self.fixture(), self.gateway, {"id": "memory", "cases": [case()]}, self.policy)
        self.assertEqual(report["memory"]["mode"], "existing_scoped_reuse")
        self.assertEqual(report["memory"]["preexisting_count"], 1)
        self.assertEqual(report["memory"]["preexisting_by_agent"], {"researcher": 1})

    async def test_preflight_rejects_invalid_suite_before_any_workflow_or_call(self):
        bad_cases = [
            {"id": "empty", "cases": []},
            {"id": "duplicate", "cases": [case(), case()]},
            {"id": "empty-assertions", "cases": [{**case(), "assertions": []}]},
            {"id": "unknown", "cases": [{**case(), "assertions": [{"kind": "llm_judge", "text": "pass"}]}]},
            {"id": "bad-sha", "cases": [{**case(), "assertions": [{"kind": "artifact_sha256", "name": "note.txt", "sha256": "bad"}]}]},
        ]
        provider = self.fixture()
        for suite in bad_cases:
            with self.subTest(suite=suite):
                with self.assertRaises(PlatformError) as caught:
                    await evaluate_suite(self.store, provider, self.gateway, suite, self.policy)
                self.assertEqual(caught.exception.code, "INVALID_EVALUATION_SUITE")
                self.assertFalse(self.store.all("SELECT * FROM workflows"))
                self.assertEqual(provider.calls, [])

    async def test_existing_open_workflow_is_not_silently_run(self):
        original = self.store.create_workflow("unrelated", policy=self.policy)
        provider = self.fixture()
        with self.assertRaises(PlatformError) as caught:
            await evaluate_suite(self.store, provider, self.gateway, {"id": "new", "cases": [case()]}, self.policy)
        self.assertEqual(caught.exception.code, "EVALUATION_BUSY")
        self.assertEqual(len(self.store.all("SELECT * FROM workflows")), 1)
        self.assertEqual(self.store.one("SELECT status FROM tasks WHERE id=?", (original["root_task_id"],))["status"], "CREATED")
        self.assertEqual(provider.calls, [])

    def test_example_exact_hashes_match_declared_contents(self):
        path = Path(__file__).parent.parent / "examples" / "evaluation_cases.json"
        suite = json.loads(path.read_text())
        for example in suite["cases"]:
            contains = next(a for a in example["assertions"] if a["kind"] == "artifact_contains")
            sha = next(a for a in example["assertions"] if a["kind"] == "artifact_sha256")
            self.assertEqual(hashlib.sha256(contains["text"].encode()).hexdigest(), sha["sha256"])


if __name__ == "__main__":
    unittest.main()
