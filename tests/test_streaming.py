import asyncio
import copy
import io
import json
import threading
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

from maplatform.domain import PlatformError
from maplatform.provider import CompletionError, DeepSeekProvider, _urllib_stream_transport


MESSAGES = [{"role": "user", "content": "Read both files."}]
TOOL = {"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}
USAGE = {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22}
SECRET = "fixture-private-key"
DONE = b"data: [DONE]\n\n"


def event(delta=None, finish=None, usage=None):
    value = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        value["usage"] = usage
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()


def fragment(index=0, call_id="call-1", arguments="", name="read_file"):
    return {"index": index, "id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class FixtureStream:
    def __init__(self, chunks, status=200):
        self.chunks = chunks
        self.status = status
        self.requests = []
        self.closed = False

    async def __call__(self, url, payload, headers, timeout, limit):
        self.requests.append({"url": url, "payload": json.loads(payload), "headers": dict(headers), "timeout": timeout, "limit": limit})
        try:
            for chunk in self.chunks:
                await asyncio.sleep(0)
                yield self.status, chunk
        finally:
            self.closed = True


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_json_contract_matches_nonstreaming(self):
        for purpose in ("AGENT", "BID", "FINAL_REPAIR", "PROBE"):
            transport = FixtureStream([event({"content": "{}"}, "stop", USAGE), DONE])
            provider = DeepSeekProvider(SECRET, "explicit", stream_transport=transport)
            await provider.stream_complete(MESSAGES, [TOOL], task={}, purpose=purpose)
            payload = transport.requests[0]["payload"]
            self.assertEqual(payload["tools"], [TOOL])
            self.assertEqual(payload.get("response_format"), None if purpose == "PROBE" else {"type": "json_object"})

    async def test_live_delta_arrives_before_stream_can_finish_on_caller_loop(self):
        saw_delta = asyncio.Event()
        callback_threads = []
        thread = threading.get_ident()
        chunks = []

        async def transport(*args):
            yield 200, event({"content": "First "})
            # Completion would time out if the provider buffered the entire
            # response before invoking the synchronous observer.
            await saw_delta.wait()
            yield 200, event({"content": "answer"}, "stop", USAGE)
            yield 200, DONE

        def observer(delta):
            chunks.append(delta)
            callback_threads.append(threading.get_ident())
            saw_delta.set()

        provider = DeepSeekProvider(SECRET, "explicit", timeout=0.3, stream_transport=transport)
        result = await provider.stream_complete(MESSAGES, [], task={}, on_delta=observer)
        self.assertEqual(result.message, {"role": "assistant", "content": "First answer"})
        self.assertEqual(chunks, [{"kind": "content", "text": "First "}, {"kind": "content", "text": "answer"}])
        self.assertEqual(callback_threads, [thread, thread])
        self.assertEqual(result.usage, USAGE)

    async def test_interleaved_tools_utf8_reasoning_and_every_byte_boundary(self):
        body = b": keep-alive\r\n\r\n" + b"".join([
            event({"role": "assistant", "reasoning_content": "秘密思考"}),
            event({"reasoning_content": " kept", "content": "讀檔"}),
            event({"tool_calls": [fragment(1, "second", '{"path":'), fragment(0, "first", '{"path":"')] }),
            event({"tool_calls": [{"index": 0, "function": {"arguments": "一.py\"}"}}, {"index": 1, "function": {"arguments": '"two.py"}'}}]}),
            event(finish="tool_calls", usage=USAGE), DONE,
        ])
        transport = FixtureStream([bytes([byte]) for byte in body])
        notifications = []
        provider = DeepSeekProvider(SECRET, "explicit", stream_transport=transport)
        result = await provider.stream_complete(MESSAGES, [TOOL], task={}, on_delta=notifications.append)
        self.assertEqual(result.message["reasoning_content"], "秘密思考 kept")
        self.assertEqual(result.message["content"], "讀檔")
        self.assertEqual([item["id"] for item in result.message["tool_calls"]], ["first", "second"])
        self.assertEqual([json.loads(item["function"]["arguments"]) for item in result.message["tool_calls"]], [{"path": "一.py"}, {"path": "two.py"}])
        self.assertEqual(notifications, [{"kind": "reasoning", "chars": 4}, {"kind": "content", "text": "讀檔"}, {"kind": "reasoning", "chars": 5}])
        self.assertNotIn("秘密", json.dumps(notifications, ensure_ascii=False))
        self.assertNotIn("path", json.dumps(notifications))
        self.assertTrue(transport.closed)

    async def test_history_payload_and_per_request_limit_are_preserved(self):
        history = MESSAGES + [{"role": "assistant", "content": "prior", "reasoning_content": "private", "vendor": {"preserved": 1}}, {"role": "user", "content": "next"}]
        original = copy.deepcopy(history)
        transport = FixtureStream([event({"content": "ok"}, "stop", USAGE), DONE])
        provider = DeepSeekProvider(SECRET, "explicit", stream_transport=transport, max_output_tokens=32)
        await provider.stream_complete(history, [TOOL], task={}, max_output_tokens=8)
        request = transport.requests[0]
        self.assertEqual(request["payload"]["messages"], original)
        self.assertEqual(history, original)
        self.assertTrue(request["payload"]["stream"])
        self.assertEqual(request["payload"]["stream_options"], {"include_usage": True})
        self.assertEqual(request["payload"]["max_tokens"], 8)
        self.assertEqual(request["headers"]["Accept"], "text/event-stream")
        self.assertEqual(provider.max_output_tokens, 32)

    async def test_usage_only_frame_after_finish_is_accepted(self):
        transport = FixtureStream([event({"content": "ok"}, "stop"), b'data: {"choices":[],"usage":{"total_tokens":22}}\n\n', DONE])
        result = await DeepSeekProvider(SECRET, "explicit", stream_transport=transport).stream_complete(MESSAGES, [], task={})
        self.assertEqual(result.usage, {"total_tokens": 22})

    async def test_bare_cr_sse_boundary_is_flushed_on_eof(self):
        body = (event({"content": "ok"}, "stop", USAGE) + DONE).replace(b"\n", b"\r")
        result = await DeepSeekProvider(SECRET, "explicit", stream_transport=FixtureStream([body])).stream_complete(MESSAGES, [], task={})
        self.assertEqual(result.message["content"], "ok")

    async def test_truncation_filter_and_interruption_never_return_partial_calls(self):
        for finish, expected in (("length", "PROVIDER_TRUNCATED"), ("content_filter", "PROVIDER_FILTERED"), ("aborted", "PROVIDER_TRANSIENT"), ("insufficient_system_resource", "PROVIDER_TRANSIENT")):
            with self.subTest(finish=finish):
                transport = FixtureStream([event({"tool_calls": [fragment(arguments="{")]}, finish, {**USAGE, "extension": SECRET}), DONE])
                with self.assertRaises(CompletionError) as caught:
                    await DeepSeekProvider(SECRET, "explicit", stream_transport=transport).stream_complete(MESSAGES, [TOOL], task={})
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(caught.exception.usage, USAGE)
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertTrue(transport.closed)

    async def test_missing_done_preserves_known_usage_and_never_returns_calls(self):
        transport = FixtureStream([event({"tool_calls": [fragment(arguments="{}")]}, "tool_calls", USAGE)])
        with self.assertRaises(CompletionError) as caught:
            await DeepSeekProvider(SECRET, "explicit", stream_transport=transport).stream_complete(MESSAGES, [TOOL], task={})
        self.assertEqual(caught.exception.code, "PROVIDER_TRUNCATED")
        self.assertEqual(caught.exception.usage, USAGE)

    async def test_malformed_streams_fail_without_exposing_output(self):
        cases = [
            [DONE],
            [event({"content": "ok"}), DONE],
            [b'data: {"choices":[],"choices":[]}\n\n'],
            [event({"content": [SECRET]})],
            [event({"role": "tool"})],
            [event({"role": []})],
            [event({"tool_calls": [fragment(arguments="{")]}, "tool_calls"), DONE],
            [event({"tool_calls": [fragment(arguments="[]")]}, "tool_calls"), DONE],
            [event({"tool_calls": [fragment(index=2, arguments="{}")]}, "tool_calls"), DONE],
            [event({"tool_calls": [fragment(arguments="{}", name="not_offered")]}, "tool_calls"), DONE],
            [event({"tool_calls": [fragment(arguments="{}")]}, "stop"), DONE],
            [event({"content": "ok"}, "tool_calls"), DONE],
            [event({"content": "ok"}, "stop"), event({"content": "extra"}), DONE],
            [event({"content": "ok"}, "stop"), event(finish="stop"), DONE],
            [event({"content": "ok"}, "unknown"), DONE],
            [b'data: {"choices":[{"index":1,"delta":{}}]}\n\n'],
            [event({"content": "ok"}, "stop", {"total_tokens": True}), DONE],
            [b'data: \xff\n\n'],
        ]
        for chunks in cases:
            with self.subTest(chunks=chunks):
                transport = FixtureStream(chunks)
                with self.assertRaises(CompletionError) as caught:
                    await DeepSeekProvider(SECRET, "explicit", stream_transport=transport).stream_complete(MESSAGES, [TOOL], task={})
                self.assertEqual(caught.exception.code, "PROVIDER_PROTOCOL")
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertTrue(transport.closed)

    async def test_tool_identity_and_history_id_must_not_change_or_repeat(self):
        history = MESSAGES + [{"role": "assistant", "content": None, "tool_calls": [{key: value for key, value in fragment(call_id="old", arguments="{}").items() if key != "index"}]}, {"role": "tool", "tool_call_id": "old", "content": "ok"}]
        cases = [
            [event({"tool_calls": [fragment(call_id="old", arguments="{}")]}, "tool_calls"), DONE],
            [event({"tool_calls": [fragment(arguments="{")]}), event({"tool_calls": [{"index": 0, "id": "changed", "function": {"arguments": "}"}}]}, "tool_calls"), DONE],
            [event({"tool_calls": [fragment(arguments="{}"), fragment(index=1, arguments="{}")]}, "tool_calls"), DONE],
        ]
        for chunks in cases:
            with self.subTest(chunks=chunks):
                with self.assertRaises(CompletionError) as caught:
                    await DeepSeekProvider(SECRET, "explicit", stream_transport=FixtureStream(chunks)).stream_complete(history, [TOOL], task={})
                self.assertEqual(caught.exception.code, "PROVIDER_PROTOCOL")

    async def test_timeout_closes_stream_keeps_usage_and_has_no_late_callbacks(self):
        closed = asyncio.Event()
        notifications = []

        async def transport(*args):
            try:
                yield 200, event({"content": "partial"}, usage=USAGE)
                await asyncio.sleep(10)
                yield 200, event({"content": "must not appear"}, "stop")
            finally:
                closed.set()

        provider = DeepSeekProvider(SECRET, "explicit", timeout=0.03, stream_transport=transport)
        started = time.monotonic()
        with self.assertRaises(CompletionError) as caught:
            await provider.stream_complete(MESSAGES, [], task={}, on_delta=notifications.append)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(caught.exception.code, "PROVIDER_TRANSIENT")
        self.assertIn("billing", str(caught.exception))
        self.assertEqual(caught.exception.usage, USAGE)
        self.assertTrue(closed.is_set())
        self.assertEqual(notifications, [{"kind": "content", "text": "partial"}])

    async def test_cancellation_closes_stream_and_retains_known_accounting(self):
        observed = asyncio.Event()
        closed = asyncio.Event()

        async def transport(*args):
            try:
                yield 200, event({"content": "partial"}, usage=USAGE)
                observed.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

        provider = DeepSeekProvider(SECRET, "explicit", stream_transport=transport)
        request = asyncio.create_task(provider.stream_complete(MESSAGES, [], task={}))
        await observed.wait()
        request.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await request
        self.assertTrue(closed.is_set())
        self.assertEqual(caught.exception.usage, USAGE)

    async def test_byte_limit_after_usage_preserves_counters(self):
        chunk = event({"content": "ok"}, usage=USAGE)
        transport = FixtureStream([chunk, b"x" * 100])
        with self.assertRaises(CompletionError) as caught:
            await DeepSeekProvider(SECRET, "explicit", max_response_bytes=len(chunk) + 10, stream_transport=transport).stream_complete(MESSAGES, [], task={})
        self.assertEqual(caught.exception.code, "PROVIDER_RESPONSE_LIMIT")
        self.assertEqual(caught.exception.usage, USAGE)
        self.assertTrue(transport.closed)

    async def test_bad_request_is_rejected_before_transport(self):
        for overrides in ({"max_output_tokens": 0}, {"on_delta": "not callable"}):
            transport = FixtureStream([])
            with self.assertRaises(PlatformError) as caught:
                await DeepSeekProvider(SECRET, "explicit", stream_transport=transport).stream_complete(MESSAGES, [], task={}, **overrides)
            self.assertEqual(caught.exception.code, "PROVIDER_REQUEST")
            self.assertFalse(transport.requests)
        transport = FixtureStream([])
        with self.assertRaises(PlatformError) as caught:
            await DeepSeekProvider(SECRET, "explicit", stream_transport=transport, max_context_chars=1).stream_complete(MESSAGES, [], task={})
        self.assertEqual(caught.exception.code, "PROVIDER_CONTEXT_LIMIT")
        self.assertFalse(transport.requests)

    async def test_http_errors_and_transport_exceptions_are_sanitized(self):
        for status, expected in ((401, "PROVIDER_HTTP"), (302, "PROVIDER_HTTP"), (429, "PROVIDER_TRANSIENT"), (500, "PROVIDER_TRANSIENT")):
            with self.subTest(status=status):
                transport = FixtureStream([SECRET.encode()], status=status)
                with self.assertRaises(CompletionError) as caught:
                    await DeepSeekProvider(SECRET, "explicit", stream_transport=transport).stream_complete(MESSAGES, [], task={})
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertTrue(transport.closed)
        for error in (RuntimeError(SECRET), PlatformError("BAD", SECRET)):
            async def transport(*args):
                raise error
                yield
            with self.assertRaises(CompletionError) as caught:
                await DeepSeekProvider(SECRET, "explicit", stream_transport=transport).stream_complete(MESSAGES, [], task={})
            self.assertNotIn(SECRET, str(caught.exception))

    async def test_done_closes_connection_without_waiting_for_eof(self):
        closed = asyncio.Event()

        async def transport(*args):
            try:
                yield 200, event({"content": "ok"}, "stop", USAGE) + DONE
                await asyncio.sleep(10)
            finally:
                closed.set()

        result = await DeepSeekProvider(SECRET, "explicit", timeout=0.05, stream_transport=transport).stream_complete(MESSAGES, [], task={})
        self.assertEqual(result.message["content"], "ok")
        self.assertTrue(closed.is_set())


class StreamingHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_transport_delivers_delta_before_second_socket_read_finishes(self):
        saw_delta = threading.Event()
        stream = Mock()
        stream.headers = {}
        stream.status = 200
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        chunks = iter([event({"content": "first"}), event({"content": "last"}, "stop", USAGE) + DONE, b""])
        reads = 0

        def read(size):
            nonlocal reads
            reads += 1
            if reads == 2 and not saw_delta.wait(0.2):
                raise TimeoutError
            return next(chunks)

        stream.read1.side_effect = read
        opener = Mock()
        opener.open.return_value = stream
        with patch("maplatform.provider.urllib.request.build_opener", return_value=opener):
            result = await DeepSeekProvider(SECRET, "explicit", timeout=1).stream_complete(MESSAGES, [], task={}, on_delta=lambda delta: saw_delta.set())
            await asyncio.sleep(0.02)
        self.assertEqual(result.message["content"], "firstlast")
        stream.__exit__.assert_called_once()

    async def test_cancellation_interrupts_blocked_default_transport_socket_read(self):
        read_started = threading.Event()
        socket_closed = threading.Event()
        stream = Mock()
        stream.headers = {}
        stream.status = 200
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)

        def read(size):
            read_started.set()
            if not socket_closed.wait(0.5):
                raise TimeoutError
            return b""

        stream.read1.side_effect = read
        stream.fp.raw._sock.shutdown.side_effect = lambda how: socket_closed.set()
        opener = Mock()
        opener.open.return_value = stream
        with patch("maplatform.provider.urllib.request.build_opener", return_value=opener):
            task = asyncio.create_task(DeepSeekProvider(SECRET, "explicit").stream_complete(MESSAGES, [], task={}))
            await asyncio.wait_for(asyncio.to_thread(read_started.wait), timeout=0.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0.02)
        self.assertTrue(socket_closed.is_set())
        stream.__exit__.assert_called_once()

    async def test_default_transport_streams_and_closes_real_reader(self):
        stream = io.BytesIO(event({"content": "ok"}, "stop", USAGE) + DONE)
        stream.headers = {}
        stream.status = 200
        opener = Mock()
        opener.open.return_value = stream
        with patch("maplatform.provider.urllib.request.build_opener", return_value=opener):
            result = await DeepSeekProvider(SECRET, "explicit").stream_complete(MESSAGES, [], task={})
            # The cancelled queue producer gets a chance to finish its finally.
            await asyncio.sleep(0.02)
        self.assertEqual(result.message["content"], "ok")
        self.assertTrue(stream.closed)

    async def test_no_redirect_handler_does_not_forward_credentials(self):
        seen = []
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError("https://api.example", 302, "redirect", {}, io.BytesIO(SECRET.encode()))

        def build(handler):
            seen.append(handler)
            return opener

        with patch("maplatform.provider.urllib.request.build_opener", side_effect=build):
            chunks = [chunk async for chunk in _urllib_stream_transport("https://api.example", b"{}", {"Authorization": "Bearer " + SECRET}, 1, 1000)]
        self.assertEqual(chunks, [(302, b"")])
        self.assertIsNone(seen[0].redirect_request(None, None, 302, "redirect", {}, "https://other.example"))
        self.assertEqual(opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
