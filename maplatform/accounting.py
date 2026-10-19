"""Content-free usage accounting; character measures are never token estimates."""
from __future__ import annotations

import json

from .domain import dumps


def summarize_usage(calls):
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    totals = dict.fromkeys(fields, 0)
    coverage = dict.fromkeys(fields, 0)
    cache_fields = ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
    cache_totals = dict.fromkeys(cache_fields, 0)
    cache_coverage = dict.fromkeys(cache_fields, 0)
    paired_calls = paired_prompt = paired_hit = 0
    reasoning = reasoning_calls = 0
    for call in calls:
        try:
            usage = json.loads(call.get("usage_json") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(usage, dict):
            continue
        for keys, sums, counts in ((fields, totals, coverage), (cache_fields, cache_totals, cache_coverage)):
            for field in keys:
                value = usage.get(field)
                if type(value) is int and value >= 0:
                    sums[field] += value
                    counts[field] += 1
        hit, miss, prompt = (usage.get(k) for k in (*cache_fields, "prompt_tokens"))
        if all(type(v) is int and v >= 0 for v in (hit, miss, prompt)) and hit + miss == prompt:
            paired_calls += 1
            paired_prompt += prompt
            paired_hit += hit
        details = usage.get("completion_tokens_details")
        value = details.get("reasoning_tokens") if isinstance(details, dict) else None
        if type(value) is int and value >= 0:
            reasoning += value
            reasoning_calls += 1
    return {
        "reported_totals": {k: totals[k] if coverage[k] else None for k in fields},
        "calls_reporting": coverage, "total_calls": len(calls),
        "complete": all(count == len(calls) for count in coverage.values()),
        "cache": {
            "reported_totals": {k: cache_totals[k] if cache_coverage[k] else None for k in cache_fields},
            "calls_reporting": cache_coverage,
            "calls_with_consistent_breakdown": paired_calls,
            "prompt_tokens_with_consistent_breakdown": paired_prompt,
            "hit_rate_on_reported_prompt": paired_hit / paired_prompt if paired_prompt else None,
        },
        "reasoning_tokens": {"reported_total": reasoning if reasoning_calls else None,
                             "calls_reporting": reasoning_calls},
        "note": "Provider-reported tokens only; missing usage is unknown, not free. Cache hits remain part of prompt tokens. No currency cost is estimated.",
    }


def request_shape(messages, tools):
    """Measure the exact projected request without storing text, arguments or hashes."""
    roles = dict.fromkeys(("system", "user", "assistant", "tool"), 0)
    reasoning_chars = tool_call_chars = 0
    for message in messages:
        role = message.get("role")
        if role in roles:
            roles[role] += len(dumps(message))
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str):
            reasoning_chars += len(reasoning)
        if message.get("tool_calls"):
            tool_call_chars += len(dumps(message["tool_calls"]))
    return {
        "unit": "serialized_json_characters_not_tokens",
        "request_chars": len(dumps({"messages": messages, "tools": tools})),
        "messages_chars": len(dumps(messages)), "tools_chars": len(dumps(tools)),
        "message_chars_by_role": roles,
        "reasoning_text_chars": reasoning_chars,
        "tool_calls_chars": tool_call_chars,
        "message_count": len(messages), "tool_schema_count": len(tools),
    }
