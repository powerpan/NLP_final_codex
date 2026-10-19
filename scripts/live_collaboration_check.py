"""Opt-in paid integration campaign through the interactive session controller.

Uses one dedicated database and records evidence without keys or raw reasoning
in its reports. The database remains private and retains the normal snapshots.
No Git gateway is configured. Docker executes generated Python unit tests only.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maplatform.__main__ import ProcessLock
from maplatform.domain import PlatformError, Policy, dumps
from maplatform.evaluation import _token_usage
from maplatform.provider import DeepSeekProvider
from maplatform.store import Store
from maplatform.terminal import TerminalController
from maplatform.tools import DockerSandbox, ToolGateway
from maplatform.web import DeepSeekWebProvider


GOAL = """這是真實整合測試，請實際完成以下有界協作，不能以口頭宣稱代替工具執行。
你是 planner。請在第一個工具批次同時做三件事，確保子任務可並行：
1. delegate_task 給 researcher，required_capabilities=["research"]，驗收包含非空與名為 web-findings.md 的產物。
完整交代 researcher：先用 search_web 搜尋 DeepSeek 官方 Search / Fetch API 文件（limit=5）。然後必須實際用 fetch_web 抓取 https://api-docs.deepseek.com/api/search/ 和 https://api-docs.deepseek.com/api/fetch/。
依實際結果整理查詢長度、結果條數、URL 限制、輸出欄位；說明來源是否截斷或缺失，不要猜測。將繁體中文結果寫成 web-findings.md，包含兩個完整來源 URL，publish_artifact 授權 planner 與 reviewer。
researcher 隨後必須用 delegate_task 指定 reviewer，required_capabilities=["review"]，把這個 artifact ID 放進 input_artifact_ids，驗收 nonempty。這是必須實際建立的孫 WORK 任務，不要以 ask_agent 代替。
reviewer 的任務是 import_artifact 後 read_file，檢查文件有來源、清楚數值與不確定性；只檢查收到的證據，不能宣稱自己獨立抓取網頁。不得再委派。
researcher 等孫任務結果後回報主要發現、審查結果，artifact_ids 包含真正發布的 web-findings.md ID。
2. delegate_task 給 coder，required_capabilities=["coding","testing"]，驗收包含 command {"kind":"command","argv":["python","-m","unittest","discover","-s","tests","-v"],"expect_exit_code":0} 和名為 counter.py 的 artifact。
完整交代 coder：在私有工作區實作 counter.py 的 count_successful_web_calls(events)，回傳成功的 search_web 與 fetch_web 次數字典，例如 {"search_web":1,"fetch_web":2}。每個 event 是含 tool_name、status 的 dict；只計 status=="SUCCEEDED"，忽略其他工具和失敗，兩個 key 永遠存在。輸入只需支援 list[dict]。
在 tests/test_counter.py 用標準 unittest 實際測試空列表、混合成功/失敗、重複成功、無關工具四種情形。用 run_tests 執行，失敗需修正。發布 counter.py 和測試檔，授權 planner 與 reviewer；final 提供真實產物 ID 和測試結果。不得用 git 或網頁工具。
3. ask_agent 給 reviewer，一次唯讀諮詢：『本輪是操作契約整合測試，請用一句繁體中文說明為何一次成功不能證明模型一般任務品質。直接回答，不用工具、不再委派。』
把每個子任務的完整要求放進它的 goal，它們不會自動收到你的整個上下文。
收到所有結果後，以繁體中文匯總：實際任務層級、搜尋與抓取結果、程式測試、審查與局限；附兩個官方來源 URL，並在 final artifact_ids 返回所有可存取的已發布文件與程式產物。若任何部分失敗要明確說明，不得假裝成功。
本輪不需人類澄清、不需記憶工具，不要執行或請求外部推送、安裝套件、發送訊息。依系統要求輸出 final JSON。"""

FOLLOWUP = """接續上輪，這次不再搜尋或抓取網頁，也不再寫程式。
先用 ask_human 問這個唯一測試問題：『下一輪摘要要使用繁體中文嗎？』，等回覆再繼續。
之後 delegate_task 給 reviewer（WORK，required_capabilities=["review"]，acceptance=[{"kind":"nonempty"}]），input_artifact_ids 放本輪新的 web-findings.md ID；請對照 session.artifact_continuity 找到新 ID，不要重用舊 workflow 的 ID。
reviewer 必須 import_artifact 並 read_file，確認這份上輪的已發布檔案在本輪仍可讀取、有兩個官方來源 URL，直接給簡短檢查結果，不再委派、不搜尋。
收到檢查後，用最多三句繁體中文總結前輪來源發現與跨輪產物檢查；保留實測限制。final artifact_ids 返回本輪的 web-findings.md ID。"""
ANSWER = "是，使用繁體中文，最多三句，並說明證據限制。這是測試驅動器提供的固定回答。"
NEWS_GOAL = "幫我查美國最近24小時比較火的三條新聞"

# Frozen independent cases, executed against the published artifact in Docker.
COUNTER_CHECK = '''import json
from candidate_counter import count_successful_web_calls
cases = [
    ([], {"search_web": 0, "fetch_web": 0}),
    ([{"tool_name": "search_web", "status": "SUCCEEDED"}], {"search_web": 1, "fetch_web": 0}),
    ([{"tool_name": "fetch_web", "status": "SUCCEEDED"}] * 3, {"search_web": 0, "fetch_web": 3}),
    ([{"tool_name": "search_web", "status": "FAILED"}, {"tool_name": "fetch_web", "status": "UNKNOWN"}], {"search_web": 0, "fetch_web": 0}),
    ([{"tool_name": "run_tests", "status": "SUCCEEDED"}], {"search_web": 0, "fetch_web": 0}),
    ([{"tool_name": "search_web", "status": "succeeded"}], {"search_web": 0, "fetch_web": 0}),
    ([{"tool_name": "search_web", "status": "SUCCEEDED"}] * 2 + [{"tool_name": "fetch_web", "status": "SUCCEEDED"}], {"search_web": 2, "fetch_web": 1}),
    ([{}, {"tool_name": "fetch_web"}, {"status": "SUCCEEDED"}], {"search_web": 0, "fetch_web": 0}),
]
for events, expected in cases:
    assert count_successful_web_calls(events) == expected
print(json.dumps({"passed": len(cases), "total": len(cases)}))
'''


def read_key():
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    try:
        result = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", getpass.getuser(),
                                 "-s", "Multi-Agent-Platform.DeepSeek", "-w"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise PlatformError("KEYCHAIN_UNAVAILABLE", "Keychain lookup failed or timed out; no API request was sent.") from None
    if result.returncode:
        raise PlatformError("KEYCHAIN_UNAVAILABLE", "The named DeepSeek Keychain item could not be read; no API request was sent.")
    return result.stdout.decode("utf-8").strip()


def cited_urls(text):
    # Full-width punctuation is a common URL delimiter in Chinese prose.
    return {url.rstrip(".,;:!?") for url in re.findall(r'https?://[^\s<>"\\\[\]()（），。；：！？、〉》」』]+', text)}


def audit(store, workflow_id, *, followup=False, news=False, stream_calls=None):
    wf = store.one("SELECT * FROM workflows WHERE id=?", (workflow_id,))
    tasks = store.all("SELECT id,parent_id,kind,owner,depth,status,retries,resumes,repairs,error,result_json FROM tasks WHERE workflow_id=? ORDER BY created,id", (workflow_id,))
    calls = store.all("SELECT id,task_id,purpose,status,usage_json,error,created FROM llm_calls WHERE workflow_id=? ORDER BY created,id", (workflow_id,))
    ops = store.all("SELECT id,task_id,tool_name,status,result_json,error FROM operations WHERE workflow_id=? ORDER BY created,id", (workflow_id,))
    root = next(t for t in tasks if t["id"] == wf["root_task_id"])
    final = json.loads(root["result_json"] or "{}")
    artifact_checks = []
    for aid in final.get("artifact_ids", []):
        row = store.one("SELECT * FROM artifacts WHERE id=?", (aid,))
        ok = bool(row and row["workflow_id"] == workflow_id and
                  (row["task_id"] == root["id"] and row["owner"] == root["owner"] or root["owner"] in json.loads(row["grantees_json"])))
        artifact_checks.append({"id": aid, "name": row["name"] if row else None,
                                "authorized": ok, "sha256_valid": bool(row and hashlib.sha256(bytes(row["content"])).hexdigest() == row["sha256"])})
    web = []
    for op in ops:
        result = json.loads(op["result_json"] or "{}")
        if op["tool_name"] == "search_web":
            entries = result.get("results", [])
            web.append({"operation_id": op["id"], "tool": op["tool_name"], "status": op["status"], "error": op["error"],
                        "nonempty": bool(entries), "source_urls": [r.get("url") for r in entries], "result_count": len(entries),
                        "official_result": any(urlsplit(r.get("url", "")).hostname == "api-docs.deepseek.com" for r in entries)})
        elif op["tool_name"] == "fetch_web":
            page = result.get("page", {})
            content = page.get("content", "")
            normalized = content.replace("\\_", "_").replace("\\/", "/").casefold()
            markers = {"https://api-docs.deepseek.com/api/search/": ("/v1/tools/search", "query", "count", "results", "128", "50"),
                       "https://api-docs.deepseek.com/api/fetch/": ("/v1/tools/fetch", "8192", "http", "https", "title", "content", "markdown")}.get(page.get("url"), ())
            web.append({"operation_id": op["id"], "tool": op["tool_name"], "status": op["status"], "error": op["error"],
                        "nonempty": bool(content.strip()), "url": page.get("url"), "title": page.get("title"),
                        "content_bytes": page.get("content_bytes"), "returned_content_bytes": page.get("returned_content_bytes"),
                        "truncated": page.get("truncated"), "expected_document": bool(markers) and all(m in normalized for m in markers),
                        "bytes_consistent": page.get("returned_content_bytes") == len(content.encode("utf-8"))})
    task_by_id = {t["id"]: t for t in tasks}
    streamed = [stream_calls[c["id"]] for c in calls if c["id"] in (stream_calls or {})]
    overlaps = []
    for i, left in enumerate(streamed):
        for right in streamed[i + 1:]:
            a, b = task_by_id[left["task_id"]], task_by_id[right["task_id"]]
            if a["id"] == b["id"] or not a["parent_id"] or a["parent_id"] != b["parent_id"] or a["kind"] != "WORK" or b["kind"] != "WORK":
                continue
            seconds = min(left.get("finished", 0), right.get("finished", 0)) - max(left["started"], right["started"])
            if seconds > 0:
                overlaps.append({"task_ids": [a["id"], b["id"]], "seconds": round(seconds, 3)})
    test_runs = []
    for op in ops:
        if op["tool_name"] != "run_tests" or task_by_id[op["task_id"]]["owner"] != "coder":
            continue
        result = json.loads(op["result_json"] or "{}")
        output = result.get("stdout", "") + result.get("stderr", "")
        count = re.search(r"Ran (\d+) tests? in", output)
        test_runs.append({"operation_id": op["id"], "exit_code": result.get("exit_code"),
                          "tests_run": int(count.group(1)) if count else 0,
                          "ok": bool(re.search(r"(?m)^OK\s*$", output))})
    checks = {
        "workflow_completed": wf["status"] == "COMPLETED",
        "all_tasks_completed": bool(tasks) and all(t["status"] == "COMPLETED" for t in tasks),
        "no_open_waits": store.one("SELECT COUNT(*) n FROM waits WHERE task_id IN (SELECT id FROM tasks WHERE workflow_id=?) AND status='OPEN'", (workflow_id,))["n"] == 0,
        "no_running_operations": all(op["status"] != "RUNNING" for op in ops),
        "no_failed_model_calls": all(c["status"] == "SUCCEEDED" for c in calls),
        "published_artifacts_returned": bool(artifact_checks) and all(a["authorized"] and a["sha256_valid"] for a in artifact_checks),
        "stream_completed": len(streamed) == len(calls) and bool(streamed) and all(s.get("status") == "SUCCEEDED" and "finished" in s for s in streamed),
        "incremental_stream_received": any(s.get("deltas", 0) > 1 and s.get("first_delta", float("inf")) < s.get("finished", 0) for s in streamed),
    }
    if followup:
        checks.update(
            no_more_web_requests=not web,
            human_answer_recorded=bool(store.one("SELECT id FROM approvals WHERE workflow_id=? AND tool_name='ask_human' AND status='ANSWERED'", (workflow_id,))),
            copied_artifacts=bool(store.one("SELECT id FROM events WHERE workflow_id=? AND type='SessionArtifactCopied'", (workflow_id,))),
            reviewer_read_file=any(op["tool_name"] == "read_file" and op["status"] == "SUCCEEDED" for op in ops),
        )
    elif news:
        fetched = {w["url"] for w in web if w["tool"] == "fetch_web" and w["status"] == "SUCCEEDED" and w["nonempty"]}
        cited = cited_urls(final.get("summary", ""))
        checks.update(
            live_search_succeeded=any(w["tool"] == "search_web" and w["status"] == "SUCCEEDED" and w["nonempty"] for w in web),
            three_fetched_sources_cited=len(cited & fetched) >= 3,
            web_budget_respected=len(web) <= Policy(**json.loads(wf["policy_json"])).max_web_requests,
        )
    else:
        checks.update(
            nested_work=any(t["depth"] >= 2 and t["kind"] == "WORK" for t in tasks),
            sibling_work=sum(t["parent_id"] == root["id"] and t["kind"] == "WORK" for t in tasks) >= 2,
            sibling_model_calls_overlap=bool(overlaps),
            peer_reply=any(t["kind"] == "REPLY" and t["owner"] == "reviewer" for t in tasks),
            official_search=any(w["tool"] == "search_web" and w["status"] == "SUCCEEDED" and w["nonempty"] and w["official_result"] for w in web),
            both_official_pages_fetched=all(any(w["tool"] == "fetch_web" and w["status"] == "SUCCEEDED" and w["nonempty"] and w["expected_document"] and w["bytes_consistent"] and w.get("url") == url for w in web) for url in ("https://api-docs.deepseek.com/api/search/", "https://api-docs.deepseek.com/api/fetch/")),
            web_operations_succeeded=bool(web) and all(w["status"] == "SUCCEEDED" for w in web),
            web_budget_respected=len(web) <= Policy(**json.loads(wf["policy_json"])).max_web_requests,
            sandbox_test_passed=any(r["exit_code"] == 0 and r["tests_run"] >= 4 and r["ok"] for r in test_runs),
            acceptance_command_passed=any(json.loads(e["payload_json"]).get("result", {}).get("exit_code") == 0 for e in store.all("SELECT payload_json FROM events WHERE workflow_id=? AND type='AcceptanceCommand'", (workflow_id,))),
        )
    return {"workflow_id": workflow_id, "passed": all(checks.values()), "checks": checks,
            "tasks": [{k: v for k, v in t.items() if k != "result_json"} for t in tasks],
            "tool_statuses": dict(Counter(op["tool_name"] + ":" + op["status"] for op in ops)),
            "tool_errors": [{k: op[k] for k in ("id", "task_id", "tool_name", "status", "error")} for op in ops if op["status"] != "SUCCEEDED"],
            "model_calls": len(calls), "model_errors": [{k: c[k] for k in ("id", "task_id", "status", "error")} for c in calls if c["status"] != "SUCCEEDED"],
            "token_usage": _token_usage(calls), "web": web, "artifacts": artifact_checks, "final": final,
            "sibling_model_overlaps": overlaps, "generated_test_runs": test_runs}


async def check_counter(store, sandbox, stage):
    candidates = [a for a in stage["artifacts"] if a["name"] == "counter.py" and a["authorized"] and a["sha256_valid"]]
    if len(candidates) != 1:
        return {"passed": False, "error": "Expected one final published counter.py"}
    artifact = store.one("SELECT * FROM artifacts WHERE id=?", (candidates[0]["id"],))
    task = {"workflow_id": stage["workflow_id"], "id": "external_validation", "owner": "external_validator"}
    await sandbox.write(task, "candidate_counter.py", bytes(artifact["content"]).decode("utf-8"))
    await sandbox.write(task, "check_counter.py", COUNTER_CHECK)
    result = await sandbox.run(task, ["python", "check_counter.py"], timeout=20)
    try:
        counts = json.loads(result.get("stdout", ""))
    except ValueError:
        counts = {}
    return {"artifact_id": artifact["id"], "sha256": artifact["sha256"],
            "passed": result.get("exit_code") == 0 and counts == {"passed": 8, "total": 8},
            "result": result, "scope": "Fixed external cases in a separate Docker workspace; no model call."}


async def campaign(args, directory):
    key = read_key()
    effort = args.reasoning_effort or ("high" if args.scenario == "news" else "low")
    provider = DeepSeekProvider(api_key=key, model=args.model, thinking=True, reasoning_effort=effort,
                                timeout=60 if args.scenario == "news" else 120, max_output_tokens=8192)
    web = DeepSeekWebProvider(api_key=key, timeout=20 if args.scenario == "news" else 40)
    del key
    policy = Policy(max_calls=60, max_tasks=20, max_depth=4, max_children=6, max_web_requests=20,
                    max_output_tokens=8192, max_context_chars=120000,
                    deadline_seconds=600 if args.scenario == "news" else 1200, tool_timeout=20 if args.scenario == "news" else 40)
    primary_goal = NEWS_GOAL if args.scenario == "news" else GOAL
    stages = (("news", NEWS_GOAL),) if args.scenario == "news" else (("primary", GOAL), ("followup", FOLLOWUP))
    report = {"status": "RUNNING", "scenario": args.scenario, "started_at": datetime.now(timezone.utc).isoformat(), "model": args.model,
              "reasoning_effort": effort, "policy_per_turn": asdict(policy), "primary_goal": primary_goal, "followup_goal": FOLLOWUP if args.scenario != "news" else None,
              "stages": [], "automatic_clarification": ANSWER, "independent_counter_check": COUNTER_CHECK,
              "scope": ("Unscripted natural-language news task. Automated checks establish retrieval/citation contracts; publication dates, relevance and factual accuracy require independent content review. No real Git push configured." if args.scenario == "news" else "Guided live integration contracts, not an autonomous benchmark. No real Git push configured.")}
    root = Path(__file__).resolve().parents[1]
    report["source_sha256"] = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted((root / "maplatform").glob("*.py")) + [Path(__file__).resolve()]}
    def save():
        path = directory / "report.json"
        tmp = directory / "report.tmp"
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    save()
    db = directory / "live.sqlite"
    with ProcessLock(db):
        store = Store(db)
        sandbox = DockerSandbox()
        gateway = ToolGateway(store, sandbox, policy, search=web)
        controller = TerminalController(store, provider, gateway, policy=policy, new=True, demo=False)
        report["session_id"] = controller.session["id"]
        stream_calls = {}
        def on_stream(event):
            now = time.monotonic()
            cid = event["call_id"]
            record = stream_calls.setdefault(cid, {"task_id": event["task_id"], "owner": event["owner"], "started": now,
                                                    "content_chars": 0, "reasoning_chars": 0, "deltas": 0})
            if event["type"] == "ModelDelta":
                delta = event["delta"]
                record.setdefault("first_delta", now)
                record["deltas"] += 1
                record["content_chars" if delta["kind"] == "content" else "reasoning_chars"] += len(delta["text"]) if delta["kind"] == "content" else delta["chars"]
            elif event["type"] == "ModelFinished":
                record["finished"] = now
                record["status"] = event["status"]
        controller.on_stream = on_stream
        try:
            for name, goal in stages:
                if not await controller.submit(goal):
                    raise PlatformError("LIVE_SUBMIT", controller.notices[-1])
                workflow_id = controller.workflows()[-1]["id"]
                print(dumps({"stage": name, "workflow_id": workflow_id, "database": str(db)}), flush=True)
                previous = None
                while True:
                    if controller.runner:
                        try:
                            await asyncio.wait_for(asyncio.shield(controller.runner), timeout=2)
                        except asyncio.TimeoutError:
                            pass
                    counts = dict(Counter(t["status"] for t in controller.tasks()))
                    snapshot = {"stage": name, "tasks": counts,
                                "calls": store.one("SELECT COUNT(*) n FROM llm_calls WHERE workflow_id=?", (workflow_id,))["n"],
                                "operations": store.one("SELECT COUNT(*) n FROM operations WHERE workflow_id=?", (workflow_id,))["n"]}
                    if snapshot != previous:
                        print(dumps(snapshot), flush=True)
                        previous = snapshot
                    fatal = store.one("SELECT error FROM operations WHERE workflow_id=? AND error IN ('WEB_AUTH','WEB_BALANCE')", (workflow_id,))
                    if fatal:
                        await controller.pause()
                        raise PlatformError(fatal["error"], "Live API access or balance requires attention; stopped further requests.")
                    if controller.busy:
                        continue
                    pending = controller.pending()
                    if name == "followup" and len(pending) == 1 and pending[0]["tool_name"] == "ask_human" and json.loads(pending[0]["args_json"])["question"] == "下一輪摘要要使用繁體中文嗎？":
                        # Reopen the real SQLite connection/controller while paused.
                        session_id = controller.session["id"]
                        await controller.close()
                        store.close()
                        store = Store(db)
                        gateway = ToolGateway(store, sandbox, policy, search=web)
                        controller = TerminalController(store, provider, gateway, policy=policy, session_id=session_id, demo=False)
                        controller.on_stream = on_stream
                        report["reopened_during_question"] = True
                        await controller.answer(pending[0]["id"], "answer", ANSWER)
                        continue
                    if pending:
                        report["pending_requests"] = [{"id": p["id"], "tool": p["tool_name"], "args": json.loads(p["args_json"])} for p in pending]
                    break
                stage = audit(store, workflow_id, followup=name == "followup", news=name == "news", stream_calls=stream_calls)
                if name == "news":
                    # Natural-language news does not require a downloadable file.
                    stage["checks"].pop("published_artifacts_returned")
                if name == "primary":
                    stage["independent_counter_result"] = await check_counter(store, sandbox, stage)
                    stage["checks"]["independent_counter_passed"] = stage["independent_counter_result"]["passed"]
                stage["passed"] = all(stage["checks"].values())
                report["stages"].append({"name": name, **stage})
                save()
                print(dumps({"stage": name, "passed": stage["passed"], "checks": stage["checks"], "model_calls": stage["model_calls"]}), flush=True)
                if not stage["passed"]:
                    break
            report["status"] = "PASS" if len(report["stages"]) == len(stages) and all(s["passed"] for s in report["stages"]) else "FAIL"
            if args.scenario == "news":
                report["independent_content_review"] = "Required separately; automatic PASS is not a news accuracy verdict."
        except BaseException as exc:
            report["status"] = "INTERRUPTED" if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else "ERROR"
            report["error"] = exc.as_dict() if isinstance(exc, PlatformError) else {"type": type(exc).__name__}
            raise
        finally:
            await controller.close()
            report["streaming_calls"] = stream_calls
            report["database_integrity"] = store.one("PRAGMA integrity_check")["integrity_check"]
            report["foreign_key_violations"] = store.all("PRAGMA foreign_key_check")
            if report["database_integrity"] != "ok" or report["foreign_key_violations"]:
                report["status"] = "FAIL"
            report["total_token_usage"] = _token_usage(store.all("SELECT usage_json FROM llm_calls"))
            report["ended_at"] = datetime.now(timezone.utc).isoformat()
            try:
                await sandbox.cleanup()
                report["containers_cleaned"] = True
            except PlatformError as exc:
                report["cleanup_error"] = exc.code
            save()
            store.close()
    print(dumps({"status": report["status"], "report": str(directory / "report.json"), "usage": report["total_token_usage"]}), flush=True)
    return 0 if report["status"] == "PASS" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, help="New private output directory; existing paths are rejected")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--scenario", choices=("collaboration", "news"), default="collaboration")
    parser.add_argument("--reasoning-effort", choices=("low", "high", "max"))
    args = parser.parse_args()
    directory = Path(args.run_dir).resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    try:
        return asyncio.run(campaign(args, directory))
    except PlatformError as exc:
        print(dumps(exc.as_dict()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
