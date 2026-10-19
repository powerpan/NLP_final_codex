"""Buffered and streaming model boundaries and deterministic test double.

The provider never retries or executes tools. The runtime reserves every call and
owns retry decisions. Assistant messages are returned intact so that subsequent
DeepSeek tool requests retain *all* reasoning_content and tool_calls.
"""

from __future__ import annotations

import asyncio
import codecs
import concurrent.futures
import copy
import inspect
import json
import math
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from collections.abc import AsyncIterator
from typing import Any, Callable, Mapping, Protocol

from .domain import PlatformError


@dataclass(frozen=True)
class Completion:
    message: dict
    usage: dict


class Provider(Protocol):
    async def complete(
        self, messages: list, tools: list, *, task: dict, purpose: str = "AGENT",
        max_output_tokens: int | None = None,
    ) -> Completion: ...


Transport = Callable[[str, bytes, Mapping[str, str], float, int], tuple[int, bytes]]
StreamTransport = Callable[[str, bytes, Mapping[str, str], float, int], AsyncIterator[tuple[int, bytes]]]
DeltaCallback = Callable[[dict], None]
_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_MAX_OUTPUT_TOKENS = 393216


class CompletionError(PlatformError):
    """An unusable completion can still have known, billable token usage."""

    def __init__(self, code: str, message: str, usage: dict):
        super().__init__(code, message)
        # Retain only validated accounting counters, never output or extensions.
        self.usage = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens") if key in usage}


def _error(code: str, message: str) -> PlatformError:
    # Only fixed descriptions go into errors: response bodies, credentials,
    # request headers and the transport's exception text are never interpolated.
    return PlatformError(code, message)


def _loads(value: str | bytes, code: str = "PROVIDER_PROTOCOL") -> Any:
    def reject_constant(_: str) -> None:
        raise ValueError("non-finite JSON")

    def unique_pairs(pairs: list) -> dict:
        obj = {}
        for key, item in pairs:
            if key in obj:
                raise ValueError("duplicate JSON key")
            obj[key] = item
        return obj

    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number

    try:
        return json.loads(value, parse_constant=reject_constant, parse_float=finite_float, object_pairs_hook=unique_pairs)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _error(code, "Invalid or ambiguous JSON at the model boundary.") from None


def _encode(value: Any, code: str = "PROVIDER_REQUEST") -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _error(code, "Model input must be finite JSON data.") from None


def _calls(message: dict, code: str) -> list[dict]:
    if message.get("role") != "assistant":
        raise _error(code, "Expected an assistant message.")
    if "content" not in message or (message["content"] is not None and not isinstance(message["content"], str)):
        raise _error(code, "Assistant content must be a string or null.")
    if "reasoning_content" in message and message["reasoning_content"] is not None and not isinstance(message["reasoning_content"], str):
        raise _error(code, "Assistant reasoning_content must be a string or null.")
    if "function_call" in message:
        raise _error(code, "Legacy function_call responses are not supported.")
    calls = message.get("tool_calls")
    if calls is None:
        return []
    if not isinstance(calls, list):
        raise _error(code, "tool_calls must be an array or null.")
    seen = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise _error(code, "Only function tool calls are supported.")
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id.strip() or call_id in seen:
            raise _error(code, "Tool calls require unique, nonempty IDs.")
        seen.add(call_id)
        function = call.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not _NAME.fullmatch(function["name"]):
            raise _error(code, "Tool calls require a valid function name.")
        arguments = function.get("arguments")
        if not isinstance(arguments, str) or not isinstance(_loads(arguments, code), dict):
            raise _error(code, "Tool call arguments must encode a JSON object.")
    return calls


def _validate_tools(tools: list) -> set[str]:
    if not isinstance(tools, list):
        raise _error("PROVIDER_REQUEST", "Tools must be a list.")
    names = set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise _error("PROVIDER_REQUEST", "Only function tool schemas are supported.")
        function = tool.get("function")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not _NAME.fullmatch(function["name"]):
            raise _error("PROVIDER_REQUEST", "Tool schemas require a valid function name.")
        name = function["name"]
        if name in names:
            raise _error("PROVIDER_REQUEST", "Tool schema names must be unique.")
        parameters = function.get("parameters")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise _error("PROVIDER_REQUEST", "Tool parameters must have an object schema.")
        names.add(name)
    return names


