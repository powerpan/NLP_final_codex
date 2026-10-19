from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import hashlib
import os
from pathlib import Path
import sys

from .domain import Policy, PlatformError, digest, dumps


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def budgets(command):
    command.add_argument("--deadline", type=positive_int, default=600, help="Workflow deadline in seconds")
    command.add_argument("--max-calls", type=positive_int, default=60, help="Maximum model calls per workflow, including bids and retries")
    command.add_argument("--max-tasks", type=positive_int, default=20, help="Maximum tasks per workflow, including the root")
    command.add_argument("--max-web-requests", type=positive_int, default=20, help="Maximum search/fetch requests per workflow, including failed or unknown attempts")
    command.add_argument("--tool-timeout", type=int, choices=range(1, 121), default=20, metavar="SECONDS", help="Per-tool time limit, 1–120 seconds; saved with each workflow")


def parser():
    p = argparse.ArgumentParser(description="Durable Multi-Agent course prototype (local trusted CLI)")
    p.add_argument("--db", default="data/platform.sqlite")
    s = p.add_subparsers(dest="command", required=True)
    shell = s.add_parser("shell", aliases=["interactive"], help="Interactive terminal: conversation, agent tree, streaming and approvals")
    shell.add_argument("--db", default=argparse.SUPPRESS, help="Session database (also accepted before shell)")
    shell.add_argument("--session", help="Resume displaying a saved session by ID; /resume explicitly continues unfinished work")
    shell.add_argument("--new", action="store_true", help="Start a new session instead of opening the latest")
    shell.add_argument("--owner", choices=["planner", "researcher", "coder", "reviewer"], default="planner")
    shell.add_argument("--demo", action="store_true", help="Offline interactive walkthrough; no API key or real commands")
    shell.add_argument("--plain", action="store_true", help="Line-oriented accessible interface, also suitable for pipes")
    shell.add_argument("--web", choices=["deepseek", "brave", "off"], default=None, help="Web provider (interactive default: DeepSeek official)")
    shell.add_argument("--parallelism", type=int, choices=range(1, 9), default=4)
    shell.add_argument("--max-depth", type=positive_int, default=4)
    shell.add_argument("--max-children", type=positive_int, default=6)
    budgets(shell)
    r = s.add_parser("run", help="Create and execute a real DeepSeek workflow")
    r.add_argument("goal")
    r.add_argument("--owner", default="planner")
    r.add_argument("--acceptance", help="Path to frozen acceptance JSON list")
    budgets(r)
    s.add_parser("resume", help="Recover all durable work and execute until idle")
    s.add_parser("inspect", help="Show workflow/task summaries")
    s.add_parser("pending", help="List unanswered human requests")
    a = s.add_parser("answer", help="Record a response; invoke resume separately")
    a.add_argument("approval_id")
    a.add_argument("decision", choices=["approve", "deny", "answer"])
    a.add_argument("--text")
    c = s.add_parser("cancel")
    c.add_argument("workflow_id")
    e = s.add_parser("events", help="Export trace; no raw model reasoning or credentials")
    e.add_argument("--workflow")
    e.add_argument("--output")
    artifact = s.add_parser("export", help="Export an authorized published artifact to a new local file")
    artifact.add_argument("artifact_id")
    artifact.add_argument("--output", required=True)
    evaluation = s.add_parser("evaluate", help="Run a fixed external case suite with real DeepSeek")
    evaluation.add_argument("suite")
    evaluation.add_argument("--report", required=True)
    budgets(evaluation)
    reconciliation = s.add_parser("reconcile", help="Inspect an UNKNOWN Git push against its configured remote; never repush")
    reconciliation.add_argument("operation_id")
    chat = s.add_parser("chat", help="Send human message to one agent or an explicit group mention")
    chat.add_argument("text")
    chat.add_argument("--to", required=True, help="Comma-separated registered agent IDs")
    chat.add_argument("--group", action="store_true")
    budgets(chat)
    d = s.add_parser("demo", help="Offline scripted fixture; no real LLM or code execution")
    d.add_argument("--auto-answer", action="store_true", help="Answer this fixture's clarification with yes")
    probe_command = s.add_parser("probe", help="Spend up to two model calls to test tools + thinking roundtrip")
    probe_command.add_argument("--report", help="Save status and known usage to a new JSON file, including failures")
    doctor = s.add_parser("doctor", help="Check local configuration and Docker without API calls or creating a database")
    doctor.add_argument("--require-live", action="store_true", help="Fail if API key/model configuration is missing or invalid")
    doctor.add_argument("--require-docker", action="store_true", help="Fail unless the Docker daemon and preinstalled sandbox image are available")
    return p


