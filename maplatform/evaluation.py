"""Fixed, external, non-LLM assertions over independently registered workflows.

This measures the supplied case contracts only. It is not a general-purpose
code-correctness benchmark, and scripted providers never measure model quality.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone

from .domain import AGENTS, TERMINAL, PlatformError, Policy, digest, dumps
from .provider import ScriptedProvider
from .runtime import Runtime, validate_acceptance


EVALUATOR_VERSION = "external-contracts-v1"
_USER_ID = "human:local"
_ASSERTIONS = {
    "summary_contains": {"kind", "text"},
    "artifact_contains": {"kind", "name", "text"},
    "artifact_sha256": {"kind", "name", "sha256"},
    "completed_task_count": {"kind", "owner", "task_kind", "count"},
    "tool_calls_exact": {"kind", "names"},
}


def _invalid(message):
    return PlatformError("INVALID_EVALUATION_SUITE", message)


def _text(value, name, limit=100000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise _invalid(f"{name} must be nonempty text within its size limit.")


def _freeze_suite(suite):
    # The detached canonical value is hashed before any workflow/provider call.
    # Later mutation of the caller's dict cannot change cases or assertions.
    try:
        frozen = json.loads(dumps(suite))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _invalid("Suite must contain finite JSON data.") from None
    if not isinstance(frozen, dict) or set(frozen) - {"id", "description", "cases"}:
        raise _invalid("Suite fields are id, optional description, and cases.")
    _text(frozen.get("id"), "Suite ID", 256)
    if "description" in frozen:
        _text(frozen["description"], "Suite description")
    cases = frozen.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 128:
        raise _invalid("Suite must contain between 1 and 128 external cases.")
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) - {"id", "goal", "owner", "acceptance", "assertions"}:
            raise _invalid("Unknown or invalid case fields.")
        _text(case.get("id"), "Case ID", 256)
        if case["id"] in seen:
            raise _invalid("Case IDs must be unique.")
        seen.add(case["id"])
        _text(case.get("goal"), "Case goal")
        if not isinstance(case.get("owner", "planner"), str) or case.get("owner", "planner") not in AGENTS:
            raise _invalid("Case owner must be a registered agent.")
        try:
            validate_acceptance(case.get("acceptance", []))
        except (PlatformError, TypeError, ValueError):
            raise _invalid("Case contains an invalid runtime acceptance criterion.") from None
        assertions = case.get("assertions")
        if not isinstance(assertions, list) or not 1 <= len(assertions) <= 128:
            raise _invalid("Every case needs between 1 and 128 external assertions.")
        for assertion in assertions:
            if not isinstance(assertion, dict) or not isinstance(assertion.get("kind"), str) or assertion.get("kind") not in _ASSERTIONS:
                raise _invalid("Unsupported external assertion kind.")
            kind = assertion["kind"]
            if set(assertion) != _ASSERTIONS[kind]:
                raise _invalid("An assertion has missing or unknown fields.")
            if "text" in assertion:
                _text(assertion["text"], "Assertion text")
            if "name" in assertion:
                _text(assertion["name"], "Artifact name", 256)
            if kind == "artifact_sha256" and (not isinstance(assertion["sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", assertion["sha256"])):
                raise _invalid("An artifact SHA-256 must contain 64 hexadecimal characters.")
            if kind == "completed_task_count":
                if not isinstance(assertion["owner"], str) or assertion["owner"] not in AGENTS or assertion["task_kind"] not in ("WORK", "REPLY"):
                    raise _invalid("Task-count assertions require a registered owner and WORK or REPLY kind.")
                if type(assertion["count"]) is not int or not 0 <= assertion["count"] <= 1000:
                    raise _invalid("Task-count assertions require a bounded nonnegative integer count.")
            if kind == "tool_calls_exact":
                names = assertion["names"]
                if not isinstance(names, list) or len(names) > 128 or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name) for name in names):
                    raise _invalid("Tool-call assertions require a bounded list of function names.")
    return frozen, digest(frozen)


def _utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _token_usage(calls):
    # Kept as a compatibility entry point for the opt-in live test runners.
    from .accounting import summarize_usage
    return summarize_usage(calls)


def _authorized_final_artifacts(store, root, result):
    """Read metadata and check root identity/ACL *before* extracting any bytes."""
    ids = result.get("artifact_ids")
    if not isinstance(ids, list) or any(not isinstance(aid, str) for aid in ids) or len(set(ids)) != len(ids):
        return {}, ["INVALID_FINAL_ARTIFACT_IDS"]
    artifacts = {}
    errors = []
    for aid in ids:
        metadata = store.one("SELECT id,workflow_id,task_id,owner,name,sha256,grantees_json FROM artifacts WHERE id=?", (aid,))
        permitted = False
        if metadata and metadata["workflow_id"] == root["workflow_id"]:
            try:
                grants = json.loads(metadata["grantees_json"])
                permitted = (
                    metadata["owner"] == root["owner"] and metadata["task_id"] == root["id"]
                ) or (isinstance(grants, list) and root["owner"] in grants)
            except (TypeError, ValueError):
                permitted = False
        if not permitted:
            errors.append("FINAL_ARTIFACT_NOT_AUTHORIZED")
            continue
        row = store.one("SELECT content FROM artifacts WHERE id=?", (aid,))
        if not row or not isinstance(row["content"], (bytes, bytearray, memoryview)):
            errors.append("ARTIFACT_CONTENT_UNAVAILABLE")
            continue
        content = bytes(row["content"])
        sha = hashlib.sha256(content).hexdigest()
        if sha != metadata["sha256"]:
            errors.append("ARTIFACT_INTEGRITY_MISMATCH")
            continue
        artifacts[aid] = {"name": metadata["name"], "content": content, "sha256": sha}
    return artifacts, errors


def _tool_call_names(store, workflow_id):
    """Extract names only; snapshots and private reasoning never enter reports."""
    rows = store.all("SELECT s.messages_json FROM snapshots s JOIN tasks t ON t.id=s.task_id WHERE t.workflow_id=?", (workflow_id,))
    if not rows:
        return None
    names = []
    try:
        for row in rows:
            messages = json.loads(row["messages_json"])
            if not isinstance(messages, list):
                return None
            for message in messages:
                if message["role"] == "assistant":
                    for call in message.get("tool_calls") or []:
                        name = call["function"]["name"]
                        if not isinstance(name, str):
                            return None
                        names.append(name)
    except (ValueError, TypeError, KeyError):
        return None
    return names


def _judge_assertions(assertions, summary, artifacts, completed_tasks=(), tool_names=None):
    results = []
    for index, assertion in enumerate(assertions):
        kind = assertion["kind"]
        reason = None
        if kind == "summary_contains":
            if assertion["text"] not in summary:
                reason = "SUMMARY_TEXT_MISSING"
        elif kind == "completed_task_count":
            count = sum(task["owner"] == assertion["owner"] and task["kind"] == assertion["task_kind"] for task in completed_tasks)
            if count != assertion["count"]:
                reason = "COMPLETED_TASK_COUNT_MISMATCH"
        elif kind == "tool_calls_exact":
            if tool_names is None:
                reason = "TOOL_TRACE_UNAVAILABLE"
            elif Counter(tool_names) != Counter(assertion["names"]):
                reason = "TOOL_CALLS_MISMATCH"
        else:
            matches = [a for a in artifacts.values() if a["name"] == assertion["name"]]
            if not matches:
                reason = "FINAL_ARTIFACT_MISSING"
            elif len(matches) != 1:
                reason = "FINAL_ARTIFACT_NAME_AMBIGUOUS"
            elif kind == "artifact_sha256":
                if matches[0]["sha256"] != assertion["sha256"].lower():
                    reason = "ARTIFACT_SHA256_MISMATCH"
            else:
                try:
                    text = matches[0]["content"].decode("utf-8")
                except UnicodeError:
                    reason = "ARTIFACT_NOT_UTF8"
                else:
                    if assertion["text"] not in text:
                        reason = "ARTIFACT_TEXT_MISSING"
        results.append({"index": index, "kind": kind, "passed": reason is None, "reason": reason})
    return results


def _case_report(store, case, workflow, database, registration_event_id, observed_at):
    wf = store.one("SELECT * FROM workflows WHERE id=?", (workflow["id"],))
    root = store.one("SELECT * FROM tasks WHERE id=?", (wf["root_task_id"],))
    events = store.all("SELECT id,type,created,payload_json FROM events WHERE workflow_id=? ORDER BY id", (wf["id"],))
    terminal = next((event for event in reversed(events) if event["type"] == "WorkflowTerminated"), None)
    calls = store.all("SELECT id,purpose,status,usage_json FROM llm_calls WHERE workflow_id=? ORDER BY created,id", (wf["id"],))
    purposes = dict(sorted(Counter(call["purpose"] for call in calls).items()))
    task_ids = [row["id"] for row in store.all("SELECT id FROM tasks WHERE workflow_id=? ORDER BY created,id", (wf["id"],))]
    report = {
        "case_id": case["id"], "workflow_id": wf["id"], "root_task_id": root["id"],
        "workflow_status": wf["status"], "task_status": root["status"],
        "passed": False, "status": "INCOMPLETE", "assertion_results": [], "assertion_failures": [],
        "call_count": len(calls), "purpose_counts": purposes,
        "token_usage": _token_usage(calls),
        "call_status_counts": dict(sorted(Counter(call["status"] for call in calls).items())),
        "wall_time_seconds": max(0.0, terminal["created"] - wf["created"]) if terminal else None,
        "observed_elapsed_seconds": max(0.0, observed_at - wf["created"]),
        "created_at": _utc(wf["created"]), "terminal_at": _utc(terminal["created"]) if terminal else None,
        "artifact_ids": [], "output_failures": [],
        "assertions_hash": digest(case["assertions"]),
        "trace": {
            "database": database, "workflow_id": wf["id"], "root_task_id": root["id"],
            "task_ids": task_ids, "registration_event_id": registration_event_id,
            "terminal_event_id": terminal["id"] if terminal else None,
            "event_id_min": events[0]["id"] if events else None,
            "event_id_max": events[-1]["id"] if events else None,
            "llm_call_ids": [call["id"] for call in calls],
        },
    }
    if root["status"] == "WAITING" or wf["status"] not in TERMINAL:
        human = store.one("SELECT id FROM waits WHERE task_id IN (SELECT id FROM tasks WHERE workflow_id=?) AND status='OPEN' AND kind IN ('HUMAN_CLARIFY','HUMAN_APPROVAL') LIMIT 1", (wf["id"],))
        report["status"] = "BLOCKED_HUMAN" if human else "INCOMPLETE"
        report["output_failures"].append("WORKFLOW_NOT_TERMINAL")
    elif wf["status"] != "COMPLETED" or root["status"] != "COMPLETED":
        report["status"] = wf["status"] if wf["status"] in {"TIMED_OUT", "CANCELLED"} else "WORKFLOW_FAILED"
        report["output_failures"].append("WORKFLOW_NOT_COMPLETED")
    elif not terminal:
        report["output_failures"].append("TERMINAL_EVENT_MISSING")
    else:
        try:
            result = json.loads(root["result_json"] or "{}")
        except (TypeError, ValueError):
            result = {}
        if not isinstance(result, dict) or not isinstance(result.get("summary"), str):
            report["status"] = "ASSERTION_FAILED"
            report["output_failures"].append("INVALID_FINAL_OUTPUT")
        else:
            artifacts, output_errors = _authorized_final_artifacts(store, root, result)
            report["artifact_ids"] = list(artifacts)
            report["output_failures"].extend(output_errors)
            completed_tasks = store.all("SELECT owner,kind FROM tasks WHERE workflow_id=? AND status='COMPLETED'", (wf["id"],))
            tool_names = _tool_call_names(store, wf["id"]) if any(a["kind"] == "tool_calls_exact" for a in case["assertions"]) else None
            report["assertion_results"] = _judge_assertions(case["assertions"], result["summary"], artifacts, completed_tasks, tool_names)
            report["assertion_failures"] = [r for r in report["assertion_results"] if not r["passed"]]
            report["passed"] = not report["assertion_failures"] and not output_errors
            report["status"] = "PASS" if report["passed"] else "ASSERTION_FAILED"
    if not report["assertion_results"]:
        report["assertion_results"] = [{"index": i, "kind": a["kind"], "passed": False, "reason": "NO_VALID_COMPLETED_OUTPUT"} for i, a in enumerate(case["assertions"])]
        report["assertion_failures"] = list(report["assertion_results"])
    return report


async def evaluate_suite(store, provider, gateway, suite: dict, policy: Policy) -> dict:
    """Run a frozen suite once, with one workflow per external case.

    TCR always uses len(suite.cases), including failed, timed-out and waiting
    workflows. Human waits are left pending and produce incomplete reports.
    Existing agent/user-scoped memories are reused and explicitly reported.
    The caller supplies/owns the database, provider and sandbox lifetimes.
    """
    frozen, suite_hash = _freeze_suite(suite)
    if not isinstance(policy, Policy):
        raise _invalid("Evaluation policy must be a Policy instance.")
    # Runtime dispatches every runnable task in its database. Refuse to silently
    # run unrelated unfinished work alongside the fixed suite.
    if store.one("SELECT id FROM workflows WHERE status='OPEN' LIMIT 1"):
        raise PlatformError("EVALUATION_BUSY", "Finish existing open workflows or use a separate evaluation database.")
    tested_at = time.time()
    agent_hash = digest({name: asdict(spec) for name, spec in AGENTS.items()})
    policy_hash = digest(asdict(policy))
    memories = store.all("SELECT agent_id,COUNT(*) AS count FROM memories WHERE user_id=? GROUP BY agent_id ORDER BY agent_id", (_USER_ID,))
    memory = {
        "mode": "existing_scoped_reuse", "user_id": _USER_ID,
        "preexisting_count": sum(row["count"] for row in memories),
        "preexisting_by_agent": {row["agent_id"]: row["count"] for row in memories},
        "scope": "Agent and user scoped; cases may reuse prior or newly recorded experiences. No memory ablation is performed.",
    }
    fixture_only = isinstance(provider, ScriptedProvider) or type(provider).__name__ == "DemoProvider"
    model = getattr(provider, "model", None)
    provider_info = {"class": type(provider).__name__, "module": type(provider).__module__,
                     "model": model if isinstance(model, str) else None, "fixture_only": fixture_only}
    dbs = store.all("PRAGMA database_list")
    database = next((row["file"] for row in dbs if row["name"] == "main"), "") or ":memory:"
    registered = []
    for case in frozen["cases"]:
        workflow = store.create_workflow(case["goal"], user_id=_USER_ID, owner=case.get("owner", "planner"),
                                         acceptance=case.get("acceptance", []), policy=policy)
        with store.tx() as conn:
            store.event(conn, workflow["id"], workflow["root_task_id"], "EvaluationCaseRegistered", {
                "evaluator_version": EVALUATOR_VERSION, "suite_id": frozen["id"], "suite_hash": suite_hash,
                "case_id": case["id"], "assertions_hash": digest(case["assertions"]),
                "agent_config_hash": agent_hash, "policy_hash": policy_hash,
            })
            event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        registered.append((case, workflow, event_id))
    await Runtime(store, provider, gateway, policy).run_until_idle(max_seconds=policy.deadline_seconds + 1)
    observed_at = time.time()
    cases = [_case_report(store, case, workflow, database, event_id, observed_at) for case, workflow, event_id in registered]
    passed = sum(case["passed"] for case in cases)
    blocked = sum(case["status"] == "BLOCKED_HUMAN" for case in cases)
    incomplete = sum(case["workflow_status"] not in TERMINAL or case["status"] == "INCOMPLETE" for case in cases)
    summary = {
        "total": len(frozen["cases"]), "passed": passed, "failed": len(frozen["cases"]) - passed,
        "blocked_human": blocked, "incomplete": incomplete, "tcr": passed / len(frozen["cases"]),
        "tcr_numerator": passed, "tcr_denominator": len(frozen["cases"]),
        "call_count": sum(case["call_count"] for case in cases),
    }
    call_records = []
    for _, workflow, _ in registered:
        call_records.extend(store.all("SELECT usage_json FROM llm_calls WHERE workflow_id=?", (workflow["id"],)))
    summary["token_usage"] = _token_usage(call_records)
    return {
        "evaluator_version": EVALUATOR_VERSION, "suite_id": frozen["id"], "suite_hash": suite_hash,
        "tested_at": _utc(tested_at), "finished_at": _utc(observed_at),
        "provider": provider_info, "agent_config_hash": agent_hash, "policy_hash": policy_hash,
        "memory": memory, "complete": incomplete == 0, "summary": summary, "cases": cases,
        "measurement_scope": "Exact external case assertions only; not a full code-correctness benchmark. No LLM judge is used.",
        "interpretation": "Deterministic fixture results only; these scores do not measure model quality." if fixture_only else "Observed outcomes apply only to this frozen suite, configured provider, policy and memory state.",
    }
