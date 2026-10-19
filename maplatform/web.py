"""DeepSeek's official Search and Fetch JSON APIs, independent of model calls.

Only fixed api.deepseek.com endpoints receive credentials. Fetching happens at
DeepSeek, never by opening the supplied target URL on this host. Returned text
is untrusted evidence, not permission to execute page instructions. There are no
automatic retries: every attempt may be billed and must fit the workflow budget.

Contracts: https://api-docs.deepseek.com/api/search/
           https://api-docs.deepseek.com/api/fetch/
"""
from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import time
import urllib.parse
from typing import Callable, Mapping

from .domain import PlatformError, dumps

Transport = Callable[[str, bytes, Mapping[str, str], float, int], tuple[int, bytes]]
_MAX_RESULT_BYTES = 60_000


def public_url(value: str) -> str:
    """Reject credentials, local names, and nonpublic literal addresses.

    We intentionally do not resolve target names on the local machine: the
    remote service resolves and fetches them under its own safety policy. This
    validation is not a DNS-rebinding or destination-redirect guarantee.
    """
    valid = isinstance(value, str) and 1 <= len(value) <= 8192
    if valid:
        valid = not any(ord(c) <= 32 or ord(c) == 127 for c in value) and "\\" not in value
    try:
        parsed = urllib.parse.urlsplit(value if valid else "")
        host = (parsed.hostname or "").rstrip(".").lower()
        valid = (valid and parsed.scheme in {"http", "https"} and bool(host)
                 and parsed.username is None and parsed.password is None and "%" not in host)
        parsed.port
        host = host.encode("idna").decode("ascii")
        if host in {"localhost", "localhost.localdomain", "metadata.google.internal"} or host.endswith(
                (".localhost", ".local", ".internal", ".lan", ".home", ".localdomain")):
            valid = False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # inet_aton recognizes IPv4 shortcuts such as 127.1, integer, octal,
            # or hex forms which URL parsers and fetchers may also recognize.
            try:
                address = ipaddress.ip_address(socket.inet_aton(host))
            except OSError:
                address = None
            if address is None and ("." not in host or re.fullmatch(r"[0-9.]+", host)):
                valid = False
        if address is not None and not address.is_global:
            valid = False
        if address is None and any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                   for label in host.split(".")):
            valid = False
        value.encode("utf-8")
    except (AttributeError, TypeError, ValueError, UnicodeError):
        valid = False
    if not valid:
        raise PlatformError("INVALID_ARGUMENT", "URL must be public HTTP(S), without credentials, local hosts, or private IP addresses.")
    return value


