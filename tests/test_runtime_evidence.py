"""Offline regressions for the real-news failure modes; no API calls."""
import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from maplatform.demo import tool
from maplatform.domain import Policy, PlatformError, dumps
from maplatform.provider import ScriptedProvider
from maplatform.runtime import COORDINATION, Runtime, RUNTIME_CONTEXT_MARKER, TASK_CONTEXT_MARKER, validate_coordination
from maplatform.store import Store
from maplatform.tools import MemorySandbox, ToolGateway


def final(summary="Done"):
    return {"role": "assistant", "content": dumps({"summary": summary, "artifact_ids": []})}


def calling(call):
    return {"role": "assistant", "content": "", "reasoning_content": "preserve original reasoning", "tool_calls": [call]}


class WebFixture:
    cache_identity = "offline-web-fixture-v1"
    max_results = 50

    def __init__(self):
        self.calls = []

    async def search(self, query, limit=5):
        self.calls.append(("search", query, limit))
        return [{"url": f"https://example.com/{query}/{i}", "title": f"Source {i}",
                 "summary": "s" * 3000, "snippet": "snippet", "date": "2026-10-19T00:00:00Z"} for i in range(limit)]

    async def fetch(self, url):
        self.calls.append(("fetch", url))
        return {"url": url, "title": "Saved article", "content": "article content " * 2500,
                "truncated": False, "content_bytes": 40000, "returned_content_bytes": 40000}


class RuntimeEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "evidence.sqlite")
        self.policy = Policy(max_context_chars=24000)
        self.web = WebFixture()
        self.gateway = ToolGateway(self.store, MemorySandbox(), self.policy, search=self.web)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def workflow(self, owner="researcher"):
        return self.store.create_workflow("Current source-backed news", owner=owner, policy=self.policy)

    def task(self, wf):
        return self.store.one("SELECT * FROM tasks WHERE id=?", (wf["root_task_id"],))

    def start(self, wf):
        self.store.route(wf["root_task_id"], self.task(wf)["owner"])
        return self.store.claim(wf["root_task_id"])

    async def drive(self, provider, wf):
        await Runtime(self.store, provider, self.gateway, self.policy).run_until_idle(workflow_ids=[wf["id"]], max_seconds=4)

    @staticmethod
    def context(messages):
        static = json.loads(messages[0]["content"].split(TASK_CONTEXT_MARKER, 1)[1])
        return {**static, **json.loads(messages[-1]["content"].split(RUNTIME_CONTEXT_MARKER, 1)[1])}

    async def test_large_searches_keep_full_evidence_and_fit_model_context(self):
        wf = self.workflow()
        provider = ScriptedProvider([calling(tool(f"search-{i}", "search_web", {"query": f"topic{i}", "limit": 10})) for i in range(5)] + [final()])
        await self.drive(provider, wf)
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        ops = self.store.all("SELECT * FROM operations WHERE tool_name='search_web'")
        self.assertEqual(len(ops), 5)
        self.assertGreater(sum(len(op["result_json"]) for op in ops), 150000)
        for request in provider.calls:
            self.assertLessEqual(len(dumps({"messages": request["messages"], "tools": request["tools"]})), self.policy.max_context_chars)
        latest = provider.calls[-1]["messages"]
        observations = [json.loads(m["content"]) for m in latest if m["role"] == "tool"]
        self.assertTrue(any("note" in item and "read_web_result" in item for item in observations))
        self.assertTrue(all(m["reasoning_content"] == "preserve original reasoning" for m in latest if m.get("tool_calls")))
        self.assertEqual([m["tool_call_id"] for m in latest if m["role"] == "tool"], [f"search-{i}" for i in range(5)])
        context = self.context(latest)
        self.assertEqual(context["remaining_before_this_request"]["web_requests"], 15)
        self.assertEqual(context["remaining_before_this_request"]["model_calls"], 55)
        self.assertRegex(context["current_utc"], r"^\d{4}-\d\d-\d\dT.*\+00:00$")

    async def test_legacy_full_web_snapshot_is_projected_without_rewriting_evidence(self):
        wf = self.workflow()
        task = self.start(wf)
        runtime = Runtime(self.store, None, self.gateway, self.policy)
        call = tool("legacy-search", "search_web", {"query": "legacy", "limit": 15})
        op, _ = self.store.begin_operation(task["id"], task["active_attempt_id"], call["id"], "search_web", {"query": "legacy", "limit": 15})
        raw = await self.gateway.execute(task, "search_web", {"query": "legacy", "limit": 15})
        self.store.finish_operation(op["id"], raw)
        raw_text = dumps(raw)
        self.assertGreater(len(raw_text), self.policy.max_context_chars)
        messages = runtime.initial_messages(task) + [calling(call), {"role": "tool", "tool_call_id": call["id"], "content": raw_text}]
        self.store.save_snapshot(task["id"], task["active_attempt_id"], messages)
        provider = ScriptedProvider([final()])
        await self.drive(provider, wf)
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        self.assertLess(len(next(m["content"] for m in provider.calls[0]["messages"] if m.get("tool_call_id") == call["id"])), 6001)
        self.assertEqual(self.store.one("SELECT result_json FROM operations WHERE id=?", (op["id"],))["result_json"], raw_text)
        self.assertEqual(next(m["content"] for m in self.store.snapshot(task["id"]) if m["role"] == "tool"), raw_text)

    async def test_old_read_pages_become_recoverable_references_under_pressure(self):
        # Leave space for the current page alongside both WORK/REPLY capability
        # menus; six pages still exceed this intentionally small context limit.
        self.policy = Policy(max_context_chars=28000)
        wf = self.workflow()
        step = 0
        original_id = None
        def script(messages, tools, *, task, purpose):
            nonlocal step, original_id
            step += 1
            if step == 1:
                return calling(tool("fetch", "fetch_web", {"url": "https://example.com/article"}))
            original_id = self.store.one("SELECT id FROM operations WHERE tool_name='fetch_web'")["id"]
            if step <= 7:
                return calling(tool(f"page-{step-2}", "read_web_result", {"operation_id": original_id, "offset": (step-2)*6000, "max_chars": 6000}))
            return final()
        provider = ScriptedProvider(script)
        await self.drive(provider, wf)
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        latest = provider.calls[-1]["messages"]
        pages = [json.loads(m["content"]) for m in latest if m.get("tool_call_id", "").startswith("page-")]
        self.assertEqual(pages[0]["operation_id"], original_id)
        self.assertEqual(pages[0]["offset"], 0)
        self.assertEqual(pages[0]["read_web_result"]["operation_id"], original_id)
        self.assertNotIn("content", pages[0])
        self.assertIn("content", pages[-1])
        saved_pages = self.store.all("SELECT result_json FROM operations WHERE tool_name='read_web_result'")
        self.assertEqual(len(saved_pages), 6)
        self.assertTrue(all(len(json.loads(row["result_json"])["content"]) == 6000 for row in saved_pages))
        self.assertEqual(self.web.calls, [("fetch", "https://example.com/article")])

    async def test_parent_sees_public_saved_sources_after_child_fails(self):
        wf = self.workflow("planner")
        def script(messages, tools, *, task, purpose):
            replies = [m for m in messages if m["role"] == "tool"]
            if task["owner"] == "planner":
                if not replies:
                    return calling(tool("research", "delegate_task", {"goal": "Find current sources", "target_agent": "researcher", "required_capabilities": ["research"], "acceptance": [{"kind": "nonempty"}]}))
                context = self.context(messages)
                self.assertEqual(context["saved_web_evidence"][0]["tool"], "search_web")
                self.assertEqual(context["failed_tasks"][0]["error"], "CONTEXT_LIMIT")
                self.assertNotIn("PRIVATE_CHILD_REASONING", dumps(messages))
                peer = next(p for p in context["peers"] if p["id"] == "researcher")
                self.assertIn("search_web", peer["work_tools"])
                self.assertIn("read_web_result", context["current_task_tools"])
                return final("A search succeeded, but the child reached a context limit; saved sources remain available.")
            if not replies:
                response = calling(tool("source", "search_web", {"query": "news", "limit": 1}))
                response["reasoning_content"] = "PRIVATE_CHILD_REASONING"
                return response
            raise PlatformError("CONTEXT_LIMIT", "fixture child context failure")
        provider = ScriptedProvider(script)
        await self.drive(provider, wf)
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM tasks WHERE status='FAILED'")["n"], 1)

    async def test_cache_reuses_success_but_not_provider_changes_or_invalid_arguments(self):
        wf = self.workflow()
        task = self.start(wf)
        runtime = Runtime(self.store, None, self.gateway, self.policy)
        first = await runtime.invoke(task, tool("first", "search_web", {"query": "same"}))
        second = await runtime.invoke(task, tool("second", "search_web", {"query": "same", "limit": 5}))
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(len(self.web.calls), 1)
        for i, args in enumerate(({"query": "same", "extra": "not allowed"}, {"query": "same", "limit": True}, {"query": "same", "limit": 5.0})):
            result = await runtime.invoke(task, tool(f"invalid-{i}", "search_web", args))
            self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(len(self.web.calls), 1)
        self.web.cache_identity = "different-provider-v2"
        changed = await runtime.invoke(task, tool("changed", "search_web", {"query": "same"}))
        self.assertNotEqual(changed["operation_id"], first["operation_id"])
        self.assertEqual(len(self.web.calls), 2)

    async def test_runtime_index_never_shares_other_workflows_or_shortens_citation_urls(self):
        first = self.workflow()
        other = self.workflow()
        task = self.start(first)
        runtime = Runtime(self.store, None, self.gateway, self.policy)
        result = await runtime.invoke(task, tool("source", "search_web", {"query": "private-workflow", "limit": 1}))
        other_task = self.task(other)
        context = runtime.runtime_context(other_task, runtime.schemas(other_task), self.policy)
        self.assertEqual(context["saved_web_evidence"], [])
        self.assertNotIn(result["operation_id"], dumps(context))
        op, _ = self.store.begin_operation(task["id"], task["active_attempt_id"], "long", "fetch_web", {"url": "https://example.com"})
        long_url = "https://example.com/" + "a" * 500
        self.store.finish_operation(op["id"], {"page": {"url": long_url, "title": "long", "content": "body"}})
        context = runtime.runtime_context(task, runtime.schemas(task), self.policy)
        source = context["saved_web_evidence"][0]["untrusted_source_metadata"][0]
        self.assertNotIn("url", source)
        self.assertTrue(source["source_url_omitted"])

    def test_delegation_schema_exposes_actual_acceptance_shapes(self):
        spec = next(s for s in COORDINATION if s["function"]["name"] == "delegate_task")
        shapes = spec["function"]["parameters"]["properties"]["acceptance"]["items"]["oneOf"]
        by_kind = {s["properties"]["kind"]["enum"][0]: s for s in shapes}
        self.assertEqual(set(by_kind), {"nonempty", "contains", "artifact", "command"})
        self.assertEqual(by_kind["contains"]["required"], ["kind", "text"])
        self.assertNotIn("note", by_kind["contains"]["properties"])
        self.assertIn("argv", by_kind["command"]["required"])
        for criterion in ({"kind": "nonempty"}, {"kind": "contains", "text": "source"}, {"kind": "artifact", "name": "notes.md"}, {"kind": "command", "argv": ["python", "-m", "unittest"]}):
            validate_coordination("delegate_task", {"goal": "Review", "required_capabilities": [], "acceptance": [criterion]})

    async def test_child_handoff_filters_nontransitive_grants_without_changing_original_results(self):
        wf = self.workflow("planner")
        def script(messages, tools, *, task, purpose):
            replies = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m["role"] == "tool"}
            if task["owner"] == "planner":
                if "child" not in replies:
                    return calling(tool("child", "delegate_task", {"goal": "Publish notes and ask a reviewer to produce a private review", "target_agent": "researcher", "required_capabilities": ["research"], "acceptance": [{"kind": "nonempty"}]}))
                observed = replies["child"]
                self.assertEqual(observed["artifact_delivery"]["withheld_count"], 1)
                self.assertEqual(len(observed["result"]["artifact_ids"]), 1)
                self.assertEqual(observed["artifact_delivery"]["accessible"][0]["name"], "notes.md")
                context = self.context(messages)
                self.assertEqual([a["name"] for a in context["accessible_artifacts"]["items"]], ["notes.md"])
                self.assertNotIn("restricted-review.txt", dumps(context))
                researcher = next(p for p in context["peers"] if p["id"] == "researcher")
                self.assertIn("import_artifact", researcher["work_tools"])
                self.assertNotIn("import_artifact", researcher["reply_tools"])
                self.assertNotIn("publish_artifact", researcher["reply_tools"])
                return {"role": "assistant", "content": dumps({"summary": "Public notes and the review completed", "artifact_ids": observed["result"]["artifact_ids"]})}
            name = "notes.md" if task["owner"] == "researcher" else "restricted-review.txt"
            if "write" not in replies:
                return calling(tool("write", "write_file", {"path": name, "content": "evidence"}))
            if "publish" not in replies:
                grantees = ["planner"] if task["owner"] == "researcher" else ["researcher"]
                return calling(tool("publish", "publish_artifact", {"path": name, "name": name, "grantees": grantees}))
            ids = [replies["publish"]["artifact_id"]]
            if task["owner"] == "researcher":
                if "review" not in replies:
                    return calling(tool("review", "delegate_task", {"goal": "Publish a private review for researcher", "target_agent": "reviewer", "required_capabilities": ["review"], "acceptance": [{"kind": "nonempty"}]}))
                ids += replies["review"]["result"]["artifact_ids"]
            return {"role": "assistant", "content": dumps({"summary": "Work completed", "artifact_ids": ids})}
        provider = ScriptedProvider(script)
        await self.drive(provider, wf)
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM tasks")["n"], 3)
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM tasks WHERE repairs>0")["n"], 0)
        child = self.store.one("SELECT * FROM tasks WHERE owner='researcher'")
        self.assertEqual(len(json.loads(child["result_json"])["artifact_ids"]), 2)
        wait = self.store.one("SELECT * FROM waits WHERE task_id=?", (wf["root_task_id"],))
        original_wait = json.loads(wait["result_json"])
        self.assertEqual(len(original_wait["result"]["artifact_ids"]), 2)
        # Older snapshots may still hold the full child result; reading them
        # projects authority again without changing the saved result or grants.
        old = self.store.snapshot(wf["root_task_id"])
        for message in old:
            if message.get("tool_call_id") == "child":
                message["content"] = wait["result_json"]
        runtime = Runtime(self.store, None, self.gateway, self.policy)
        projected = runtime.model_messages(self.task(wf), old, runtime.schemas(self.task(wf)), self.policy)
        observation = next(json.loads(m["content"]) for m in projected if m.get("tool_call_id") == "child")
        self.assertEqual(len(observation["result"]["artifact_ids"]), 1)
        self.assertEqual(next(m["content"] for m in old if m.get("tool_call_id") == "child"), wait["result_json"])
        self.assertEqual(json.loads(self.store.one("SELECT grantees_json FROM artifacts WHERE name='restricted-review.txt'")["grantees_json"]), ["researcher"])

    async def test_artifact_verification_feedback_names_invalid_ids_and_authorized_alternatives(self):
        wf = self.workflow("planner")
        task = self.start(wf)
        wait = self.store.defer(task["id"], task["active_attempt_id"], "setup", "CHILD_TASK", {"goal": "Fixture publication", "target_agent": "researcher", "required_capabilities": ["research"], "acceptance": []}, self.policy)
        self.store.route(wait["ref_id"], "researcher")
        child = self.store.claim(wait["ref_id"])
        await self.gateway.execute(child, "write_file", {"path": "notes.md", "content": "public notes"})
        artifact = await self.gateway.execute(child, "publish_artifact", {"path": "notes.md", "name": "notes.md", "grantees": ["planner"]})
        self.store.verify(child["id"], child["active_attempt_id"], {"summary": "published", "artifact_ids": [artifact["artifact_id"]]})
        self.store.complete(child["id"])
        wrong = "artifact_not_authorized"
        provider = ScriptedProvider([
            {"role": "assistant", "content": dumps({"summary": "completed", "artifact_ids": [wrong, artifact["artifact_id"]]})},
            {"role": "assistant", "content": dumps({"summary": "completed", "artifact_ids": [artifact["artifact_id"]]})},
        ])
        await self.drive(provider, wf)
        self.assertEqual(self.task(wf)["status"], "COMPLETED")
        feedback = next(m["content"] for m in reversed(provider.calls[1]["messages"]) if m["role"] == "user")
        for expected in ("ARTIFACT_ACCESS_INVALID", wrong, artifact["artifact_id"], "notes.md", "ask_agent REPLY cannot import or publish", "Remove nonessential"):
            self.assertIn(expected, feedback)

    def test_artifact_catalog_is_bounded_and_never_truncates_ids_or_leaks_other_workflows(self):
        wf = self.workflow("planner")
        other = self.workflow("planner")
        task = self.task(wf)
        with self.store.tx() as conn:
            for i in range(40):
                aid = "artifact_" + str(i).zfill(32)
                conn.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?)", (aid, wf["id"], task["id"], "planner", "long-name-" + "a" * 400,
                             hashlib.sha256(b"x").hexdigest(), "text/plain", b"x", "[]", i))
            conn.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?)", ("artifact_other", other["id"], other["root_task_id"], "planner", "OTHER_PRIVATE_NAME",
                         hashlib.sha256(b"x").hexdigest(), "text/plain", b"x", '["planner"]', 100))
        catalog = Runtime(self.store, None, self.gateway, self.policy).accessible_artifacts(task)
        self.assertEqual(catalog["total_authorized"], 40)
        self.assertGreater(catalog["omitted_count"], 0)
        self.assertLessEqual(len(dumps(catalog)), 5500)
        self.assertTrue(all(len(item["artifact_id"]) == 41 for item in catalog["items"]))
        self.assertNotIn("OTHER_PRIVATE_NAME", dumps(catalog))
        self.assertTrue(all(item.get("name_truncated") for item in catalog["items"]))


if __name__ == "__main__":
    unittest.main()
