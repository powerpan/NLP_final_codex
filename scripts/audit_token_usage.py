"""Read-only, content-free accounting of saved model calls and history replay.

Usage numbers come from provider responses. Snapshot character counts are an
attribution aid, never an estimate of billed tokens or exact historical requests.
No model, network, Store constructor, credential lookup, or sandbox is used.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3


METRICS = ("prompt_tokens", "completion_tokens", "total_tokens",
           "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "reasoning_tokens")
MEASURED_FIELDS = ("request_chars", "messages_chars", "tools_chars", "reasoning_text_chars",
                   "tool_calls_chars", "message_count", "tool_schema_count")
MEASURED_ROLES = {"system", "user", "assistant", "tool"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def label(value):
    """Only server identifiers/tool names may leave the source database."""
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:-]{1,100}", value):
        return value
    return "unrecognized"


def numeric_usage(usage):
    usage = usage if isinstance(usage, dict) else {}
    values = {k: usage.get(k) for k in METRICS}
    details = usage.get("completion_tokens_details")
    values["reasoning_tokens"] = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if values["prompt_cache_hit_tokens"] is None:
        details = usage.get("prompt_tokens_details")
        values["prompt_cache_hit_tokens"] = details.get("cached_tokens") if isinstance(details, dict) else None
    return {k: v if type(v) is int and v >= 0 else None for k, v in values.items()}


def usage_summary(usages):
    values = [numeric_usage(u) for u in usages]
    result = {"calls": len(values), "metrics": {
        key: {"sum_reported": sum(v[key] for v in values if v[key] is not None),
              "reported_calls": sum(v[key] is not None for v in values)} for key in METRICS}}
    paired = []
    for usage, value in zip(usages, values):
        prompt, hit, miss = (value[k] for k in ("prompt_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"))
        if prompt is None or hit is None or hit > prompt:
            continue
        if isinstance(usage, dict) and usage.get("prompt_cache_miss_tokens") is not None:
            if miss is None or hit + miss != prompt:
                continue
        paired.append(value)
    prompt = sum(v["prompt_tokens"] for v in paired)
    result["cache_hit_fraction_of_reported_prompt"] = sum(v["prompt_cache_hit_tokens"] for v in paired) / prompt if prompt else None
    result["cache_hit_fraction_calls"] = len(paired)
    return result


def measured_request(payload):
    """Reject arbitrary extensions instead of exporting untrusted event payloads."""
    keys = {"call_id", "unit", "message_chars_by_role", *MEASURED_FIELDS}
    if not isinstance(payload, dict) or set(payload) != keys:
        return None
    if payload["unit"] != "serialized_json_characters_not_tokens":
        return None
    if not isinstance(payload["call_id"], str) or label(payload["call_id"]) != payload["call_id"]:
        return None
    if any(type(payload[key]) is not int or payload[key] < 0 for key in MEASURED_FIELDS):
        return None
    roles = payload["message_chars_by_role"]
    if not isinstance(roles, dict) or set(roles) - MEASURED_ROLES:
        return None
    if any(type(value) is not int or value < 0 for value in roles.values()):
        return None
    return {key: payload[key] for key in MEASURED_FIELDS} | {"message_chars_by_role": dict(roles)}


def measured_summary(measurements):
    known = [value for value in measurements if value is not None]
    return {"unit": "serialized_json_characters_not_tokens", "call_count": len(measurements),
            "measured_calls": len(known),
            "sum_reported": {key: sum(value[key] for value in known) for key in MEASURED_FIELDS},
            "message_chars_by_role": {role: sum(value["message_chars_by_role"].get(role, 0) for value in known)
                                      for role in sorted(MEASURED_ROLES)}}


def message_components(message, tool_names):
    """JSON character attribution; includes escaping/quotes, not model tokens."""
    parts = Counter()
    role = message.get("role")
    for key, value in message.items():
        if key == "content":
            component = "tool_result:" + tool_names.get(message.get("tool_call_id"), "unknown") if role == "tool" else label(role) + "_content"
        elif key == "reasoning_content":
            component = "assistant_reasoning_content"
        elif key == "tool_calls":
            component = "assistant_tool_proposals"
        else:
            component = "message_framing"
        parts[component] += len(encoded(value))
    parts["message_framing"] += len(encoded(message)) - sum(parts.values())
    return parts


def snapshot_analysis(messages, prefixes):
    names = {}
    proposals = defaultdict(Counter)
    for message in messages:
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            name = label(function.get("name"))
            names[call.get("id")] = name
            arguments = function.get("arguments", "")
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                pass
            proposals[name][hashlib.sha256(encoded(arguments).encode()).hexdigest()] += 1
    components = [message_components(message, names) for message in messages]
    cumulative, replayed, unique = Counter(), Counter(), Counter()
    counts = [sum(prefix is not None and prefix > i for prefix in prefixes) for i in range(len(messages))]
    for component, count in zip(components, counts):
        if count:
            unique.update(component)
            cumulative.update({k: v * count for k, v in component.items()})
            replayed.update({k: v * (count - 1) for k, v in component.items()})
    return {
        "final_snapshot_messages": len(messages),
        "calls_with_inferred_prefix": sum(prefix is not None for prefix in prefixes),
        "cumulative_json_chars": dict(cumulative),
        "replayed_json_chars_after_first_inclusion": dict(replayed),
        "unique_included_json_chars": dict(unique),
        "tool_proposals": {name: {"total": sum(args.values()), "distinct_exact_arguments": len(args),
                                  "repeated_exact_arguments": sum(args.values()) - len(args)} for name, args in proposals.items()},
    }


def audit_database(path, name, workflow_id=None):
    path = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        clause, parameters = (" WHERE workflow_id=?", (workflow_id,)) if workflow_id else ("", ())
        tasks = {r["id"]: dict(r) for r in db.execute("SELECT * FROM tasks" + clause, parameters)}
        calls = [dict(r) for r in db.execute("SELECT * FROM llm_calls" + clause + " ORDER BY created,id", parameters)]
        for call in calls:
            call["usage"] = json.loads(call["usage_json"] or "{}")
        sizes, prefixes, measurements = {}, {}, {}
        for event in db.execute("SELECT task_id,type,payload_json FROM events" + clause + " ORDER BY id", parameters):
            payload = json.loads(event["payload_json"])
            if event["type"] == "SnapshotSaved" and type(payload.get("messages")) is int:
                sizes[event["task_id"]] = payload["messages"]
            elif event["type"] == "ModelCallReserved":
                prefixes[payload["call_id"]] = sizes.get(event["task_id"])
            elif event["type"] == "ModelRequestMeasured":
                measured = measured_request(payload)
                if measured is not None:
                    measurements[(event["task_id"], payload["call_id"])] = measured
        snapshots = {r["task_id"]: json.loads(r["messages_json"]) for r in db.execute("SELECT * FROM snapshots") if r["task_id"] in tasks}
        output_tasks, groups = [], defaultdict(list)
        progression = []
        for task_id, task in tasks.items():
            owned = [c for c in calls if c["task_id"] == task_id]
            group = label(task["owner"]) + ":" + label(task["kind"])
            groups[group].extend(c["usage"] for c in owned)
            history = snapshots.get(task_id, [])
            inferred = []
            for call in owned:
                count = prefixes.get(call["id"])
                count = count if count is not None and 0 <= count <= len(history) else None
                inferred.append(count)
                progression.append({"call_id": label(call["id"]), "task_id": label(task_id), "role_kind": group,
                                    "purpose": label(call["purpose"]), "status": label(call["status"]),
                                    "created_utc": datetime.fromtimestamp(call["created"], timezone.utc).isoformat(),
                                    "usage": numeric_usage(call["usage"]), "inferred_snapshot_prefix_messages": count,
                                    "measured_request": measurements.get((task_id, call["id"]))})
            output_tasks.append({"task_id": label(task_id), "role_kind": group, "status": label(task["status"]),
                                 "depth": task["depth"], "usage": usage_summary([c["usage"] for c in owned]),
                                 "measured_requests": measured_summary([measurements.get((task_id, c["id"])) for c in owned]),
                                 "history": snapshot_analysis(history, inferred)})
        operations = [dict(r) for r in db.execute("SELECT tool_name,status,args_hash,workflow_id FROM operations" + clause, parameters)]
        operation_totals = defaultdict(Counter)
        duplicate_operations = defaultdict(Counter)
        for operation in operations:
            tool = label(operation["tool_name"])
            operation_totals[tool][label(operation["status"])] += 1
            duplicate_operations[tool][(operation["workflow_id"], operation["args_hash"])] += 1
        replay = Counter()
        cumulative = Counter()
        for task in output_tasks:
            replay.update(task["history"]["replayed_json_chars_after_first_inclusion"])
            cumulative.update(task["history"]["cumulative_json_chars"])
        return {"name": label(name), "kind": "sqlite", "workflow_filter": label(workflow_id) if workflow_id else None,
                "usage": usage_summary([c["usage"] for c in calls]),
                "measured_requests": measured_summary([measurements.get((c["task_id"], c["id"])) for c in calls]),
                "roles": {role: usage_summary(usages) for role, usages in groups.items()},
                "tasks": output_tasks, "calls": sorted(progression, key=lambda c: c["created_utc"]),
                "snapshot_cumulative_json_chars": dict(cumulative), "snapshot_replayed_json_chars": dict(replay),
                "operations": {tool: {"statuses": dict(counts),
                                      "repeated_exact_arguments_extra_reservations": sum(duplicate_operations[tool].values()) - len(duplicate_operations[tool])}
                               for tool, counts in operation_totals.items()}}


def audit_usage_report(path, name):
    report = json.loads(Path(path).read_text())
    # Only opt-in probes with per-call usage, not arbitrary report contents.
    usages = report.get("usage")
    if not isinstance(usages, list) or any(not isinstance(u, dict) for u in usages):
        raise ValueError("Usage report needs an explicit per-call usage list")
    return {"name": label(name), "kind": "usage_report", "usage": usage_summary(usages)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", nargs=2, action="append", metavar=("LABEL", "PATH"), default=[])
    parser.add_argument("--workflow", nargs=2, action="append", metavar=("LABEL", "ID"), default=[])
    parser.add_argument("--usage-report", nargs=2, action="append", metavar=("LABEL", "PATH"), default=[])
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if not args.database and not args.usage_report:
        parser.error("At least one database or usage report is required")
    labels = [name for name, _ in args.database + args.usage_report]
    if len(labels) != len(set(labels)) or any(label(name) != name for name in labels):
        parser.error("Use unique simple labels")
    workflows = dict(args.workflow)
    if any(name not in {n for n, _ in args.database} for name in workflows):
        parser.error("Workflow filters must name a database label")
    report = {"generated_utc": datetime.now(timezone.utc).isoformat(),
              "method": {"provider_usage": "Exact reported usage. Missing fields stay unknown; sums include only reporting calls.",
                         "measured_requests": "Content-free ModelRequestMeasured events capture projected messages and tool schema JSON character counts before a model request. Coverage is explicit; absent historical measurements are not reconstructed or mixed with inferred snapshot characters. These are not billed tokens or the complete HTTP payload.",
                         "snapshot_chars": "Final saved history prefixes inferred from SnapshotSaved and ModelCallReserved. JSON character counts are not tokens and omit historical tool schema/transient context/projection changes. They measure stored replay pressure, not exact wire requests.",
                         "repeated_arguments": "Exact argument matches only, within a workflow; neither semantic duplication nor proof that a repeat was avoidable.",
                         "privacy": "No goals, messages, raw reasoning, tool arguments/results, credentials, or article text are exported."},
              "runs": [audit_database(path, name, workflows.get(name)) for name, path in args.database]
                      + [audit_usage_report(path, name) for name, path in args.usage_report]}
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    # Never overwrite source evidence, symlinks, or earlier audit reports.
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(encoded({"report": str(output), "runs": len(report["runs"]),
                   "calls": sum(run["usage"]["calls"] for run in report["runs"])}))


if __name__ == "__main__":
    main()