class ProcessLock:
    """Single writer/process ownership; works on Unix and Windows."""
    def __init__(self, path):
        self.path = Path(str(path) + ".lock")
        self.f = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                self.f.write(b"0")
                self.f.flush()
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            raise PlatformError("PROCESS_LOCKED", "Another CLI process owns this database")
        return self

    def __exit__(self, *args):
        self.f.close()


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def summary(store):
    return {"workflows": store.all("SELECT id,root_task_id,status,deadline FROM workflows ORDER BY created"),
            "tasks": store.all("SELECT id,parent_id,kind,owner,status,retries,resumes,repairs,error,result_json FROM tasks ORDER BY created")}


def require_idle(store):
    if store.one("SELECT id FROM workflows WHERE status='OPEN' LIMIT 1"):
        raise PlatformError("DATABASE_BUSY", "Use a fresh database or finish/cancel existing work before creating another run. Use resume to continue it explicitly.")


def workflow_exit_code(store, workflow_ids):
    return int(any(store.one("SELECT status FROM workflows WHERE id=?", (wid,))["status"] != "COMPLETED"
                   for wid in workflow_ids))


def integrations(args, *, api_key=None):
    from .adapters import BraveSearchProvider, GitGateway
    mode = getattr(args, "web", None) or os.environ.get("MULTI_AGENT_WEB_PROVIDER") or ("deepseek" if args.command in {"shell", "interactive"} else "brave")
    if mode == "deepseek":
        from .web import DeepSeekWebProvider
        search = DeepSeekWebProvider(api_key=api_key, timeout=getattr(args, "tool_timeout", 20))
    elif mode == "brave":
        search = BraveSearchProvider(os.environ["BRAVE_SEARCH_API_KEY"]) if os.environ.get("BRAVE_SEARCH_API_KEY") else None
    elif mode == "off":
        search = None
    else:
        raise PlatformError("CONFIGURATION", "MULTI_AGENT_WEB_PROVIDER must be deepseek, brave or off")
    base = os.environ.get("MULTI_AGENT_GIT_REPO")
    git = GitGateway(Path(base), Path(os.environ.get("MULTI_AGENT_GIT_STAGING", str(Path(args.db).parent / "git-staging"))),
                     remote_url=os.environ.get("MULTI_AGENT_GIT_REMOTE") or None) if base else None
    return search, git


