"""Offline DeepSeek Search/Fetch contracts; no network or billable requests."""
import json
import time
import unittest
from unittest.mock import patch

from maplatform.domain import AGENTS, PlatformError, Policy, dumps
from maplatform.demo import tool
from maplatform.provider import ScriptedProvider
from maplatform.runtime import Runtime
from maplatform.store import Store
from maplatform.tools import MemorySandbox, ToolGateway, MAX_OUTPUT_BYTES
from maplatform.web import DeepSeekWebProvider, public_url, _transport


KEY = "sk-offline-web-fixture-secret"


def entry(**changes):
    return {"title": "Fixture title", "url": "https://example.com/source", "snippet": "Short evidence",
            "summary": "Longer evidence", "content": "", "rerank_score": 2.1, **changes}


class FixtureTransport:
    def __init__(self, payload=None, status=200, exception=None):
        self.payload = payload if payload is not None else {"results": [entry()]}
        self.status = status
        self.exception = exception
        self.calls = []

    def __call__(self, url, payload, headers, timeout, limit):
        self.calls.append((url, json.loads(payload), dict(headers), timeout, limit))
        if self.exception:
            raise self.exception
        body = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()
        return self.status, body


class WebProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_uses_official_endpoint_and_preserves_sources(self):
        transport = FixtureTransport()
        provider = DeepSeekWebProvider(KEY, transport=transport)
        result = await provider.search("官方搜尋 🔍", limit=5)
        url, body, headers, _, _ = transport.calls[0]
        self.assertEqual(url, "https://api.deepseek.com/v1/tools/search")
        self.assertEqual(body, {"query": "官方搜尋 🔍", "count": 5, "need_content": False})
        self.assertEqual(headers["Authorization"], "Bearer " + KEY)
        self.assertEqual(result[0]["url"], "https://example.com/source")
        self.assertEqual(result[0]["summary"], "Longer evidence")
        self.assertTrue(result[0]["untrusted"])
        self.assertNotIn(KEY, dumps(result))
        self.assertEqual(len(transport.calls), 1)

    async def test_search_preserves_optional_freshness_and_quality_metadata(self):
        metadata = {"date": "2026-10-19T00:00:00Z", "site_name": "Official source", "rerank_score": 2.5, "authority": 3}
        provider = DeepSeekWebProvider(KEY, transport=FixtureTransport({"results": [entry(**metadata)]}))
        result = (await provider.search("recent evidence"))[0]
        self.assertEqual({key: result[key] for key in metadata}, metadata)
        for metadata in ({"date": 123}, {"site_name": []}, {"rerank_score": True},
                         {"rerank_score": 3.1}, {"authority": 1.0}, {"authority": 5}):
            with self.subTest(metadata=metadata), self.assertRaises(PlatformError) as invalid:
                await DeepSeekWebProvider(KEY, transport=FixtureTransport({"results": [entry(**metadata)]})).search("test")
            self.assertEqual(invalid.exception.code, "WEB_PROTOCOL")
        result = await DeepSeekWebProvider(KEY, transport=FixtureTransport({"results": [entry(date=None, site_name=None)]})).search("test")
        self.assertIsNone(result[0]["date"])
        self.assertIsNone(result[0]["site_name"])

    async def test_per_request_timeout_overrides_constructor_without_mutation(self):
        transport = FixtureTransport()
        provider = DeepSeekWebProvider(KEY, transport=transport, timeout=1)
        await provider.search("test", timeout=7)
        self.assertEqual(transport.calls[0][3], 7)
        self.assertEqual(provider.timeout, 1)
        transport.payload = {"title": "Fixture", "content": "Evidence"}
        await provider.fetch("https://example.com/", timeout=9)
        self.assertEqual(transport.calls[1][3], 9)
        self.assertEqual(provider.timeout, 1)
        for timeout in (True, 0, 121, float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(PlatformError):
                await provider.search("test", timeout=timeout)
        self.assertEqual(len(transport.calls), 2)

    async def test_fetch_returns_markdown_and_original_url(self):
        transport = FixtureTransport({"title": "Page", "content": "# Evidence\n\nA factual claim."})
        provider = DeepSeekWebProvider(KEY, transport=transport)
        result = await provider.fetch("https://example.com/article?q=topic")
        self.assertEqual(transport.calls[0][0], "https://api.deepseek.com/v1/tools/fetch")
        self.assertEqual(transport.calls[0][1], {"url": "https://example.com/article?q=topic"})
        self.assertEqual(result["content"], "# Evidence\n\nA factual claim.")
        self.assertEqual(result["url"], "https://example.com/article?q=topic")
        self.assertFalse(result["truncated"])
        self.assertTrue(result["untrusted"])

    async def test_fetch_rejects_missing_or_blank_page_content(self):
        for body, code in (({"title": "Page"}, "WEB_PROTOCOL"),
                           ({"title": "Page", "content": ""}, "WEB_EMPTY_CONTENT"),
                           ({"title": "Page", "content": " \n\t"}, "WEB_EMPTY_CONTENT")):
            with self.subTest(body=body), self.assertRaises(PlatformError) as failure:
                await DeepSeekWebProvider(KEY, transport=FixtureTransport(body)).fetch("https://example.com/")
            self.assertEqual(failure.exception.code, code)

    async def test_query_unicode_limit_and_count_reject_without_io(self):
        transport = FixtureTransport()
        provider = DeepSeekWebProvider(KEY, transport=transport)
        for query, limit in (("", 5), (" \t", 5), ("中" * 129, 5), ("\ud800", 5),
                             ("valid", True), ("valid", 0), ("valid", 51), ("\x00x", 5)):
            with self.subTest(query=repr(query), limit=limit), self.assertRaises(PlatformError) as raised:
                await provider.search(query, limit)
            self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(transport.calls, [])
        await provider.search("中" * 128, 50)
        self.assertEqual(len(transport.calls), 1)

    async def test_nonpublic_fetch_targets_rejected_without_io(self):
        transport = FixtureTransport()
        provider = DeepSeekWebProvider(KEY, transport=transport)
        bad_urls = ["file:///etc/passwd", "http://localhost/", "http://localhost./", "http://host.local/a",
                    "http://metadata.google.internal/", "http://127.0.0.1/", "http://127.1/",
                    "http://2130706433/", "http://0x7f000001/", "http://10.0.0.1/", "http://100.64.0.1/",
                    "http://169.254.169.254/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                    "http://[fc00::1]/", "http://[fe80::1%25eth0]/", "http://user:pass@example.com/",
                    "https://@example.com/", "https://example.com:bad/", "https://example.com/\nX-Test:x",
                    "https://example.com\\@127.0.0.1/", "https://%31%32%37.0.0.1/", "http://intranet/",
                    "https://example.com/" + "a" * 8192]
        for url in bad_urls:
            with self.subTest(url=url), self.assertRaises(PlatformError) as raised:
                await provider.fetch(url)
            self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(transport.calls, [])
        for url in ("https://example.com/資料", "http://8.8.8.8/", "https://[2606:4700:4700::1111]/"):
            self.assertEqual(public_url(url), url)

    async def test_http_status_errors_never_echo_body_or_retry(self):
        expected = {301: "WEB_REDIRECT", 307: "WEB_REDIRECT", 400: "WEB_REQUEST", 401: "WEB_AUTH",
                    402: "WEB_BALANCE", 403: "WEB_FORBIDDEN", 404: "WEB_NOT_FOUND", 408: "WEB_TIMEOUT",
                    413: "WEB_REQUEST", 429: "WEB_RATE_LIMIT", 500: "WEB_UNAVAILABLE", 504: "WEB_UNAVAILABLE",
                    418: "WEB_HTTP"}
        for status, code in expected.items():
            transport = FixtureTransport({"error": {"message": KEY}}, status=status)
            with self.subTest(status=status), self.assertRaises(PlatformError) as raised:
                await DeepSeekWebProvider(KEY, transport=transport).search("test")
            self.assertEqual(raised.exception.code, code)
            self.assertNotIn(KEY, str(raised.exception))
            self.assertEqual(len(transport.calls), 1)

    async def test_transport_exceptions_are_sanitized(self):
        for failure in (RuntimeError(KEY), PlatformError("CUSTOM", KEY), TimeoutError(KEY)):
            transport = FixtureTransport(exception=failure)
            with self.subTest(failure=type(failure)), self.assertRaises(PlatformError) as raised:
                await DeepSeekWebProvider(KEY, transport=transport).search("test")
            self.assertNotIn(KEY, str(raised.exception))
            self.assertEqual(len(transport.calls), 1)

    async def test_success_response_cannot_echo_credentials(self):
        transport = FixtureTransport({"results": [entry(summary="Unexpected " + KEY)]})
        with self.assertRaises(PlatformError) as raised:
            await DeepSeekWebProvider(KEY, transport=transport).search("test")
        self.assertEqual(raised.exception.code, "WEB_PROTOCOL")
        self.assertNotIn(KEY, str(raised.exception))

    async def test_request_timeout_is_bounded_without_retries(self):
        def slow(*args):
            time.sleep(0.06)
            return 200, b'{"results":[]}'
        started = time.monotonic()
        with self.assertRaises(PlatformError) as raised:
            await DeepSeekWebProvider(KEY, transport=slow, timeout=0.01).search("test")
        self.assertEqual(raised.exception.code, "WEB_TIMEOUT")
        self.assertLess(time.monotonic() - started, 0.05)

    async def test_oversized_invalid_and_ambiguous_json_rejected(self):
        invalid = [b'{"results":[],"results":[]}', b'{"results":NaN}', b'{"results":[],"score":1e999}',
                   b'not JSON', b'[]', b'{"wrong":[]}', b'{"results":[null]}',
                   json.dumps({"results": [entry(url="http://localhost/")]}).encode(),
                   json.dumps({"results": [entry(summary=None)]}).encode()]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(PlatformError) as raised:
                await DeepSeekWebProvider(KEY, transport=FixtureTransport(raw)).search("test")
            self.assertEqual(raised.exception.code, "WEB_PROTOCOL")
        with self.assertRaises(PlatformError) as raised:
            await DeepSeekWebProvider(KEY, max_response_bytes=1024,
                                      transport=FixtureTransport(b" " * 1025)).search("test")
        self.assertEqual(raised.exception.code, "WEB_RESPONSE_LIMIT")

    async def test_search_and_fetch_truncation_is_explicit_and_json_bounded(self):
        huge = "內容" * 60_000
        search = DeepSeekWebProvider(KEY, transport=FixtureTransport({"results": [entry(summary=huge)] * 50}),
                                    max_response_bytes=8_388_608)
        # Keep raw fixture below cap while retaining enough long results to
        # exceed the model-facing output budget.
        search._transport.payload = {"results": [entry(summary="x" * 8000)] * 50}
        result = await search.search("test", 50)
        self.assertLess(len(result), 50)
        self.assertTrue(result[0]["truncated"])
        self.assertIn("summary", result[0]["truncated_fields"])
        self.assertTrue(result[-1]["results_truncated"])
        self.assertLess(len(dumps(result).encode()), MAX_OUTPUT_BYTES)
        for content in (huge, "\x01" * 90_000):
            fetch = DeepSeekWebProvider(KEY, transport=FixtureTransport({"title": "Page", "content": content}))
            page = await fetch.fetch("https://example.com/")
            self.assertTrue(page["truncated"])
            self.assertEqual(page["content_bytes"], len(content.encode()))
            self.assertEqual(page["returned_content_bytes"], len(page["content"].encode()))
            self.assertLess(page["returned_content_bytes"], page["content_bytes"])
            self.assertLess(len(dumps(page).encode()), MAX_OUTPUT_BYTES)

    async def test_configuration_env_key_and_invalid_limits(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": KEY}):
            self.assertEqual(DeepSeekWebProvider()._api_key, KEY)
        for options in ({"api_key": ""}, {"api_key": "secret\nleak"}, {"timeout": True},
                        {"timeout": float("nan")}, {"timeout": 121}, {"max_response_bytes": True},
                        {"max_response_bytes": 100}):
            with self.subTest(options=options), self.assertRaises(PlatformError):
                DeepSeekWebProvider(**{"api_key": KEY, **options})


class WebTransportTests(unittest.TestCase):
    def test_redirect_response_does_not_connect_to_second_host(self):
        class Response:
            status = 307
            def __enter__(self): return self
            def __exit__(self, *args): pass
        class Connection:
            sock = None
            def request(self, *args, **kwargs): self.requested = (args, kwargs)
            def getresponse(self): return Response()
            def close(self): self.closed = True
        connection = Connection()
        with patch("maplatform.web.http.client.HTTPSConnection", return_value=connection) as constructor:
            status, body = _transport(DeepSeekWebProvider.SEARCH_ENDPOINT, b"{}", {"Authorization": "Bearer " + KEY}, 1, 1024)
        self.assertEqual((status, body), (307, b""))
        constructor.assert_called_once_with("api.deepseek.com", timeout=1)
        self.assertTrue(connection.closed)


class WebGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.gateway = ToolGateway(self.store, MemorySandbox(), Policy())
        workflow = self.store.create_workflow("web fixture", owner="researcher")
        self.store.route(workflow["root_task_id"], "researcher")
        self.task = self.store.claim(workflow["root_task_id"])

    def tearDown(self):
        self.store.close()

    async def test_fetch_hidden_for_unconfigured_or_search_only_backend(self):
        for backend in (None, lambda query, limit: []):
            self.gateway.search = backend
            names = {s["function"]["name"] for s in self.gateway.schemas(AGENTS["researcher"])}
            self.assertNotIn("fetch_web", names)
            with self.assertRaises(PlatformError) as raised:
                await self.gateway.execute(self.task, "fetch_web", {"url": "https://example.com/"})
            self.assertEqual(raised.exception.code, "TOOL_UNAVAILABLE")

    async def test_backend_schema_matches_official_query_limit(self):
        transport = FixtureTransport()
        self.gateway.search = DeepSeekWebProvider(KEY, transport=transport)
        schemas = {s["function"]["name"]: s for s in self.gateway.schemas(AGENTS["researcher"], "REPLY")}
        self.assertIn("fetch_web", schemas)
        props = schemas["search_web"]["function"]["parameters"]["properties"]
        self.assertEqual(props["query"]["maxLength"], 128)
        self.assertEqual(props["limit"]["maximum"], 50)
        with self.assertRaises(PlatformError):
            await self.gateway.execute(self.task, "search_web", {"query": "x" * 129})
        self.assertEqual(transport.calls, [])
        result = await self.gateway.execute(self.task, "search_web", {"query": "valid"})
        self.assertTrue(result["untrusted"])

    async def test_fetch_result_is_bounded_and_marked_untrusted(self):
        self.gateway.search = DeepSeekWebProvider(KEY, transport=FixtureTransport({"title": "Page", "content": "Evidence"}))
        result = await self.gateway.execute(self.task, "fetch_web", {"url": "https://example.com/"})
        self.assertEqual(result["page"]["url"], "https://example.com/")
        self.assertTrue(result["untrusted"])
        self.assertIn("not instructions", result["notice"])

    async def test_gateway_uses_saved_timeout_after_provider_recreated(self):
        transport = FixtureTransport()
        provider = DeepSeekWebProvider(KEY, transport=transport, timeout=1)
        self.gateway.search = provider
        await self.gateway.execute(self.task, "search_web", {"query": "test"})
        self.assertEqual(transport.calls[0][3], Policy().tool_timeout)
        self.assertEqual(provider.timeout, 1)

    async def test_runtime_enforces_saved_budget_before_external_io_and_replay(self):
        with self.store.tx() as conn:
            conn.execute("UPDATE workflows SET policy_json=? WHERE id=?",
                         (dumps({"max_web_requests": 1}), self.task["workflow_id"]))
        transport = FixtureTransport()
        self.gateway.search = DeepSeekWebProvider(KEY, transport=transport)
        runtime = Runtime(self.store, ScriptedProvider([]), self.gateway)
        call = tool("one", "search_web", {"query": "test"})
        first = await runtime.invoke(self.task, call)
        self.assertIn("results", first)
        replay = await runtime.invoke(self.task, call)
        self.assertEqual(first, replay)
        denied = await runtime.invoke(self.task, tool("two", "fetch_web", {"url": "https://example.com/"}))
        self.assertEqual(denied["error"]["code"], "WEB_BUDGET_EXHAUSTED")
        self.assertEqual(len(transport.calls), 1)

    async def test_memory_search_is_scoped_to_current_session(self):
        other = self.store.create_workflow("other session", owner="researcher")
        prior = self.store.create_workflow("prior turn", owner="researcher")
        with self.store.tx() as conn:
            for session in ("first", "second"):
                conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)",
                             (session, "human:local", "fixture", "researcher", time.time(), time.time()))
            for ordinal, workflow_id, session in ((1, prior["id"], "first"),
                                                  (2, self.task["workflow_id"], "first"),
                                                  (1, other["id"], "second")):
                conn.execute("INSERT INTO session_turns(id,session_id,ordinal,workflow_id,owner,user_text,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                             (workflow_id, session, ordinal, workflow_id, "researcher", "fixture", "COMPLETED", time.time(), time.time()))
                conn.execute("INSERT INTO memories VALUES(?,?,?,?,?,?)",
                             (workflow_id, "researcher", "human:local", workflow_id, session + " evidence", time.time()))
        result = await self.gateway.execute(self.task, "search_memory", {"query": "evidence"})
        self.assertEqual({row["workflow_id"] for row in result["memories"]}, {prior["id"], self.task["workflow_id"]})
        legacy = self.store.create_workflow("legacy", owner="researcher")
        self.store.route(legacy["root_task_id"], "researcher")
        legacy_task = self.store.claim(legacy["root_task_id"])
        result = await self.gateway.execute(legacy_task, "search_memory", {"query": "evidence"})
        self.assertEqual(result["memories"], [])


class WebBudgetTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def task(self, maximum):
        workflow = self.store.create_workflow("budget fixture", owner="researcher", policy=Policy(max_web_requests=maximum))
        self.store.route(workflow["root_task_id"], "researcher")
        return self.store.claim(workflow["root_task_id"])

    def test_reserved_failed_and_unknown_requests_consume_budget(self):
        task = self.task(2)
        first, run = self.store.begin_operation(task["id"], task["active_attempt_id"], "search1", "search_web", {"query": "test"})
        self.assertTrue(run)
        self.store.finish_operation(first["id"], {"error": {"code": "WEB_TIMEOUT"}}, status="UNKNOWN")
        second, _ = self.store.begin_operation(task["id"], task["active_attempt_id"], "fetch1", "fetch_web", {"url": "https://example.com/"})
        self.store.finish_operation(second["id"], {"error": {"code": "WEB_NOT_FOUND"}}, status="FAILED")
        with self.assertRaises(PlatformError) as raised:
            self.store.begin_operation(task["id"], task["active_attempt_id"], "search2", "search_web", {"query": "retry"})
        self.assertEqual(raised.exception.code, "WEB_BUDGET_EXHAUSTED")
        self.assertEqual(len(self.store.all("SELECT * FROM operations")), 2)
        cached, run = self.store.begin_operation(task["id"], task["active_attempt_id"], "search1", "search_web", {"query": "test"})
        self.assertFalse(run)
        self.assertEqual(cached["id"], first["id"])

    def test_zero_budget_fails_before_reservation_other_tools_continue(self):
        task = self.task(0)
        with self.assertRaises(PlatformError) as raised:
            self.store.begin_operation(task["id"], task["active_attempt_id"], "search", "search_web", {"query": "test"})
        self.assertEqual(raised.exception.code, "WEB_BUDGET_EXHAUSTED")
        _, run = self.store.begin_operation(task["id"], task["active_attempt_id"], "memory", "search_memory", {"query": "test"})
        self.assertTrue(run)


if __name__ == "__main__":
    unittest.main()