def _transport(url, payload, headers, timeout, limit):
    """One HTTPS request; no proxies, redirects, retries, or target-page IO."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname != "api.deepseek.com" or parsed.scheme != "https" or parsed.path not in {
            "/v1/tools/search", "/v1/tools/fetch"}:
        raise PlatformError("WEB_CONFIG", "Web API endpoint is not permitted.")
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPSConnection("api.deepseek.com", timeout=timeout)
    try:
        connection.request("POST", parsed.path, body=payload, headers=dict(headers))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        response = connection.getresponse()
        with response:
            if response.status != 200:
                return response.status, b""  # Discard bodies which could echo keys.
            length = response.getheader("Content-Length")
            if length is not None:
                if not length.isdigit():
                    raise PlatformError("WEB_PROTOCOL", "Invalid response length header.")
                if int(length) > limit:
                    raise PlatformError("WEB_RESPONSE_LIMIT", "Web response exceeded its byte limit.")
            data = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                sock = connection.sock
                if sock is None and response.fp is not None:
                    sock = getattr(getattr(response.fp, "raw", None), "_sock", None)
                if sock is not None:
                    sock.settimeout(remaining)
                chunk = response.read1(min(65536, limit + 1 - len(data)))
                if not chunk:
                    return response.status, bytes(data)
                data.extend(chunk)
                if len(data) > limit:
                    raise PlatformError("WEB_RESPONSE_LIMIT", "Web response exceeded its byte limit.")
    finally:
        connection.close()


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError
        return result

    def constant(value):
        raise ValueError

    try:
        result = json.loads(raw, object_pairs_hook=pairs, parse_float=number, parse_constant=constant)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise PlatformError("WEB_PROTOCOL", "Web provider returned invalid or ambiguous JSON.") from None


def _text(value, limit):
    if not isinstance(value, str):
        raise PlatformError("WEB_PROTOCOL", "Web result text must be a string.")
    try:
        raw = value.encode("utf-8")
    except UnicodeError:
        raise PlatformError("WEB_PROTOCOL", "Web result must contain valid UTF-8 text.") from None
    return raw[:limit].decode("utf-8", errors="ignore"), len(raw) > limit, len(raw)


class DeepSeekWebProvider:
    SEARCH_ENDPOINT = "https://api.deepseek.com/v1/tools/search"
    FETCH_ENDPOINT = "https://api.deepseek.com/v1/tools/fetch"
    max_query_chars = 128
    max_results = 50
    # Stable public adapter identity; credentials never participate in cache keys.
    cache_identity = "deepseek-search-fetch-v1"

    def __init__(self, api_key: str | None = None, *, transport: Transport | None = None,
                 timeout: float = 15, max_response_bytes: int = 2_097_152):
        key = os.environ.get("DEEPSEEK_API_KEY", "") if api_key is None else api_key
        if (not isinstance(key, str) or not key.strip() or len(key) > 4096
                or any(ord(c) <= 32 or ord(c) >= 127 for c in key)):
            raise PlatformError("WEB_CONFIG", "Configure a valid DEEPSEEK_API_KEY before enabling web tools.")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 120
                or type(max_response_bytes) is not int or not 1024 <= max_response_bytes <= 8_388_608):
            raise PlatformError("WEB_CONFIG", "Web timeout and response limits must be bounded positive values.")
        self._api_key = key
        self._transport = transport or _transport
        self.timeout = float(timeout)
        self.max_response_bytes = max_response_bytes

    async def _request(self, endpoint, data, *, timeout=None):
        timeout = self.timeout if timeout is None else timeout
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 120):
            raise PlatformError("WEB_CONFIG", "Web request timeout must be a bounded positive value.")
        payload = dumps(data).encode("utf-8")
        headers = {"Authorization": "Bearer " + self._api_key,
                   "Content-Type": "application/json", "Accept": "application/json"}
        try:
            status, raw = await asyncio.wait_for(asyncio.to_thread(
                self._transport, endpoint, payload, headers, timeout, self.max_response_bytes), timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise PlatformError("WEB_TIMEOUT", "Web request timed out; it may have been processed and billed. No automatic retry was performed.") from None
        except PlatformError as error:
            if error.code == "WEB_RESPONSE_LIMIT":
                raise PlatformError("WEB_RESPONSE_LIMIT", "Web response exceeded its byte limit.") from None
            raise PlatformError("WEB_PROTOCOL", "Web transport returned an invalid response.") from None
        except Exception:
            raise PlatformError("WEB_TRANSPORT", "Web transport failed; processing and billing may be unknown. No automatic retry was performed.") from None
        if type(status) is not int or not isinstance(raw, bytes):
            raise PlatformError("WEB_PROTOCOL", "Web transport must return an HTTP status and response bytes.")
        errors = {
            400: ("WEB_REQUEST", "Web API rejected the request format (HTTP 400)."),
            401: ("WEB_AUTH", "DeepSeek web authentication failed (HTTP 401); check the local key."),
            402: ("WEB_BALANCE", "DeepSeek account has insufficient balance (HTTP 402)."),
            403: ("WEB_FORBIDDEN", "The remote fetch safety policy rejected this URL (HTTP 403)."),
            404: ("WEB_NOT_FOUND", "No usable page content was available (HTTP 404)."),
            408: ("WEB_TIMEOUT", "The remote web request timed out (HTTP 408); no automatic retry was performed."),
            413: ("WEB_REQUEST", "Web API rejected the request size (HTTP 413)."),
            429: ("WEB_RATE_LIMIT", "DeepSeek web rate limit reached (HTTP 429); no automatic retry was performed."),
        }
        if 300 <= status <= 399:
            raise PlatformError("WEB_REDIRECT", "Web API redirected; credentials were not forwarded.")
        if status in errors:
            raise PlatformError(*errors[status])
        if 500 <= status <= 599:
            raise PlatformError("WEB_UNAVAILABLE", "DeepSeek web service failed; no automatic retry was performed.")
        if status != 200:
            raise PlatformError("WEB_HTTP", "DeepSeek web service returned an unexpected HTTP status.")
        if len(raw) > self.max_response_bytes:
            raise PlatformError("WEB_RESPONSE_LIMIT", "Web response exceeded its byte limit.")
        data = _json(raw)
        pending = [data]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, str) and self._api_key in item:
                raise PlatformError("WEB_PROTOCOL", "Web response unexpectedly contained credential material and was discarded.")
        return data

    async def search(self, query: str, limit: int = 5, *, timeout=None) -> list[dict]:
        if (not isinstance(query, str) or not query.strip() or len(query) > self.max_query_chars
                or "\x00" in query or type(limit) is not int or not 1 <= limit <= self.max_results):
            raise PlatformError("INVALID_ARGUMENT", "DeepSeek search requires 1–128 Unicode characters and a result limit of 1–50.")
        try:
            query.encode("utf-8")
        except UnicodeError:
            raise PlatformError("INVALID_ARGUMENT", "Search query must contain valid UTF-8 text.") from None
        data = await self._request(self.SEARCH_ENDPOINT, {"query": query, "count": limit, "need_content": False}, timeout=timeout)
        entries = data.get("results")
        if not isinstance(entries, list):
            raise PlatformError("WEB_PROTOCOL", "Web search response is missing its results array.")
        results = []
        for item in entries[:limit]:
            if not isinstance(item, dict):
                raise PlatformError("WEB_PROTOCOL", "Web search result must be an object.")
            try:
                url = public_url(item.get("url"))
            except PlatformError:
                raise PlatformError("WEB_PROTOCOL", "Web search result contains an unsafe or invalid URL.") from None
            out = {"url": url, "untrusted": True}
            truncated = []
            for key, maximum in (("title", 512), ("snippet", 1024), ("summary", 4096)):
                out[key], shortened, _ = _text(item.get(key), maximum)
                if shortened:
                    truncated.append(key)
            for key, maximum in (("date", 128), ("site_name", 256)):
                if key in item:
                    if item[key] is None:
                        out[key] = None
                    else:
                        out[key], shortened, _ = _text(item[key], maximum)
                        if shortened:
                            truncated.append(key)
            if "rerank_score" in item:
                value = item["rerank_score"]
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 3:
                    raise PlatformError("WEB_PROTOCOL", "Web result relevance score must be a finite number between 0 and 3.")
                out["rerank_score"] = value
            if "authority" in item:
                value = item["authority"]
                if type(value) is not int or not -1 <= value <= 4:
                    raise PlatformError("WEB_PROTOCOL", "Web result authority must be an integer between -1 and 4.")
                out["authority"] = value
            out["truncated_fields"] = truncated
            out["truncated"] = bool(truncated)
            # Keep complete source URLs. Omit tail results instead of silently
            # shortening URLs or exceeding the model tool-result byte budget.
            if len(dumps(results + [out]).encode("utf-8")) > _MAX_RESULT_BYTES - 512:
                break
            results.append(out)
        if results:
            results[-1]["results_truncated"] = len(results) < len(entries)
            results[-1]["available_result_count"] = len(entries)
            results[-1]["returned_result_count"] = len(results)
        return results

    async def fetch(self, url: str, *, timeout=None) -> dict:
        url = public_url(url)
        data = await self._request(self.FETCH_ENDPOINT, {"url": url}, timeout=timeout)
        title, title_truncated, _ = _text(data.get("title"), 512)
        content, truncated, original_bytes = _text(data.get("content"), 48_000)
        if not content.strip():
            raise PlatformError("WEB_EMPTY_CONTENT", "Web fetch returned no usable page text.")
        result = {"url": url, "title": title, "content": content, "untrusted": True,
                  "truncated": truncated or title_truncated, "content_bytes": original_bytes,
                  "returned_content_bytes": len(content.encode("utf-8")),
                  "truncated_fields": (["title"] if title_truncated else []) + (["content"] if truncated else [])}
        # JSON escaping may make control-heavy content larger than its UTF-8.
        while len(dumps(result).encode("utf-8")) > _MAX_RESULT_BYTES:
            content, _, _ = _text(content, max(0, len(content.encode("utf-8")) * 3 // 4))
            result.update(content=content, truncated=True, returned_content_bytes=len(content.encode("utf-8")))
            if "content" not in result["truncated_fields"]:
                result["truncated_fields"].append("content")
        return result