async def probe(provider=None, *, report=None):
    from .runtime import function
    from .provider import DeepSeekProvider
    result = {"complete": False, "status": "NOT_FINISHED", "native_tool_roundtrip": False,
              "reasoning_roundtrip": False, "calls_attempted": 0, "usage": [], "usage_complete": True,
              "note": "At most two live calls; this does not validate all provider capabilities. Missing usage is unknown, not zero."}
    destination = Path(report) if report else None
    if destination:
        destination.parent.mkdir(parents=True, exist_ok=True)
    # Reserve before provider setup or any paid request; reject existing files,
    # database aliases and symlinks without overwriting them.
    with destination.open("x", encoding="utf-8") if destination else contextlib.nullcontext() as handle:
        def save():
            result["usage_complete"] = all(isinstance(item, dict) and "total_tokens" in item for item in result["usage"])
            if handle:
                handle.seek(0)
                handle.write(json.dumps(result, ensure_ascii=False, indent=2))
                handle.truncate()
                handle.flush()

        def counters(usage):
            # Provider extensions are untrusted and can contain raw content.
            return {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                    if isinstance(usage, dict) and type(usage.get(key)) is int and usage[key] >= 0}

        async def complete():
            result["calls_attempted"] += 1
            result["usage"].append(None)
            save()
            try:
                completion = await provider.complete(messages, tools, task={"id": "probe"}, purpose="PROBE")
            except PlatformError as exc:
                if hasattr(exc, "usage"):
                    result["usage"][-1] = counters(exc.usage)
                raise
            result["usage"][-1] = counters(completion.usage)
            save()
            return completion

        save()
        try:
            if provider is None:
                provider = DeepSeekProvider.from_env()
            tools = [function("probe_echo", "Echo the supplied text", {"text": {"type": "string"}}, ("text",))]
            messages = [{"role": "user", "content": 'Call probe_echo with text="roundtrip". After its result, reply OK.'}]
            first = await complete()
            result["reasoning_roundtrip"] = bool(first.message.get("reasoning_content"))
            calls = first.message.get("tool_calls", [])
            try:
                valid = bool(calls) and all(c["function"]["name"] == "probe_echo" and json.loads(c["function"]["arguments"]) == {"text": "roundtrip"} for c in calls)
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                raise PlatformError("PROBE_FAILED", "Model did not issue the expected tool call")
            messages.append(first.message)
            for call in calls:
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": dumps({"text": "roundtrip"})})
            second = await complete()
            passed = not second.message.get("tool_calls") and (second.message.get("content") or "").strip() == "OK"
            result.update(complete=True, status="PASS" if passed else "FAIL", native_tool_roundtrip=passed)
            if not passed:
                result["error"] = {"code": "PROBE_FAILED", "message": "Model did not finish with the expected OK response"}
            save()
            print_json(result)
            return 0 if passed else 1
        except BaseException as exc:
            interrupted = isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError))
            result.update(complete=not interrupted, status="INTERRUPTED" if interrupted else "FAIL")
            result["error"] = exc.as_dict()["error"] if isinstance(exc, PlatformError) else {
                "code": "PROBE_INTERRUPTED" if interrupted else "PROBE_ERROR",
                "message": "Probe interrupted; request processing and billing may be unknown." if interrupted else "Probe failed unexpectedly; raw exception text was not saved.",
            }
            save()
            raise


