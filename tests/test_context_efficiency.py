"""Lossless request projections; no model or network calls."""
import copy
import json
import unittest
from unittest.mock import patch

from maplatform.demo import tool
from maplatform.domain import Policy, dumps
from maplatform.evidence import compact_web_view
from maplatform.provider import _validate_history
from maplatform.runtime import Runtime, RUNTIME_CONTEXT_MARKER, TASK_CONTEXT_MARKER, deduplicate_private_memory
from maplatform.store import Store
from maplatform.tools import MemorySandbox, ToolGateway


class WebFixture:
    cache_identity = "context-fixture"
    max_results = 5

    def __init__(self):
        self.calls = 0

    async def search(self, query, limit=5):
        self.calls += 1
        return [{"url": "https://example.org/article", "title": "Dated evidence", "summary": "Original search text"}]

    async def fetch(self, url):
        self.calls += 1
        return {"url": url, "title": "Article", "content": "第一段🙂" * 1000, "truncated": False}


class ContextEfficiencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.policy = Policy()
        self.web = WebFixture()
        self.gateway = ToolGateway(self.store, MemorySandbox(), self.policy, search=self.web)
        self.runtime = Runtime(self.store, None, self.gateway, self.policy)
        self.wf = self.store.create_workflow("Inspect public evidence", owner="researcher", policy=self.policy)
        self.store.route(self.wf["root_task_id"], "researcher")
        self.task = self.store.claim(self.wf["root_task_id"])

    def tearDown(self):
        self.store.close()

    def project(self, messages):
        return self.runtime.model_messages(self.task, messages, self.runtime.schemas(self.task), self.policy)

    def dynamic(self, messages):
        return json.loads(messages[-1]["content"].split(RUNTIME_CONTEXT_MARKER, 1)[1])

    async def invoke_messages(self, messages, call_id, name, args):
        call = tool(call_id, name, args)
        result = await self.runtime.invoke(self.task, call)
        messages.extend([{"role": "assistant", "content": "", "reasoning_content": "Full original thinking " + call_id,
                          "tool_calls": [call]},
                         {"role": "tool", "tool_call_id": call_id, "content": dumps(result)}])
        return result

    async def test_clock_and_budget_suffix_preserves_stable_prefix_and_all_reasoning(self):
        messages = self.runtime.initial_messages(self.task)
        await self.invoke_messages(messages, "saved", "search_web", {"query": "dated sources"})
        # A final-only assistant turn must also retain its reasoning when tools
        # are present in a subsequent request (DeepSeek thinking contract).
        messages.append({"role": "assistant", "content": '{"summary":"prior","artifact_ids":[]}',
                         "reasoning_content": "Original final-only reasoning"})
        messages.append({"role": "user", "content": "Check one more source"})
        original = copy.deepcopy(messages)
        with patch("maplatform.runtime.datetime") as clock:
            clock.now.return_value.isoformat.return_value = "2026-10-19T00:00:00+00:00"
            clock.fromtimestamp.return_value.isoformat.return_value = "2026-10-19T00:10:00+00:00"
            first = self.project(messages)
            clock.now.return_value.isoformat.return_value = "2026-10-19T00:00:30+00:00"
            self.store.reserve_call(self.task["id"], self.task["active_attempt_id"], "AGENT", "fixture")
            second = self.project(messages)
        self.assertEqual(first[:-1], second[:-1])
        self.assertNotEqual(first[-1], second[-1])
        self.assertIn(TASK_CONTEXT_MARKER, first[0]["content"])
        self.assertNotIn(RUNTIME_CONTEXT_MARKER, first[0]["content"])
        self.assertEqual(first[-1]["role"], "system")
        self.assertEqual([m for m in first if m["role"] == "assistant"], [m for m in original if m["role"] == "assistant"])
        self.assertEqual(_validate_history(first), {"saved"})
        self.assertEqual(messages, original)
        self.assertEqual(self.dynamic(second)["remaining_before_this_request"]["model_calls"], self.policy.max_calls - 1)

    async def test_duplicate_operation_and_page_keep_one_body_and_all_tool_replies(self):
        messages = self.runtime.initial_messages(self.task)
        source = await self.invoke_messages(messages, "fetch-one", "fetch_web", {"url": "https://example.org/article"})
        await self.invoke_messages(messages, "fetch-repeat", "fetch_web", {"url": "https://example.org/article"})
        page_args = {"operation_id": source["operation_id"], "offset": 0, "max_chars": 1000}
        await self.invoke_messages(messages, "page-one", "read_web_result", page_args)
        await self.invoke_messages(messages, "page-repeat", "read_web_result", page_args)
        await self.invoke_messages(messages, "page-distinct", "read_web_result", {**page_args, "offset": 1000})
        original = copy.deepcopy(messages)
        self.store.save_snapshot(self.task["id"], self.task["active_attempt_id"], messages)
        result = self.project(messages)
        observations = {m["tool_call_id"]: json.loads(m["content"]) for m in result if m["role"] == "tool"}
        self.assertEqual(observations["fetch-repeat"]["duplicate_of_tool_call_id"], "fetch-one")
        self.assertEqual(observations["page-repeat"]["duplicate_of_tool_call_id"], "page-one")
        self.assertEqual(observations["page-repeat"]["read_web_result"], page_args)
        self.assertEqual(observations["page-one"]["content"], "第一段🙂" * 250)
        self.assertEqual(observations["page-distinct"]["offset"], 1000)
        self.assertIn("content", observations["page-distinct"])
        self.assertEqual(_validate_history(result), set(observations))
        self.assertEqual(self.web.calls, 1)
        self.assertEqual(messages, original)
        self.assertEqual(self.store.snapshot(self.task["id"]), original)
        saved = json.loads(self.store.one("SELECT result_json FROM operations WHERE id=?", (source["operation_id"],))["result_json"])
        self.assertEqual(saved["page"]["content"], "第一段🙂" * 1000)

    async def test_shared_index_keeps_unseen_successes_and_omits_duplicate_metadata(self):
        messages = self.runtime.initial_messages(self.task)
        hidden = await self.runtime.invoke(self.task, tool("shared", "search_web", {"query": "shared evidence"}))
        seen = await self.invoke_messages(messages, "in-history", "fetch_web", {"url": "https://example.org/article"})
        result = self.project(messages)
        index = self.dynamic(result)["saved_web_evidence"]
        self.assertEqual([row["operation_id"] for row in index], [hidden["operation_id"]])
        self.assertNotIn(seen["operation_id"], dumps(index))
        self.assertIn("https://example.org/article", result[-2]["content"])

    async def test_partial_saved_page_keeps_source_index_and_duplicate_pressure_stays_retrievable(self):
        source = await self.runtime.invoke(self.task, tool("source", "fetch_web", {"url": "https://example.org/article"}))
        partial = self.runtime.initial_messages(self.task)
        await self.invoke_messages(partial, "deep-page", "read_web_result",
                                   {"operation_id": source["operation_id"], "offset": 1000, "max_chars": 100})
        self.assertEqual(self.dynamic(self.project(partial))["saved_web_evidence"][0]["operation_id"], source["operation_id"])
        repeated = self.runtime.initial_messages(self.task)
        await self.invoke_messages(repeated, "original-preview", "fetch_web", {"url": "https://example.org/article"})
        await self.invoke_messages(repeated, "repeated-preview", "fetch_web", {"url": "https://example.org/article"})
        full = self.project(repeated)
        tools = self.runtime.schemas(self.task)
        pressure = Policy(max_context_chars=len(dumps({"messages": full, "tools": tools})) - 800)
        compact = self.runtime.model_messages(self.task, repeated, tools, pressure)
        observations = [json.loads(m["content"]) for m in compact if m["role"] == "tool"]
        self.assertNotIn("page", observations[0])
        self.assertEqual(observations[1]["duplicate_of_tool_call_id"], "original-preview")
        for observation in observations:
            self.assertEqual(observation["read_web_result"]["operation_id"], source["operation_id"])
            self.assertEqual(observation["read_web_result"]["offset"], 0)
        self.assertLessEqual(len(dumps({"messages": compact, "tools": tools})), pressure.max_context_chars)
        self.assertEqual(_validate_history(compact), {"original-preview", "repeated-preview"})

    async def test_legacy_context_deduplicates_exact_public_summary_without_rewriting_snapshot(self):
        messages = self.runtime.initial_messages(self.task)
        context = json.loads(messages[1]["content"])
        experience = {"type": "objective_experience", "summary": "Shared public answer", "status": "FAILED",
                      "error": "CHECK_FAILED", "acceptance": [{"kind": "contains", "text": "required"}], "task_id": "previous"}
        context.update(session={"previous_turns": [{"role": "assistant", "content": "Shared public answer"}]},
                       private_memory=[dumps(experience), "Other private fact", "Shared public answer"])
        messages[1]["content"] = dumps(context)
        original = copy.deepcopy(messages)
        result = self.project(messages)
        projected = json.loads(result[1]["content"])
        memory = json.loads(projected["private_memory"][0])
        self.assertEqual(memory.pop("summary_from_session_previous_turns_index_0_based"), 0)
        self.assertEqual(memory, {k: v for k, v in experience.items() if k != "summary"})
        self.assertEqual(projected["private_memory"][1:], ["Other private fact"])
        self.assertEqual(projected["session"], context["session"])
        self.assertEqual(messages, original)


class ExactProjectionTests(unittest.TestCase):
    def test_memory_only_exact_duplicates_are_removed(self):
        context = {"goal": "goal", "authorized_messages": [{"content": "shared"}],
                   "session": {"previous_turns": [{"content": "answer"}]},
                   "private_memory": ["goal", "shared", "answer", "unique", "unique", " answer", "answer!"]}
        self.assertEqual(deduplicate_private_memory(context)["private_memory"], ["unique", " answer", "answer!"])

    def test_duplicate_url_metadata_is_removed_only_when_full_urls_remain(self):
        view = {"source_urls": ["https://example.org/article"], "page": {"url": "https://example.org/article", "preview": "exact source text"}}
        original = copy.deepcopy(view)
        self.assertEqual(compact_web_view(view), {"page": view["page"]})
        self.assertEqual(view, original)
        absent = {**view, "page": {"url": "https://example.org/different"}}
        self.assertEqual(compact_web_view(absent), absent)
        omitted = {"source_urls": [], "page": {"source_url_omitted": True}}
        self.assertEqual(compact_web_view(omitted), omitted)


if __name__ == "__main__":
    unittest.main()
