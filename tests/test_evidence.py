"""Persisted public web evidence: bounded model views, no additional HTTP."""
import json
import unittest

from maplatform.domain import AGENTS, PlatformError, Policy, dumps
from maplatform.evidence import MAX_PROJECTION_CHARS, project_web_result, read_web_result
from maplatform.store import Store
from maplatform.tools import MemorySandbox, ToolGateway


def operation(tool="fetch_web", *, content="完整內容 🙂", entries=None, **changes):
    result = ({"page": {"url": "https://example.com/article", "title": "Source",
                        "content": content, "content_bytes": len(content.encode()),
                        "returned_content_bytes": len(content.encode()), "truncated": False}}
              if tool == "fetch_web" else {"results": entries or []})
    return {"id": "operation_fixture", "status": "SUCCEEDED", "tool_name": tool,
            "result_json": dumps(result), **changes}


class EvidenceProjectionTests(unittest.TestCase):
    def test_fetch_compact_view_keeps_original_and_marks_available_text(self):
        op = operation(content="完整內容 🙂" * 8000)
        original = op["result_json"]
        view = project_web_result(op)
        self.assertLessEqual(len(dumps(view)), MAX_PROJECTION_CHARS)
        self.assertTrue(view["projection_truncated"])
        self.assertFalse(view["stored_result_truncated"])
        self.assertEqual(view["available_chars"], 6 * 8000)
        self.assertEqual(view["source_urls"], ["https://example.com/article"])
        self.assertEqual(view["read_web_result"]["operation_id"], op["id"])
        self.assertTrue(view["untrusted"])
        self.assertEqual(op["result_json"], original)

    def test_many_search_results_keep_dates_and_pageable_omitted_sources(self):
        entries = [{"url": "https://example.com/" + str(i), "title": "來源 " + str(i),
                    "snippet": "短摘要" * 200, "summary": "長摘要" * 3000,
                    "date": "2026-10-19T00:00:00Z", "site_name": "Example",
                    "rerank_score": 2.2, "authority": 3, "truncated": False} for i in range(50)]
        op = operation("search_web", entries=entries)
        view = project_web_result(op)
        self.assertLessEqual(len(dumps(view)), MAX_PROJECTION_CHARS)
        self.assertTrue(view["projection_truncated"])
        self.assertEqual(view["returned_result_count"], 50)
        self.assertLess(view["shown_result_count"], 50)
        self.assertEqual(view["results"][0]["date"], entries[0]["date"])
        self.assertEqual(view["results"][0]["authority"], 3)
        self.assertEqual(view["results"][0]["rerank_score"], 2.2)
        self.assertEqual(view["results"][0]["site_name"], "Example")
        chunks, offset = [], 0
        while offset is not None:
            page = read_web_result(op, offset=offset, max_chars=6000)
            chunks.append(page["content"])
            offset = page["next_offset"]
        self.assertEqual("".join(chunks), op["result_json"])

    def test_unicode_paging_reassembles_without_loss_or_repeated_characters(self):
        text = "繁體🙂e\u0301\n" * 1234
        op = operation(content=text)
        pieces, offset = [], 0
        while offset is not None:
            page = read_web_result(op, offset=offset, max_chars=17)
            self.assertEqual(page["offset"], offset)
            self.assertEqual(page["available_chars"], len(text))
            self.assertEqual(page["returned_chars"], len(page["content"]))
            self.assertLessEqual(page["returned_chars"], 17)
            self.assertFalse(page["network_request"])
            pieces.append(page["content"])
            offset = page["next_offset"]
        self.assertEqual("".join(pieces), text)
        eof = read_web_result(op, offset=len(text), max_chars=17)
        self.assertEqual(eof["content"], "")
        self.assertIsNone(eof["next_offset"])

    def test_long_urls_and_escaped_controls_do_not_break_projection_or_page_limits(self):
        op = operation(content="\x01" * 20000)
        result = json.loads(op["result_json"])
        url = "https://example.com/" + "界" * 8100
        result["page"].update(url=url, title="\x01" * 512, truncated=True)
        op["result_json"] = dumps(result)
        view = project_web_result(op)
        self.assertLessEqual(len(dumps(view)), MAX_PROJECTION_CHARS)
        self.assertTrue(view["page"]["source_url_omitted"])
        self.assertEqual(view["source_urls"], [])
        page = read_web_result(op, max_chars=6000)
        self.assertEqual(page["source"]["url"], url)
        self.assertLess(len(dumps(page).encode()), 65536)
        self.assertGreater(page["next_offset"], 0)
        self.assertTrue(page["stored_result_truncated"])

    def test_invalid_state_paging_and_nonweb_data_are_rejected(self):
        for changes in ({"status": "RUNNING"}, {"status": "FAILED"}, {"status": "UNKNOWN"},
                        {"tool_name": "read_file"}, {"result_json": "{}"}):
            with self.subTest(changes=changes), self.assertRaises(PlatformError):
                project_web_result(operation(**changes))
        for args in ({"offset": -1}, {"offset": True}, {"offset": 10000}, {"max_chars": 0},
                     {"max_chars": 6001}, {"max_chars": True}):
            with self.subTest(args=args), self.assertRaises(PlatformError):
                read_web_result(operation(), **args)


class EvidenceGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.policy = Policy(max_web_requests=1)
        self.gateway = ToolGateway(self.store, MemorySandbox(), self.policy)
        self.wf = self.store.create_workflow("shared public evidence", owner="researcher", policy=self.policy)
        self.store.route(self.wf["root_task_id"], "researcher")
        self.researcher = self.store.claim(self.wf["root_task_id"])

    def tearDown(self):
        self.store.close()

    def saved(self, task=None, *, status="SUCCEEDED", tool="fetch_web"):
        task = task or self.researcher
        op, _ = self.store.begin_operation(task["id"], task["active_attempt_id"], "save-" + tool, tool,
                                            {"url": "https://example.com/article"})
        return self.store.finish_operation(op["id"], json.loads(operation()["result_json"]), status=status)

    async def test_all_roles_and_reply_menus_can_read_when_provider_is_disabled(self):
        for agent in AGENTS.values():
            for kind in ("WORK", "REPLY"):
                names = {s["function"]["name"] for s in self.gateway.schemas(agent, kind)}
                self.assertIn("read_web_result", names)
                self.assertNotIn("search_web", names)
                self.assertNotIn("fetch_web", names)
        op = self.saved()
        content = await self.gateway.execute(self.researcher, "read_web_result", {"operation_id": op["id"]})
        self.assertEqual(content["content"], "完整內容 🙂")
        self.assertFalse(content["network_request"])
        self.assertEqual(self.store.one("SELECT COUNT(*) n FROM operations WHERE tool_name IN ('search_web','fetch_web')")["n"], 1)
        with self.assertRaises(PlatformError) as exhausted:
            self.store.begin_operation(self.researcher["id"], self.researcher["active_attempt_id"], "new-network", "fetch_web", {"url": "https://example.com/other"})
        self.assertEqual(exhausted.exception.code, "WEB_BUDGET_EXHAUSTED")

    async def test_parent_can_read_failed_childs_successful_public_evidence(self):
        # Tasks may fail after a successful fetch; the successful public result
        # remains evidence and does not require access to the child's snapshots.
        wf = self.store.create_workflow("parent workflow", owner="planner", policy=self.policy)
        self.store.route(wf["root_task_id"], "planner")
        parent = self.store.claim(wf["root_task_id"])
        with self.store.tx() as conn:
            self.store._insert_task(conn, "task_child", wf["id"], "fetch evidence", "researcher", [], [], parent=parent)
        self.store.route("task_child", "researcher")
        child = self.store.claim("task_child")
        op = self.saved(child)
        self.store.fail(child["id"], "fixture child failure")
        result = await self.gateway.execute(parent, "read_web_result", {"operation_id": op["id"]})
        self.assertEqual(result["content"], "完整內容 🙂")

    async def test_cross_workflow_nonweb_and_failed_operations_are_hidden(self):
        op = self.saved()
        other = self.store.create_workflow("private other workflow", owner="researcher", policy=self.policy)
        self.store.route(other["root_task_id"], "researcher")
        task = self.store.claim(other["root_task_id"])
        with self.assertRaises(PlatformError) as denied:
            await self.gateway.execute(task, "read_web_result", {"operation_id": op["id"]})
        self.assertEqual(denied.exception.code, "WEB_EVIDENCE_UNAVAILABLE")
        failed = self.saved(task, status="FAILED")
        private = self.saved(task, tool="read_file")
        for operation_id in (failed["id"], private["id"], "missing"):
            with self.subTest(operation_id=operation_id), self.assertRaises(PlatformError) as denied:
                await self.gateway.execute(task, "read_web_result", {"operation_id": operation_id})
            self.assertEqual(denied.exception.code, "WEB_EVIDENCE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