async def execute(args, store):
    from .provider import DeepSeekProvider
    from .tools import DockerSandbox, MemorySandbox, ToolGateway
    from .runtime import Runtime, validate_acceptance
    from .comms import CommunicationService
    if args.command in {"shell", "interactive"}:
        return await interactive(args, store)
    if args.command == "inspect":
        print_json(summary(store)); return
    if args.command == "pending":
        print_json(store.all("SELECT id,workflow_id,task_id,tool_name,args_json,status FROM approvals WHERE status IN ('OPEN','PENDING') ORDER BY created")); return
    if args.command == "answer":
        print_json(store.answer(args.approval_id, args.decision, args.text)); return
    if args.command == "cancel":
        wf = store.one("SELECT * FROM workflows WHERE id=?", (args.workflow_id,))
        if not wf:
            raise PlatformError("NOT_FOUND", "Workflow not found")
        store.fail(wf["root_task_id"], "USER_CANCELLED", status="CANCELLED")
        print_json(summary(store)); return
    if args.command == "export":
        artifact = store.one("SELECT a.* FROM artifacts a JOIN workflows w ON w.id=a.workflow_id WHERE a.id=? AND w.user_id='human:local'", (args.artifact_id,))
        if not artifact:
            raise PlatformError("ACCESS_DENIED", "Artifact not found or not owned by this local user")
        content = bytes(artifact["content"])
        if hashlib.sha256(content).hexdigest() != artifact["sha256"]:
            raise PlatformError("ARTIFACT_CORRUPT", "Artifact checksum mismatch")
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as handle:
            handle.write(content)
        print_json({"path": str(destination), "sha256": artifact["sha256"], "bytes": len(content)}); return
    if args.command == "reconcile":
        _, git = integrations(args)
        op = store.one("SELECT * FROM operations WHERE id=?", (args.operation_id,))
        if not git or not op or op["tool_name"] != "git_push" or op["status"] != "UNKNOWN":
            raise PlatformError("RECONCILE_UNAVAILABLE", "Specify an UNKNOWN Git push and its configured Git gateway")
        approval = store.one("SELECT * FROM approvals WHERE task_id=? AND tool_name='git_push' AND args_hash=? AND status='APPROVED' ORDER BY created DESC LIMIT 1", (op["task_id"], op["args_hash"]))
        if not approval:
            raise PlatformError("APPROVAL_MISMATCH", "No exact approved arguments are available")
        arguments = json.loads(approval["args_json"])
        if digest(arguments) != op["args_hash"]:
            raise PlatformError("APPROVAL_MISMATCH", "Approved arguments no longer match the operation")
        task = store.one("SELECT * FROM tasks WHERE id=?", (op["task_id"],))
        result = await git.reconcile_push(task, arguments)
        # The adapter reports evidence; the command never starts another push.
        if result.get("matched") is True:
            store.finish_operation(op["id"], result)
        print_json(result); return
    if args.command == "events":
        rows = store.all("SELECT * FROM events" + (" WHERE workflow_id=?" if args.workflow else "") + " ORDER BY created,id", (args.workflow,) if args.workflow else ())
        data = "\n".join(dumps(row) for row in rows) + "\n"
        if args.output:
            destination = Path(args.output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("x", encoding="utf-8") as handle:
                handle.write(data)
        else:
            print(data, end="")
        return
    if args.command == "demo":
        from .demo import DemoProvider
        policy = Policy()
        provider, sandbox = DemoProvider(), MemorySandbox()
        require_idle(store)
        workflow = store.create_workflow("Offline collaboration protocol fixture", acceptance=[{"kind": "artifact", "name": "add.py"}], policy=policy)
        gateway = ToolGateway(store, sandbox, policy)
        runtime = Runtime(store, provider, gateway, policy)
        await runtime.run_until_idle()
        if args.auto_answer:
            for item in store.all("SELECT * FROM approvals WHERE workflow_id=? AND status IN ('OPEN','PENDING') AND tool_name='ask_human'", (workflow["id"],)):
                store.answer(item["id"], "answer", "yes")
            await runtime.run_until_idle()
        print_json({"mode": "SCRIPTED_FILE_ONLY_FIXTURE", **summary(store)})
        return workflow_exit_code(store, [workflow["id"]])
    if args.command in {"run", "chat", "evaluate"}:
        require_idle(store)
    if args.command == "resume" and store.one("SELECT 1 FROM events e JOIN workflows w ON w.id=e.workflow_id WHERE w.status='OPEN' AND ((e.type='ExecutionMode' AND json_extract(e.payload_json,'$.mode')='fixture') OR (e.type='RunConfiguration' AND (json_extract(e.payload_json,'$.provider_class') IN ('DemoProvider','ScriptedProvider','TerminalDemoProvider') OR json_extract(e.payload_json,'$.sandbox_class')='MemorySandbox'))) LIMIT 1"):
        raise PlatformError("FIXTURE_DATABASE", "This database contains unfinished offline fixtures. Real-provider resume is disabled; use a fresh live database.")
    if args.command == "resume" and store.one("SELECT 1 FROM session_turns s JOIN workflows w ON w.id=s.workflow_id WHERE w.status='OPEN' AND NOT EXISTS (SELECT 1 FROM events e WHERE e.workflow_id=w.id AND e.type IN ('ExecutionMode','RunConfiguration')) LIMIT 1"):
        raise PlatformError("SESSION_MODE_UNKNOWN", "An unfinished session has no execution mode record. Cancel it before starting a new turn; real-provider resume is disabled.")
    if args.command == "probe":
        return await probe(report=args.report)
    provider = DeepSeekProvider.from_env()
    policy = Policy(deadline_seconds=getattr(args, "deadline", 600),
                    max_calls=getattr(args, "max_calls", 60), max_tasks=getattr(args, "max_tasks", 20),
                    max_web_requests=getattr(args, "max_web_requests", 20),
                    tool_timeout=getattr(args, "tool_timeout", 20),
                    max_output_tokens=provider.max_output_tokens, max_context_chars=provider.max_context_chars)
    workflow_ids = [w["id"] for w in store.all("SELECT id FROM workflows WHERE status='OPEN'")]
    sandbox = DockerSandbox(image=os.environ.get("MULTI_AGENT_SANDBOX_IMAGE", "python:3.12-slim"))
    try:
        search, git = integrations(args)
        if args.command == "run":
            acceptance = json.loads(Path(args.acceptance).read_text(encoding="utf-8")) if args.acceptance else [{"kind": "nonempty"}]
            validate_acceptance(acceptance)
            workflow_ids = [store.create_workflow(args.goal, owner=args.owner, acceptance=acceptance, policy=policy)["id"]]
        elif args.command == "chat":
            result = CommunicationService(store).create(args.text, recipients=[recipient.strip() for recipient in args.to.split(",")], group=args.group, policy=policy)
            workflow_ids = [w["id"] for w in result["workflows"]]
        gateway = ToolGateway(store, sandbox, policy, search=search, git=git)
        if args.command == "evaluate":
            from .evaluation import _freeze_suite, evaluate_suite
            suite = json.loads(Path(args.suite).read_text(encoding="utf-8"))
            _freeze_suite(suite)
            path = Path(args.report)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Reserve a new destination before paying for any model calls.
            # Exclusive creation also rejects DB/suite aliases and symlinks.
            with path.open("x", encoding="utf-8") as handle:
                handle.write(dumps({"complete": False, "status": "NOT_FINISHED", "note": "No final report yet. Inspect the database before resuming."}))
                handle.flush()
                report = await evaluate_suite(store, provider, gateway, suite, policy)
                handle.seek(0)
                handle.write(json.dumps(report, ensure_ascii=False, indent=2))
                handle.truncate()
            print_json(report)
            return int(report["summary"]["failed"] > 0 or not report["complete"])
        await Runtime(store, provider, gateway, policy).run_until_idle(max_seconds=policy.deadline_seconds + 1)
        for task in store.all("SELECT * FROM tasks WHERE status='COMPLETED' AND kind='REPLY' AND parent_id IS NULL"):
            CommunicationService(store).materialize_root_reply(task)
        print_json(summary(store))
        return workflow_exit_code(store, workflow_ids)
    finally:
        await sandbox.cleanup()


async def interactive(args, store):
    from .terminal import TerminalController, run_plain, run_tui
    from .tools import DockerSandbox, MemorySandbox, ToolGateway
    if args.session and args.new:
        raise PlatformError("CONFIGURATION", "Choose either --session or --new")
    if args.demo:
        from .terminal_demo import TerminalDemoProvider, DemoGitGateway
        provider, sandbox, search, git = TerminalDemoProvider(), MemorySandbox(), None, DemoGitGateway()
    else:
        from .provider import DeepSeekProvider
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key and sys.stdin.isatty():
            import getpass
            key = getpass.getpass("DeepSeek API key（隱藏輸入，僅存本程序記憶體）: ").strip()
        provider = DeepSeekProvider.from_env(api_key=key, model=os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
        sandbox = DockerSandbox(image=os.environ.get("MULTI_AGENT_SANDBOX_IMAGE", "python:3.12-slim"))
        search, git = integrations(args, api_key=key)
    policy = Policy(deadline_seconds=args.deadline, max_calls=args.max_calls, max_tasks=args.max_tasks,
                    max_web_requests=args.max_web_requests, max_depth=args.max_depth, max_children=args.max_children,
                    tool_timeout=args.tool_timeout,
                    max_output_tokens=getattr(provider, "max_output_tokens", 8192),
                    max_context_chars=getattr(provider, "max_context_chars", 120000))
    try:
        gateway = ToolGateway(store, sandbox, policy, search=search, git=git)
        controller = TerminalController(store, provider, gateway, policy=policy, session_id=args.session,
                                        new=args.new, owner=args.owner, demo=args.demo, parallelism=args.parallelism)
        if args.plain or not (sys.stdin.isatty() and sys.stdout.isatty()):
            await run_plain(controller)
        else:
            await run_tui(controller)
        print("會話已保存：" + controller.session["id"])
        print("資料庫：" + str(Path(args.db).resolve()))
        return 0
    finally:
        await sandbox.cleanup()


def main():
    args = parser().parse_args(sys.argv[1:] or ["shell"])
    from .store import Store
    try:
        if args.command == "doctor":
            from .preflight import diagnose
            report = diagnose(require_live=args.require_live, require_docker=args.require_docker)
            print_json(report)
            return 0 if report["ready"] else 1
        if args.command == "probe":
            return asyncio.run(probe(report=args.report))
        with ProcessLock(args.db):
            store = Store(args.db)
            try:
                return asyncio.run(execute(args, store)) or 0
            finally:
                store.close()
    except PlatformError as exc:
        print_json(exc.as_dict()); return 2
    except KeyboardInterrupt:
        print("Interrupted. Durable work can be resumed.", file=sys.stderr); return 130
    except (ValueError, OSError) as exc:
        print_json({"error": {"code": "CONFIGURATION", "message": str(exc)}}); return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