def _validate_history(messages: list) -> set[str]:
    if not isinstance(messages, list) or not messages:
        raise _error("PROVIDER_REQUEST", "Messages must be a nonempty list.")
    all_ids: set[str] = set()
    pending: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            raise _error("PROVIDER_REQUEST", "Every message must be an object.")
        role = message.get("role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise _error("PROVIDER_REQUEST", "Tool replies must match an unresolved tool call exactly once.")
            if not isinstance(message.get("content"), str):
                raise _error("PROVIDER_REQUEST", "This text-only provider requires string tool results.")
            pending.remove(call_id)
            continue
        if pending:
            raise _error("PROVIDER_REQUEST", "All tool calls must have replies before another conversation message.")
        if role == "assistant":
            calls = _calls(message, "PROVIDER_REQUEST")
            for call in calls:
                if call["id"] in all_ids:
                    raise _error("PROVIDER_REQUEST", "Tool call IDs must not be reused in conversation history.")
                all_ids.add(call["id"])
                pending.add(call["id"])
        elif role in {"system", "user"}:
            if not isinstance(message.get("content"), str):
                raise _error("PROVIDER_REQUEST", "This text-only provider requires string messages.")
        else:
            raise _error("PROVIDER_REQUEST", "Unsupported conversation message role.")
    if pending:
        raise _error("PROVIDER_REQUEST", "Cannot request a completion with unresolved tool calls.")
    return all_ids


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward Authorization to a redirected host or downgrade HTTPS.
        return None


def _urllib_transport(url: str, payload: bytes, headers: Mapping[str, str], timeout: float, limit: int) -> tuple[int, bytes]:
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(url, data=payload, headers=dict(headers), method="POST")
    opener = urllib.request.build_opener(_NoRedirects())
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        # The body is deliberately discarded: gateways can echo credentials or
        # user input in errors. Status alone determines our error category.
        status = error.code
        error.close()
        return status, b""
    with response:
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                if int(length) > limit:
                    raise _error("PROVIDER_RESPONSE_LIMIT", "Model response exceeds the configured byte limit.")
            except ValueError:
                raise _error("PROVIDER_PROTOCOL", "Invalid response length header.") from None
        body = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError
            # DeepSeek can send whitespace keep-alives before the JSON body.
            # read() may wait for the entire response despite those bytes;
            # read1() lets us re-check the total deadline between reads.
            chunk = response.read1(min(65536, limit + 1 - len(body)))
            if not chunk:
                return response.status, bytes(body)
            body.extend(chunk)
            if len(body) > limit:
                raise _error("PROVIDER_RESPONSE_LIMIT", "Model response exceeds the configured byte limit.")


async def _urllib_stream_transport(
    url: str, payload: bytes, headers: Mapping[str, str], timeout: float, limit: int,
) -> AsyncIterator[tuple[int, bytes]]:
    """Bridge blocking HTTPS reads to a bounded, cancellable async stream.

    The worker owns response.close(); shutting down its socket wakes a blocked
    read without taking the buffered reader lock on the event loop. Cancellation
    during DNS/connect is bounded by the configured transport timeout. No
    callbacks run in this worker and redirects never forward Authorization.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=4)
    stopped = threading.Event()
    active: list[Any] = []
    lock = threading.Lock()
    deadline = time.monotonic() + timeout

    def send(item: Any) -> None:
        if stopped.is_set():
            return
        future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        try:
            while not stopped.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    future.result(timeout=min(0.05, remaining))
                    return
                except concurrent.futures.TimeoutError:
                    continue
        finally:
            if not future.done():
                future.cancel()

    def worker() -> None:
        request = urllib.request.Request(url, data=payload, headers=dict(headers), method="POST")
        opener = urllib.request.build_opener(_NoRedirects())
        try:
            try:
                response = opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                status = error.code
                error.close()
                send((status, b""))
                return
            with response:
                with lock:
                    active.append(response)
                if stopped.is_set():
                    return
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        size = int(length)
                    except ValueError:
                        raise _error("PROVIDER_PROTOCOL", "Invalid response length header.") from None
                    if size < 0:
                        raise _error("PROVIDER_PROTOCOL", "Invalid response length header.")
                    if size > limit:
                        raise _error("PROVIDER_RESPONSE_LIMIT", "Model response exceeds the configured byte limit.")
                send((response.status, b""))
                size = 0
                while not stopped.is_set():
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    chunk = response.read1(min(65536, limit + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise _error("PROVIDER_RESPONSE_LIMIT", "Model response exceeds the configured byte limit.")
                    send((response.status, chunk))
        except Exception as error:
            # The consumer sanitizes errors without exposing transport text.
            try:
                send(error)
            except (RuntimeError, TimeoutError, concurrent.futures.CancelledError):
                pass
        finally:
            with lock:
                active.clear()
            try:
                send(None)
            except (RuntimeError, TimeoutError, concurrent.futures.CancelledError):
                pass

    future = loop.run_in_executor(None, worker)
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        stopped.set()
        with lock:
            response = active[0] if active else None
            # CPython's urllib HTTPResponse wraps its socket in a buffered file.
            raw = getattr(getattr(response, "fp", None), "raw", None)
            connection = getattr(raw, "_sock", None)
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        # A cancelled request has unknown server-side processing/billing. The
        # worker checks stopped before any further queue writes or socket reads.
        future.cancel()


class _SSEDecoder:
    """Incremental SSE frames, including split UTF-8 and CR/LF boundaries."""

    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.buffer = ""
        self.data: list[str] = []
        self.scan = 0

    def feed(self, chunk: bytes, *, final: bool = False) -> list[str]:
        try:
            self.buffer += self.decoder.decode(chunk, final=final)
        except UnicodeError:
            raise _error("PROVIDER_PROTOCOL", "Stream contains invalid UTF-8.") from None
        frames = []
        while True:
            match = re.compile(r"\r\n|\r|\n").search(self.buffer, self.scan)
            if match is None:
                self.scan = len(self.buffer)
                break
            if not final and match.group() == "\r" and match.end() == len(self.buffer):
                self.scan = match.start()
                break
            line = self.buffer[:match.start()]
            self.buffer = self.buffer[match.end():]
            self.scan = 0
            if not line:
                if self.data:
                    frames.append("\n".join(self.data))
                    self.data.clear()
            elif not line.startswith(":"):
                field, separator, value = line.partition(":")
                if field == "data":
                    self.data.append(value[1:] if separator and value.startswith(" ") else value)
        return frames


class _StreamCompletion:
    """Buffer private state until the protocol commits a complete assistant turn."""

    def __init__(self, on_delta: DeltaCallback | None):
        self.on_delta = on_delta
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.calls: dict[int, dict] = {}
        self.arguments: dict[int, list[str]] = {}
        self.usage: dict = {}
        self.finish_reason: str | None = None
        self.done = False

    def accept(self, frame: str) -> None:
        if self.done:
            raise _error("PROVIDER_PROTOCOL", "Unexpected data after the stream completion marker.")
        if frame == "[DONE]":
            if self.finish_reason is None:
                raise _error("PROVIDER_PROTOCOL", "Stream ended without a completion finish reason.")
            self.done = True
            return
        event = _loads(frame)
        if not isinstance(event, dict) or "error" in event:
            raise _error("PROVIDER_PROTOCOL", "Provider returned an invalid streaming response.")
        usage = event.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                raise _error("PROVIDER_PROTOCOL", "Completion usage must be an object.")
            for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if name in usage and (type(usage[name]) is not int or usage[name] < 0):
                    raise _error("PROVIDER_PROTOCOL", "Completion token usage must contain nonnegative integers.")
            self.usage.update(copy.deepcopy(usage))
        choices = event.get("choices")
        # Official DeepSeek usage accompanies the final choice. A usage-only
        # frame is also accepted for OpenAI-compatible gateways.
        if choices == [] and usage is not None:
            return
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise _error("PROVIDER_PROTOCOL", "Expected exactly one streaming completion choice.")
        choice = choices[0]
        if type(choice.get("index")) is not int or choice["index"] != 0:
            raise _error("PROVIDER_PROTOCOL", "Unexpected streaming completion choice index.")
        delta = choice.get("delta")
        if not isinstance(delta, dict) or "function_call" in delta:
            raise _error("PROVIDER_PROTOCOL", "Invalid streaming assistant delta.")
        if delta.get("role") not in (None, "assistant"):
            raise _error("PROVIDER_PROTOCOL", "Expected an assistant streaming message.")
        if self.finish_reason is not None and any(delta.get(key) for key in ("content", "reasoning_content", "tool_calls")):
            raise _error("PROVIDER_PROTOCOL", "Stream contains output after its finish reason.")
        notifications = []
        for field, parts, kind in (("content", self.content, "content"), ("reasoning_content", self.reasoning, "reasoning")):
            text = delta.get(field)
            if text is not None:
                if not isinstance(text, str):
                    raise _error("PROVIDER_PROTOCOL", "Streamed assistant text must be a string or null.")
                parts.append(text)
                if text:
                    notifications.append({"kind": kind, "text": text} if kind == "content" else {"kind": kind, "chars": len(text)})
        fragments = delta.get("tool_calls")
        if fragments is not None:
            if not isinstance(fragments, list):
                raise _error("PROVIDER_PROTOCOL", "Streamed tool calls must be an array or null.")
            for fragment in fragments:
                if not isinstance(fragment, dict) or type(fragment.get("index")) is not int or fragment["index"] < 0:
                    raise _error("PROVIDER_PROTOCOL", "Streamed tool calls require nonnegative indexes.")
                call = self.calls.setdefault(fragment["index"], {"function": {"arguments": ""}})
                for key in ("id", "type"):
                    value = fragment.get(key)
                    if value is not None:
                        if not isinstance(value, str) or (key in call and call[key] != value):
                            raise _error("PROVIDER_PROTOCOL", "Streamed tool identity changed during generation.")
                        call[key] = value
                function = fragment.get("function")
                if function is not None:
                    if not isinstance(function, dict):
                        raise _error("PROVIDER_PROTOCOL", "Invalid streamed tool function.")
                    name = function.get("name")
                    if name is not None:
                        if not isinstance(name, str) or ("name" in call["function"] and call["function"]["name"] != name):
                            raise _error("PROVIDER_PROTOCOL", "Streamed tool function name changed during generation.")
                        call["function"]["name"] = name
                    arguments = function.get("arguments")
                    if arguments is not None:
                        if not isinstance(arguments, str):
                            raise _error("PROVIDER_PROTOCOL", "Streamed tool arguments must be strings.")
                        self.arguments.setdefault(fragment["index"], []).append(arguments)
        reason = choice.get("finish_reason")
        if reason is not None:
            if not isinstance(reason, str) or self.finish_reason is not None:
                raise _error("PROVIDER_PROTOCOL", "Invalid or repeated streaming finish reason.")
            self.finish_reason = reason
        if self.on_delta:
            for notification in notifications:
                try:
                    self.on_delta(notification)
                except Exception:
                    raise _error("PROVIDER_OBSERVER", "Stream observer failed; the model request has been stopped.") from None

    def complete(self, history_ids: set[str], tool_names: set[str]) -> Completion:
        if not self.done:
            raise _error("PROVIDER_TRUNCATED", "Model stream ended before its completion marker; partial content and tool calls will not be executed.")
        reason = self.finish_reason
        if reason == "length":
            raise _error("PROVIDER_TRUNCATED", "Model output reached a limit; partial content and tool calls will not be executed. Check the output token cap, which also covers reasoning.")
        if reason == "content_filter":
            raise _error("PROVIDER_FILTERED", "Provider filtered the completion.")
        if reason in {"insufficient_system_resource", "aborted"}:
            raise _error("PROVIDER_TRANSIENT", "Provider interrupted generation; partial content and tool calls will not be executed. No automatic retry was performed by the provider adapter.")
        if reason not in {"stop", "tool_calls"}:
            raise _error("PROVIDER_PROTOCOL", "Unsupported or missing completion finish reason.")
        if sorted(self.calls) != list(range(len(self.calls))):
            raise _error("PROVIDER_PROTOCOL", "Streamed tool call indexes must be contiguous.")
        message: dict = {"role": "assistant", "content": "".join(self.content) if self.content else None}
        if self.reasoning:
            message["reasoning_content"] = "".join(self.reasoning)
        if self.calls:
            for index, call in self.calls.items():
                call["function"]["arguments"] = "".join(self.arguments.get(index, []))
            message["tool_calls"] = [self.calls[index] for index in sorted(self.calls)]
        calls = _calls(message, "PROVIDER_PROTOCOL")
        if bool(calls) != (reason == "tool_calls"):
            raise _error("PROVIDER_PROTOCOL", "Completion finish reason does not match its tool calls.")
        for call in calls:
            if call["id"] in history_ids:
                raise _error("PROVIDER_PROTOCOL", "Provider reused a previous tool call ID.")
            if call["function"]["name"] not in tool_names:
                raise _error("PROVIDER_PROTOCOL", "Provider requested a tool that was not offered.")
        return Completion(copy.deepcopy(message), copy.deepcopy(self.usage))


class DeepSeekProvider:
    """Explicit DeepSeek Chat Completions configuration; stdlib HTTPS only.

    A custom base URL is permitted, but is not a claim of third-party gateway
    compatibility. No API-key-bearing errors or response bodies are logged.
    Transport is a synchronous injection point, useful for local fixtures.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "https://api.deepseek.com",
        thinking: bool = True,
        reasoning_effort: str = "high",
        timeout: float = 60.0,
        max_output_tokens: int = 8192,
        max_context_chars: int = 120000,
        max_response_bytes: int = 4_000_000,
        transport: Transport | None = None,
        stream_transport: StreamTransport | None = None,
    ):
        if not isinstance(api_key, str) or not api_key.strip() or "\r" in api_key or "\n" in api_key:
            raise _error("PROVIDER_CONFIG", "An API key is required and must be a valid header value.")
        if not isinstance(model, str) or not model.strip() or len(model) > 256:
            raise _error("PROVIDER_CONFIG", "An explicit model ID is required.")
        try:
            parsed = urllib.parse.urlsplit(base_url)
            valid_url = parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            parsed.port  # Validate malformed ports without including the URL in errors.
        except (TypeError, ValueError):
            valid_url = False
        if not valid_url:
            raise _error("PROVIDER_CONFIG", "Provider base URL must be HTTPS without credentials, query or fragment.")
        if not isinstance(thinking, bool) or reasoning_effort not in {"low", "high", "max"}:
            raise _error("PROVIDER_CONFIG", "Thinking must be boolean and reasoning effort must be low, high or max.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise _error("PROVIDER_CONFIG", "Provider timeout must be greater than zero and at most 300 seconds.")
        for value, upper in ((max_output_tokens, _MAX_OUTPUT_TOKENS), (max_context_chars, 16_000_000), (max_response_bytes, 64_000_000)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
                raise _error("PROVIDER_CONFIG", "Provider token and size limits must be bounded positive integers.")
        self._api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.timeout = float(timeout)
        self.max_output_tokens = max_output_tokens
        self.max_context_chars = max_context_chars
        self.max_response_bytes = max_response_bytes
        self._transport = transport or _urllib_transport
        self._stream_transport = stream_transport or _urllib_stream_transport

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides) -> "DeepSeekProvider":
        env = os.environ if environ is None else environ
        raw_thinking = env.get("DEEPSEEK_THINKING", "true").strip().lower()
        if raw_thinking not in {"true", "false", "1", "0", "enabled", "disabled"}:
            raise _error("PROVIDER_CONFIG", "DEEPSEEK_THINKING must be true or false.")
        try:
            config = {
                "api_key": env.get("DEEPSEEK_API_KEY", ""),
                "model": env.get("DEEPSEEK_MODEL", ""),
                "base_url": env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                "thinking": raw_thinking in {"true", "1", "enabled"},
                "reasoning_effort": env.get("DEEPSEEK_REASONING_EFFORT", "high"),
                "timeout": float(env.get("DEEPSEEK_TIMEOUT_SECONDS", "60")),
                "max_output_tokens": int(env.get("DEEPSEEK_MAX_OUTPUT_TOKENS", "8192")),
                "max_context_chars": int(env.get("DEEPSEEK_MAX_CONTEXT_CHARS", "120000")),
                "max_response_bytes": int(env.get("DEEPSEEK_MAX_RESPONSE_BYTES", "4000000")),
            }
        except (ValueError, TypeError, OverflowError):
            raise _error("PROVIDER_CONFIG", "Invalid numeric provider configuration.") from None
        config.update(overrides)
        return cls(**config)

    async def stream_complete(
        self, messages: list, tools: list, *, task: dict, purpose: str = "AGENT",
        max_output_tokens: int | None = None, on_delta: DeltaCallback | None = None,
    ) -> Completion:
        """Emit public text as it arrives; return only a validated complete turn.

        on_delta is synchronous and runs on the caller's event loop. Reasoning
        only emits character counts; tool arguments are never exposed. Partial
        output remains provisional until a valid finish reason and [DONE].
        """
        history_ids = _validate_history(messages)
        tool_names = _validate_tools(tools)
        if max_output_tokens is not None and (type(max_output_tokens) is not int or not 1 <= max_output_tokens <= _MAX_OUTPUT_TOKENS):
            raise _error("PROVIDER_REQUEST", "Per-call output token limit must be a bounded positive integer.")
        if on_delta is not None and (not callable(on_delta) or inspect.iscoroutinefunction(on_delta)):
            raise _error("PROVIDER_REQUEST", "Stream observer must be callable.")
        payload = {
            "model": self.model,
            "messages": copy.deepcopy(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": min(self.max_output_tokens, max_output_tokens) if max_output_tokens is not None else self.max_output_tokens,
            "thinking": {"type": "enabled" if self.thinking else "disabled"},
        }
        if self.thinking:
            payload["reasoning_effort"] = self.reasoning_effort
        if tools:
            payload["tools"] = copy.deepcopy(tools)
        if purpose in {"AGENT", "BID", "FINAL_REPAIR"}:
            # These application purposes require JSON even when native tools
            # are offered. Runtime still validates the final schema and ACLs.
            payload["response_format"] = {"type": "json_object"}
        context = _encode({"messages": payload["messages"], "tools": tools})
        if len(context.decode("utf-8")) > self.max_context_chars:
            raise _error("PROVIDER_CONTEXT_LIMIT", "Conversation exceeds the configured character limit; no messages were dropped.")
        encoded = _encode(payload)
        headers = {"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json", "Accept": "text/event-stream"}
        state = _StreamCompletion(on_delta)
        decoder = _SSEDecoder()
        stream = None
        received = 0
        deadline = time.monotonic() + self.timeout
        try:
            async with asyncio.timeout(self.timeout):
                try:
                    stream = self._stream_transport(self.base_url + "/chat/completions", encoded, headers, self.timeout, self.max_response_bytes)
                except Exception:
                    raise _error("PROVIDER_TRANSIENT", "Model transport failed; request processing and billing may be unknown.") from None
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    try:
                        item = await anext(stream)
                    except StopAsyncIteration:
                        for frame in decoder.feed(b"", final=True):
                            state.accept(frame)
                        break
                    except asyncio.CancelledError:
                        raise
                    except (TimeoutError, socket.timeout):
                        raise TimeoutError from None
                    except PlatformError as error:
                        if error.code == "PROVIDER_RESPONSE_LIMIT":
                            raise _error("PROVIDER_RESPONSE_LIMIT", "Model response exceeds the configured byte limit.") from None
                        raise _error("PROVIDER_PROTOCOL", "Invalid response at the transport boundary.") from None
                    except Exception:
                        raise _error("PROVIDER_TRANSIENT", "Model transport failed; request processing and billing may be unknown.") from None
                    if not isinstance(item, tuple) or len(item) != 2:
                        raise _error("PROVIDER_PROTOCOL", "Stream transport must yield HTTP status and response bytes.")
                    status, chunk = item
                    if type(status) is not int or not isinstance(chunk, bytes):
                        raise _error("PROVIDER_PROTOCOL", "Stream transport must yield HTTP status and response bytes.")
                    if status == 429 or 500 <= status <= 599:
                        raise _error("PROVIDER_TRANSIENT", "Provider returned a rate limit or server error; no automatic retry was performed.")
                    if status != 200:
                        descriptions = {
                            400: "Provider rejected the request format (HTTP 400); verify model and request compatibility.",
                            401: "Provider authentication failed (HTTP 401); verify DEEPSEEK_API_KEY locally.",
                            402: "Provider account has insufficient balance (HTTP 402); check the account billing page.",
                            403: "Provider denied access (HTTP 403); verify account and model permissions.",
                            404: "Provider endpoint or model was not found (HTTP 404); verify the base URL and model ID.",
                            422: "Provider rejected request parameters (HTTP 422); verify model settings and tool schemas.",
                        }
                        raise _error("PROVIDER_HTTP", descriptions.get(status, "Provider rejected the request; verify configuration and request compatibility."))
                    received += len(chunk)
                    if received > self.max_response_bytes:
                        raise _error("PROVIDER_RESPONSE_LIMIT", "Model response exceeds the configured byte limit.")
                    for frame in decoder.feed(chunk):
                        state.accept(frame)
                    if state.done:
                        # [DONE] is the protocol terminator. Do not wait for a
                        # persistent HTTP connection to close after completion.
                        break
                return state.complete(history_ids, tool_names)
        except asyncio.CancelledError as error:
            error.usage = CompletionError("PROVIDER_CANCELLED", "", state.usage).usage
            raise
        except (TimeoutError, socket.timeout):
            raise CompletionError("PROVIDER_TRANSIENT", "Model stream timed out; the server may have processed it and billing may be unknown.", state.usage) from None
        except PlatformError as error:
            raise CompletionError(error.code, error.message, state.usage) from None
        except Exception:
            raise CompletionError("PROVIDER_PROTOCOL", "Invalid streaming model response or observer.", state.usage) from None
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                # Closing a custom transport must not replace a safe error with
                # arbitrary exception text (or conceal a caller cancellation).
                try:
                    async with asyncio.timeout(min(self.timeout, 0.25)):
                        await close()
                except Exception:
                    pass

    async def complete(
        self, messages: list, tools: list, *, task: dict, purpose: str = "AGENT",
        max_output_tokens: int | None = None,
    ) -> Completion:
        history_ids = _validate_history(messages)
        tool_names = _validate_tools(tools)
        if max_output_tokens is not None and (type(max_output_tokens) is not int or not 1 <= max_output_tokens <= _MAX_OUTPUT_TOKENS):
            raise _error("PROVIDER_REQUEST", "Per-call output token limit must be a bounded positive integer.")
        # Deep copy prevents a caller from mutating the running request. No
        # rewriting/summary/filtering of previous assistant fields occurs here.
        payload = {
            "model": self.model,
            "messages": copy.deepcopy(messages),
            "stream": False,
            "max_tokens": min(self.max_output_tokens, max_output_tokens) if max_output_tokens is not None else self.max_output_tokens,
            "thinking": {"type": "enabled" if self.thinking else "disabled"},
        }
        if self.thinking:
            payload["reasoning_effort"] = self.reasoning_effort
        if tools:
            payload["tools"] = copy.deepcopy(tools)
        if purpose in {"AGENT", "BID", "FINAL_REPAIR"}:
            payload["response_format"] = {"type": "json_object"}
        context = _encode({"messages": payload["messages"], "tools": tools})
        if len(context.decode("utf-8")) > self.max_context_chars:
            raise _error("PROVIDER_CONTEXT_LIMIT", "Conversation exceeds the configured character limit; no messages were dropped.")
        encoded = _encode(payload)
        headers = {"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json", "Accept": "application/json"}
        try:
            status, body = await asyncio.wait_for(
                asyncio.to_thread(self._transport, self.base_url + "/chat/completions", encoded, headers, self.timeout, self.max_response_bytes),
                timeout=self.timeout,
            )
        except asyncio.CancelledError:
            raise
        except (TimeoutError, socket.timeout):
            raise _error("PROVIDER_TRANSIENT", "Model request timed out; the server may have processed it and billing may be unknown.") from None
        except PlatformError as error:
            # Keep even injected/custom transport exceptions from leaking a
            # credential via their exception message.
            if error.code == "PROVIDER_RESPONSE_LIMIT":
                raise _error("PROVIDER_RESPONSE_LIMIT", "Model response exceeds the configured byte limit.") from None
            raise _error("PROVIDER_PROTOCOL", "Invalid response at the transport boundary.") from None
        except Exception:
            raise _error("PROVIDER_TRANSIENT", "Model transport failed; request processing and billing may be unknown.") from None
        if not isinstance(status, int) or isinstance(status, bool) or not isinstance(body, bytes):
            raise _error("PROVIDER_PROTOCOL", "Transport must return an HTTP status and response bytes.")
        if status == 429 or 500 <= status <= 599:
            raise _error("PROVIDER_TRANSIENT", "Provider returned a rate limit or server error; no automatic retry was performed.")
        if status != 200:
            descriptions = {
                400: "Provider rejected the request format (HTTP 400); verify model and request compatibility.",
                401: "Provider authentication failed (HTTP 401); verify DEEPSEEK_API_KEY locally.",
                402: "Provider account has insufficient balance (HTTP 402); check the account billing page.",
                403: "Provider denied access (HTTP 403); verify account and model permissions.",
                404: "Provider endpoint or model was not found (HTTP 404); verify the base URL and model ID.",
                422: "Provider rejected request parameters (HTTP 422); verify model settings and tool schemas.",
            }
            raise _error("PROVIDER_HTTP", descriptions.get(status, "Provider rejected the request; verify configuration and request compatibility."))
        if len(body) > self.max_response_bytes:
            raise _error("PROVIDER_RESPONSE_LIMIT", "Model response exceeds the configured byte limit.")
        response = _loads(body)
        if not isinstance(response, dict) or "error" in response:
            raise _error("PROVIDER_PROTOCOL", "Provider returned an invalid completion response.")
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise _error("PROVIDER_PROTOCOL", "Expected exactly one completion choice.")
        choice = choices[0]
        usage = response.get("usage", {})
        if not isinstance(usage, dict):
            raise _error("PROVIDER_PROTOCOL", "Completion usage must be an object.")
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if name in usage and (isinstance(usage[name], bool) or not isinstance(usage[name], int) or usage[name] < 0):
                raise _error("PROVIDER_PROTOCOL", "Completion token usage must contain nonnegative integers.")
        finish_reason = choice.get("finish_reason")
        if not isinstance(finish_reason, str):
            raise _error("PROVIDER_PROTOCOL", "Unsupported or missing completion finish reason.")
        if finish_reason == "length":
            raise CompletionError("PROVIDER_TRUNCATED", "Model output reached a limit; partial content and tool calls will not be executed. Check the output token cap, which also covers reasoning.", usage)
        if finish_reason == "content_filter":
            raise CompletionError("PROVIDER_FILTERED", "Provider filtered the completion.", usage)
        if finish_reason in {"insufficient_system_resource", "aborted"}:
            raise CompletionError("PROVIDER_TRANSIENT", "Provider interrupted generation; partial content and tool calls will not be executed. No automatic retry was performed by the provider adapter.", usage)
        if finish_reason not in {"stop", "tool_calls"}:
            raise _error("PROVIDER_PROTOCOL", "Unsupported or missing completion finish reason.")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise _error("PROVIDER_PROTOCOL", "Missing assistant completion message.")
        calls = _calls(message, "PROVIDER_PROTOCOL")
        if bool(calls) != (finish_reason == "tool_calls"):
            raise _error("PROVIDER_PROTOCOL", "Completion finish reason does not match its tool calls.")
        for call in calls:
            if call["id"] in history_ids:
                raise _error("PROVIDER_PROTOCOL", "Provider reused a previous tool call ID.")
            if call["function"]["name"] not in tool_names:
                raise _error("PROVIDER_PROTOCOL", "Provider requested a tool that was not offered.")
        return Completion(copy.deepcopy(message), copy.deepcopy(usage))


class ScriptedProvider:
    """Offline fixture only; it is not evidence of real model performance.

    A list supplies one assistant-message dict or Completion per call. A callable
    receives (messages, tools, *, task, purpose) and may be sync or async. The
    public calls list contains detached input snapshots for assertions.
    """

    def __init__(self, script: list[dict | Completion] | Callable):
        if not isinstance(script, list) and not callable(script):
            raise _error("PROVIDER_CONFIG", "A scripted provider requires a list or callable.")
        self._script = copy.deepcopy(script) if isinstance(script, list) else script
        self.calls: list[dict] = []
        self._index = 0

    async def complete(
        self, messages: list, tools: list, *, task: dict, purpose: str = "AGENT",
        max_output_tokens: int | None = None,
    ) -> Completion:
        request = copy.deepcopy({"messages": messages, "tools": tools, "task": task, "purpose": purpose, "max_output_tokens": max_output_tokens})
        self.calls.append(request)
        if callable(self._script):
            result = self._script(request["messages"], request["tools"], task=request["task"], purpose=purpose)
            if inspect.isawaitable(result):
                result = await result
        else:
            if self._index >= len(self._script):
                raise _error("PROVIDER_SCRIPT_EXHAUSTED", "The deterministic provider script is exhausted.")
            result = self._script[self._index]
            self._index += 1
        if isinstance(result, Completion):
            completion = result
        elif isinstance(result, dict):
            completion = Completion(result, {})
        else:
            raise _error("PROVIDER_PROTOCOL", "Script must produce an assistant message or Completion.")
        _calls(completion.message, "PROVIDER_PROTOCOL")
        return Completion(copy.deepcopy(completion.message), copy.deepcopy(completion.usage))
