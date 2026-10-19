import asyncio
import copy
import io
import json
import time
import unittest
from unittest.mock import Mock, patch

from maplatform.domain import PlatformError
from maplatform.provider import Completion, DeepSeekProvider, ScriptedProvider, _urllib_transport


TOOL = {"type": "function", "function": {"name": "read_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}
MESSAGES = [{"role": "user", "content": "Read the file."}]
SECRET = "fixture-key-never-display"


def call(call_id="tool-1", arguments='{"path":"main.py"}', name="read_file"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def assistant(tool_calls=None):
    message = {"role": "assistant", "content": None if tool_calls else "Done", "reasoning_content": "opaque reasoning preserved"}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return message


def response(message=None, finish_reason=None, usage=None):
    message = assistant() if message is None else message
    return json.dumps({"choices": [{"finish_reason": finish_reason or ("tool_calls" if message.get("tool_calls") else "stop"), "message": message}], "usage": {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22} if usage is None else usage}).encode()


class FixtureTransport:
    def __init__(self, body=None, status=200):
        self.body = response() if body is None else body
        self.status = status
        self.requests = []

    def __call__(self, url, payload, headers, timeout, limit):
        self.requests.append({"url": url, "payload": json.loads(payload), "headers": dict(headers), "timeout": timeout, "limit": limit})
        return self.status, self.body


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_contract_purposes_enable_json_mode_without_changing_tools(self):
        for purpose in ("AGENT", "BID", "FINAL_REPAIR", "PROBE"):
            transport = FixtureTransport(response(assistant([call()])))
            provider = DeepSeekProvider(SECRET, "explicit", transport=transport)
            await provider.complete(MESSAGES, [TOOL], task={}, purpose=purpose)
            payload = transport.requests[0]["payload"]
            self.assertEqual(payload["tools"], [TOOL])
            self.assertEqual(payload.get("response_format"), None if purpose == "PROBE" else {"type": "json_object"})

    async def test_all_assistant_fields_survive_full_history_and_result(self):
        earlier = assistant()
        earlier["provider_extension"] = {"opaque": True}
        previous_call = assistant([call("old")])
        history = MESSAGES + [earlier, {"role": "user", "content": "Read again."}, previous_call, {"role": "tool", "tool_call_id": "old", "content": "file content"}]
        output = assistant([call("new")])
        output["vendor_extension"] = {"reasoning_marker": "kept"}
        transport = FixtureTransport(response(output))
        provider = DeepSeekProvider(SECRET, "explicit-model", transport=transport)
        original = copy.deepcopy(history)
        completion = await provider.complete(history, [TOOL], task={"id": "t"})
        self.assertEqual(transport.requests[0]["payload"]["messages"], original)
        self.assertEqual(completion.message, output)
        self.assertEqual(completion.usage["total_tokens"], 22)
        self.assertEqual(history, original)
        self.assertFalse(transport.requests[0]["payload"]["stream"])
        self.assertEqual(transport.requests[0]["payload"]["thinking"], {"type": "enabled"})
        self.assertEqual(transport.requests[0]["payload"]["max_tokens"], 8192)
        self.assertEqual(transport.requests[0]["headers"]["Authorization"], "Bearer " + SECRET)
        completion.message["vendor_extension"]["reasoning_marker"] = "changed"
        self.assertEqual(output["vendor_extension"]["reasoning_marker"], "kept")

    async def test_http_statuses_are_classified_without_hidden_retries_or_body_leak(self):
        for status, code in ((400, "PROVIDER_HTTP"), (401, "PROVIDER_HTTP"), (302, "PROVIDER_HTTP"), (429, "PROVIDER_TRANSIENT"), (500, "PROVIDER_TRANSIENT"), (503, "PROVIDER_TRANSIENT")):
            with self.subTest(status=status):
                transport = FixtureTransport(SECRET.encode(), status=status)
                provider = DeepSeekProvider(SECRET, "explicit-model", transport=transport)
                with self.assertRaises(PlatformError) as caught:
                    await provider.complete(MESSAGES, [TOOL], task={})
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertEqual(len(transport.requests), 1)

    async def test_length_never_returns_partial_calls(self):
        transport = FixtureTransport(response(assistant([call()]), finish_reason="length"))
        with self.assertRaises(PlatformError) as caught:
            await DeepSeekProvider(SECRET, "explicit", transport=transport).complete(MESSAGES, [TOOL], task={})
        self.assertEqual(caught.exception.code, "PROVIDER_TRUNCATED")
        self.assertEqual(caught.exception.usage, {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22})

    async def test_interrupted_generation_preserves_accounting_without_partial_calls(self):
        for reason, code in (("insufficient_system_resource", "PROVIDER_TRANSIENT"), ("aborted", "PROVIDER_TRANSIENT"), ("content_filter", "PROVIDER_FILTERED")):
            with self.subTest(reason=reason):
                usage = {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22, "untrusted_extension": SECRET}
                # A partial call need not contain valid JSON, and must never be executed.
                transport = FixtureTransport(response(assistant([call(arguments="{")]), finish_reason=reason, usage=usage))
                with self.assertRaises(PlatformError) as caught:
                    await DeepSeekProvider(SECRET, "explicit", transport=transport).complete(MESSAGES, [TOOL], task={})
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.usage, {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22})
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertNotIn(SECRET, json.dumps(caught.exception.as_dict()))
                self.assertEqual(len(transport.requests), 1)

    async def test_http_authentication_and_balance_failures_are_actionable(self):
        for status, hint in ((401, "DEEPSEEK_API_KEY"), (402, "balance"), (404, "base URL"), (422, "parameters")):
            with self.subTest(status=status):
                with self.assertRaises(PlatformError) as caught:
                    await DeepSeekProvider(SECRET, "explicit", transport=FixtureTransport(SECRET.encode(), status=status)).complete(MESSAGES, [], task={})
                self.assertEqual(caught.exception.code, "PROVIDER_HTTP")
                self.assertIn(hint, str(caught.exception))
                self.assertNotIn(SECRET, str(caught.exception))

    async def test_per_request_token_caps_do_not_mutate_shared_provider(self):
        transport = FixtureTransport()
        provider = DeepSeekProvider(SECRET, "explicit", max_output_tokens=64, transport=transport)
        await asyncio.gather(
            provider.complete(MESSAGES, [], task={}, max_output_tokens=17),
            provider.complete(MESSAGES, [], task={}, max_output_tokens=128),
        )
        self.assertEqual(sorted(item["payload"]["max_tokens"] for item in transport.requests), [17, 64])
        self.assertEqual(provider.max_output_tokens, 64)
        for cap in (True, 0, -1, 1.5, 393217):
            with self.subTest(cap=cap):
                with self.assertRaises(PlatformError) as caught:
                    await provider.complete(MESSAGES, [], task={}, max_output_tokens=cap)
                self.assertEqual(caught.exception.code, "PROVIDER_REQUEST")
        self.assertEqual(len(transport.requests), 2)

    async def test_rejects_malformed_calls_and_unknown_tools(self):
        no_id = call()
        del no_id["id"]
        cases = [[no_id], [call(), call()], [call(arguments="[]")], [call(arguments="{")], [call(arguments='{"x":1,"x":2}')], [call(arguments='{"x":NaN}')], [call(arguments='{"x":1e999}')], [call(name="not_offered")]]
        for calls in cases:
            with self.subTest(calls=calls):
                transport = FixtureTransport(response(assistant(calls)))
                with self.assertRaises(PlatformError) as caught:
                    await DeepSeekProvider(SECRET, "explicit", transport=transport).complete(MESSAGES, [TOOL], task={})
                self.assertEqual(caught.exception.code, "PROVIDER_PROTOCOL")

    async def test_rejects_reused_history_call_id(self):
        history = MESSAGES + [assistant([call("old")]), {"role": "tool", "tool_call_id": "old", "content": "ok"}]
        transport = FixtureTransport(response(assistant([call("old")])))
        with self.assertRaises(PlatformError) as caught:
            await DeepSeekProvider(SECRET, "explicit", transport=transport).complete(history, [TOOL], task={})
        self.assertEqual(caught.exception.code, "PROVIDER_PROTOCOL")

    async def test_invalid_or_incomplete_tool_history_never_sends(self):
        cases = [MESSAGES + [assistant([call()])], MESSAGES + [{"role": "tool", "tool_call_id": "unknown", "content": "ok"}], MESSAGES + [assistant([call()]), {"role": "tool", "tool_call_id": "tool-1", "content": "ok"}, {"role": "tool", "tool_call_id": "tool-1", "content": "duplicate"}], MESSAGES + [assistant([call()]), {"role": "user", "content": "interrupt"}]]
        for history in cases:
            with self.subTest(history=history):
                transport = FixtureTransport()
                with self.assertRaises(PlatformError) as caught:
                    await DeepSeekProvider(SECRET, "explicit", transport=transport).complete(history, [TOOL], task={})
                self.assertEqual(caught.exception.code, "PROVIDER_REQUEST")
                self.assertFalse(transport.requests)

    async def test_response_and_context_limits_are_enforced(self):
        transport = FixtureTransport()
        with self.assertRaises(PlatformError) as caught:
            await DeepSeekProvider(SECRET, "explicit", max_context_chars=4, transport=transport).complete(MESSAGES, [TOOL], task={})
        self.assertEqual(caught.exception.code, "PROVIDER_CONTEXT_LIMIT")
        self.assertFalse(transport.requests)
        with self.assertRaises(PlatformError) as caught:
            await DeepSeekProvider(SECRET, "explicit", max_response_bytes=4, transport=transport).complete(MESSAGES, [TOOL], task={})
        self.assertEqual(caught.exception.code, "PROVIDER_RESPONSE_LIMIT")

    async def test_timeout_runs_in_thread_and_warns_about_unknown_billing(self):
        calls = []
        def slow(*args):
            calls.append(1)
            time.sleep(0.08)
            return 200, response()
        provider = DeepSeekProvider(SECRET, "explicit", timeout=0.02, transport=slow)
        started = time.monotonic()
        with self.assertRaises(PlatformError) as caught:
            await provider.complete(MESSAGES, [], task={})
        self.assertLess(time.monotonic() - started, 0.07)
        self.assertEqual(caught.exception.code, "PROVIDER_TRANSIENT")
        self.assertIn("billing", str(caught.exception))
        self.assertEqual(len(calls), 1)

    async def test_transport_exception_text_and_platform_errors_cannot_leak_key(self):
        for error in (RuntimeError(SECRET), PlatformError("CUSTOM", SECRET)):
            def broken(*args):
                raise error
            with self.assertRaises(PlatformError) as caught:
                await DeepSeekProvider(SECRET, "explicit", transport=broken).complete(MESSAGES, [], task={})
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)

    async def test_invalid_response_shapes_and_ambiguous_json_rejected(self):
        cases = [b"not JSON", b'{"choices":[],"choices":[]}', b'{"choices":[]}', response(assistant(), "unexpected"), response(assistant(), ["stop"]), response(assistant([call()]), "stop"), response(assistant(), usage={"total_tokens": -1})]
        for body in cases:
            with self.subTest(body=body):
                with self.assertRaises(PlatformError) as caught:
                    await DeepSeekProvider(SECRET, "explicit", transport=FixtureTransport(body)).complete(MESSAGES, [TOOL], task={})
                self.assertEqual(caught.exception.code, "PROVIDER_PROTOCOL")

    async def test_thinking_can_be_disabled_and_proxy_path_is_explicit(self):
        transport = FixtureTransport()
        provider = DeepSeekProvider(SECRET, "deployment-specific", base_url="https://gateway.example/v1/", thinking=False, transport=transport)
        await provider.complete(MESSAGES, [], task={})
        request = transport.requests[0]
        self.assertEqual(request["url"], "https://gateway.example/v1/chat/completions")
        self.assertEqual(request["payload"]["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", request["payload"])
        self.assertNotIn("tools", request["payload"])

    async def test_scripted_list_and_async_callable_are_detached(self):
        output = assistant([call()])
        provider = ScriptedProvider([Completion(output, {"total_tokens": 1})])
        completion = await provider.complete(MESSAGES, [TOOL], task={"id": "t"}, purpose="VERIFY")
        self.assertEqual(completion.message, output)
        self.assertEqual(provider.calls[0]["purpose"], "VERIFY")
        completion.message["content"] = "mutated"
        self.assertNotEqual(output["content"], "mutated")
        with self.assertRaises(PlatformError) as caught:
            await provider.complete(MESSAGES, [], task={})
        self.assertEqual(caught.exception.code, "PROVIDER_SCRIPT_EXHAUSTED")
        async def script(messages, tools, *, task, purpose):
            await asyncio.sleep(0)
            return {"role": "assistant", "content": task["id"] + purpose}
        result = await ScriptedProvider(script).complete(MESSAGES, [], task={"id": "T"}, purpose="BID")
        self.assertEqual(result.message["content"], "TBID")


class ConfigTests(unittest.TestCase):
    def test_env_requires_explicit_model(self):
        with self.assertRaises(PlatformError) as caught:
            DeepSeekProvider.from_env({"DEEPSEEK_API_KEY": SECRET})
        self.assertEqual(caught.exception.code, "PROVIDER_CONFIG")
        self.assertNotIn(SECRET, str(caught.exception))
        provider = DeepSeekProvider.from_env({"DEEPSEEK_API_KEY": SECRET, "DEEPSEEK_MODEL": "explicit", "DEEPSEEK_THINKING": "disabled"})
        self.assertEqual(provider.model, "explicit")
        self.assertFalse(provider.thinking)

    def test_insecure_or_credential_bearing_base_urls_rejected(self):
        for url in ("http://gateway.example", "https://user:password@gateway.example", "https://gateway.example/?api_key=secret", "https://gateway.example/#secret", "https://gateway.example:invalid", "https://"):
            with self.subTest(url=url):
                with self.assertRaises(PlatformError) as caught:
                    DeepSeekProvider(SECRET, "explicit", base_url=url)
                self.assertEqual(caught.exception.code, "PROVIDER_CONFIG")

    def test_invalid_limits_and_env_values_rejected(self):
        for overrides in ({"timeout": 0}, {"timeout": float("nan")}, {"timeout": 301}, {"max_output_tokens": True}, {"max_output_tokens": 393217}, {"max_context_chars": -1}, {"max_response_bytes": 0}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(PlatformError):
                    DeepSeekProvider(SECRET, "explicit", **overrides)
        for env in ({"DEEPSEEK_TIMEOUT_SECONDS": "broken"}, {"DEEPSEEK_THINKING": "maybe"}):
            with self.assertRaises(PlatformError):
                DeepSeekProvider.from_env({"DEEPSEEK_API_KEY": SECRET, "DEEPSEEK_MODEL": "explicit", **env})


class HttpTransportTests(unittest.TestCase):
    def test_whitespace_keepalives_are_retained_for_valid_json_parsing(self):
        body = b"\n \n" + response()
        stream = io.BytesIO(body)
        stream.headers = {}
        stream.status = 200
        opener = Mock()
        opener.open.return_value = stream
        with patch("maplatform.provider.urllib.request.build_opener", return_value=opener):
            status, result = _urllib_transport("https://api.example/chat/completions", b"{}", {}, 1, 4096)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(result), json.loads(body))
        self.assertTrue(stream.closed)

    def test_keepalive_body_obeys_total_deadline_between_socket_reads(self):
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        stream.headers = {}
        stream.read1.return_value = b"\n"
        opener = Mock()
        opener.open.return_value = stream
        with patch("maplatform.provider.urllib.request.build_opener", return_value=opener), patch("maplatform.provider.time.monotonic", side_effect=[0.0, 0.1, 0.6, 1.1]):
            with self.assertRaises(TimeoutError):
                _urllib_transport("https://api.example/chat/completions", b"{}", {}, 1, 4096)
        self.assertEqual(stream.read1.call_count, 2)
        stream.__exit__.assert_called_once()

    def test_chunked_body_cannot_exceed_response_limit(self):
        stream = io.BytesIO(b"x" * 100)
        stream.headers = {}
        stream.status = 200
        opener = Mock()
        opener.open.return_value = stream
        with patch("maplatform.provider.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(PlatformError) as caught:
                _urllib_transport("https://api.example/chat/completions", b"{}", {}, 1, 10)
        self.assertEqual(caught.exception.code, "PROVIDER_RESPONSE_LIMIT")
        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
