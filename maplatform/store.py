"""Transactional SQLite task service for the single-host prototype.

This module owns task transitions. Callers may read the database, but model output
never provides an actor identity, attempt identity, or permission grant.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

from .domain import AGENTS, TERMINAL, PlatformError, Policy, digest, dumps, new_id


_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows(
 id TEXT PRIMARY KEY, root_task_id TEXT NOT NULL, user_id TEXT NOT NULL,
 status TEXT NOT NULL, deadline REAL NOT NULL, max_calls INTEGER NOT NULL,
 max_tasks INTEGER NOT NULL, max_a2a INTEGER NOT NULL, created REAL NOT NULL,
 policy_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflows(id),
 parent_id TEXT REFERENCES tasks(id), kind TEXT NOT NULL, owner TEXT,
 goal TEXT NOT NULL, required_json TEXT NOT NULL, input_artifacts_json TEXT NOT NULL,
 acceptance_json TEXT NOT NULL, status TEXT NOT NULL, active_attempt_id TEXT,
 next_kind TEXT, retries INTEGER NOT NULL DEFAULT 0, repairs INTEGER NOT NULL DEFAULT 0,
 resumes INTEGER NOT NULL DEFAULT 0, depth INTEGER NOT NULL, result_json TEXT,
 error TEXT, created REAL NOT NULL, updated REAL NOT NULL, conversation_id TEXT);
CREATE INDEX IF NOT EXISTS tasks_poll ON tasks(status,created);
CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent_id);
CREATE TABLE IF NOT EXISTS attempts(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), kind TEXT NOT NULL,
 status TEXT NOT NULL, started REAL NOT NULL, ended REAL);
CREATE TABLE IF NOT EXISTS snapshots(
 task_id TEXT PRIMARY KEY REFERENCES tasks(id), messages_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS waits(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), tool_call_id TEXT NOT NULL,
 kind TEXT NOT NULL, ref_id TEXT, status TEXT NOT NULL, result_json TEXT,
 deadline REAL NOT NULL, args_json TEXT NOT NULL, UNIQUE(task_id,tool_call_id));
CREATE INDEX IF NOT EXISTS waits_ref ON waits(ref_id,status);
CREATE TABLE IF NOT EXISTS approvals(
 id TEXT PRIMARY KEY, wait_id TEXT NOT NULL UNIQUE REFERENCES waits(id),
 workflow_id TEXT NOT NULL, task_id TEXT NOT NULL, tool_name TEXT NOT NULL,
 args_json TEXT NOT NULL, args_hash TEXT NOT NULL, status TEXT NOT NULL,
 answer_json TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS conversations(
 id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, visibility TEXT NOT NULL,
 members_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(
 id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
 sender TEXT NOT NULL, recipient TEXT NOT NULL, visibility TEXT NOT NULL,
 kind TEXT NOT NULL, content TEXT NOT NULL, reply_to TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT NOT NULL, task_id TEXT,
 type TEXT NOT NULL, payload_json TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS llm_calls(
 id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, task_id TEXT NOT NULL,
 attempt_id TEXT, purpose TEXT NOT NULL, status TEXT NOT NULL,
 request_hash TEXT NOT NULL, usage_json TEXT, error TEXT, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS calls_workflow ON llm_calls(workflow_id);
CREATE TABLE IF NOT EXISTS operations(
 id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, task_id TEXT NOT NULL,
 attempt_id TEXT NOT NULL, call_key TEXT NOT NULL UNIQUE, effect_key TEXT UNIQUE,
 tool_name TEXT NOT NULL, args_hash TEXT NOT NULL, status TEXT NOT NULL,
 result_json TEXT, error TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS operation_calls(
 call_key TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operations(id),
 task_id TEXT NOT NULL, tool_name TEXT NOT NULL, args_hash TEXT NOT NULL, effect_key TEXT);
CREATE TABLE IF NOT EXISTS artifacts(
 id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, task_id TEXT NOT NULL,
 owner TEXT NOT NULL, name TEXT NOT NULL, sha256 TEXT NOT NULL,
 media_type TEXT NOT NULL, content BLOB NOT NULL, grantees_json TEXT NOT NULL,
 created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memories(
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, user_id TEXT NOT NULL,
 workflow_id TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS bids(
 task_id TEXT NOT NULL, agent_id TEXT NOT NULL, result_json TEXT NOT NULL,
 UNIQUE(task_id,agent_id));
CREATE TABLE IF NOT EXISTS sessions(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
 owner TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id,updated);
CREATE TABLE IF NOT EXISTS session_turns(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 ordinal INTEGER NOT NULL, workflow_id TEXT NOT NULL UNIQUE REFERENCES workflows(id),
 owner TEXT NOT NULL, user_text TEXT NOT NULL, status TEXT NOT NULL,
 assistant_text TEXT, artifact_ids_json TEXT NOT NULL DEFAULT '[]',
 error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
 UNIQUE(session_id,ordinal));
"""


def _error(code, message):
    return PlatformError(code, message)


def _text(value, field, max_len=100000):
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise _error("INVALID_ARGUMENT", f"{field} must be a nonempty string of at most {max_len} characters")
    return value


def _strings(value, field):
    if not isinstance(value, (list, tuple)) or any(not isinstance(x, str) or not x for x in value):
        raise _error("INVALID_ARGUMENT", f"{field} must be a list of strings")
    return list(dict.fromkeys(value))


def workflow_scope(workflow_ids, column="workflow_id"):
    """SQL suffix and parameters for a trusted caller's selected workflows."""
    if workflow_ids is None:
        return "", ()
    if isinstance(workflow_ids, str):
        raise _error("INVALID_ARGUMENT", "workflow_ids must be a collection of IDs")
    ids = tuple(workflow_ids)
    if any(not isinstance(value, str) or not value for value in ids):
        raise _error("INVALID_ARGUMENT", "workflow_ids must contain nonempty strings")
    ids = tuple(dict.fromkeys(ids))
    if not ids:
        return " AND 0", ()
    return f" AND {column} IN ({','.join('?' for _ in ids)})", ids


class Store:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            self.conn.close()
            raise _error("SCHEMA_VERSION", "Unsupported database schema version")
        self.conn.executescript(_SCHEMA)
        self.conn.execute("PRAGMA user_version=2")

    @contextmanager
    def tx(self):
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def one(self, sql, params=()):
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None

    def all(self, sql, params=()):
        with self._lock:
            return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def close(self):
        with self._lock:
            self.conn.close()

    def event(self, conn, workflow_id, task_id, event_type, payload):
        conn.execute("INSERT INTO events(workflow_id,task_id,type,payload_json,created) VALUES(?,?,?,?,?)",
                     (workflow_id, task_id, event_type, dumps(payload), time.time()))

    def _task(self, conn, task_id):
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise _error("TASK_NOT_FOUND", "Task does not exist")
        return dict(row)

    def _workflow(self, conn, workflow_id):
        row = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise _error("WORKFLOW_NOT_FOUND", "Workflow does not exist")
        return dict(row)

    def _bounds(self, wf, supplied=None):
        values = asdict(Policy())
        values.update(json.loads(wf["policy_json"]))
        if supplied is not None:
            extra = asdict(supplied) if is_dataclass(supplied) else vars(supplied)
            for key, value in extra.items():
                if key in values and isinstance(value, (int, float)):
                    values[key] = min(values[key], value)
        return SimpleNamespace(**values)

    def _active(self, conn, task_id, attempt_id):
        task = self._task(conn, task_id)
        attempt = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if (not attempt_id or task["status"] != "RUNNING" or task["active_attempt_id"] != attempt_id
                or attempt is None or attempt["task_id"] != task_id or attempt["status"] != "RUNNING"):
            raise _error("STALE_ATTEMPT", "Attempt no longer owns this task")
        wf = self._workflow(conn, task["workflow_id"])
        if wf["status"] != "OPEN" or wf["deadline"] <= time.time():
            raise _error("DEADLINE_EXCEEDED", "Workflow is no longer active")
        return task, wf

    def assert_active(self, task_id, attempt_id):
        with self._lock:
            self._active(self.conn, task_id, attempt_id)

    def _insert_task(self, conn, task_id, workflow_id, goal, owner, required, acceptance,
                     *, parent=None, kind="WORK", inputs=(), conversation=None):
        now = time.time()
        conn.execute("""INSERT INTO tasks(id,workflow_id,parent_id,kind,owner,goal,required_json,
          input_artifacts_json,acceptance_json,status,next_kind,depth,created,updated,conversation_id)
          VALUES(?,?,?,?,?,?,?,?,?,'CREATED','FIRST',?,?,?,?)""",
                     (task_id, workflow_id, parent["id"] if parent else None, kind, owner, goal,
                      dumps(required), dumps(inputs), dumps(acceptance), parent["depth"] + 1 if parent else 0,
                      now, now, conversation))
        self.event(conn, workflow_id, task_id, "TaskCreated", {"kind": kind, "parent_id": parent["id"] if parent else None})

    def _check_owner(self, owner, required):
        if not isinstance(owner, str) or owner not in AGENTS:
            raise _error("UNKNOWN_AGENT", "Agent is not in the registry")
        if not set(required).issubset(set(AGENTS[owner].capabilities)):
            raise _error("CAPABILITY_MISMATCH", "Agent does not have all required capabilities")

    def create_workflow(self, goal, user_id="human:local", owner="planner", required=(), acceptance=(), policy=None):
        with self.tx() as conn:
            return self._create_workflow(conn, goal, user_id, owner, required, acceptance, policy)

    def _create_workflow(self, conn, goal, user_id="human:local", owner="planner", required=(), acceptance=(), policy=None):
        """Create a workflow within an existing application transaction."""
        _text(goal, "goal")
        _text(user_id, "user_id", 256)
        required = _strings(required, "required")
        if not isinstance(acceptance, (list, tuple)):
            raise _error("INVALID_ARGUMENT", "acceptance must be a list")
        if owner is not None:
            self._check_owner(owner, required)
        chosen = policy or Policy()
        values = asdict(chosen)
        for key, value in values.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise _error("INVALID_POLICY", f"Invalid policy value for {key}")
        for key in ("max_calls", "max_tasks", "max_reply_calls", "deadline_seconds", "tool_timeout", "max_output_tokens", "max_context_chars"):
            if values[key] <= 0:
                raise _error("INVALID_POLICY", f"{key} must be positive")
        wf_id, root_id = new_id("wf"), new_id("task")
        now = time.time()
        conn.execute("""INSERT INTO workflows VALUES(?,?,?,'OPEN',?,?,?,?,?,?)""",
                     (wf_id, root_id, user_id, now + chosen.deadline_seconds, chosen.max_calls,
                      chosen.max_tasks, chosen.max_a2a, now, dumps(values)))
        self._insert_task(conn, root_id, wf_id, goal, owner, required, list(acceptance))
        self.event(conn, wf_id, root_id, "WorkflowCreated", {"user_id": user_id})
        return self._workflow(conn, wf_id)

    def route(self, task_id, owner):
        with self.tx() as conn:
            task = self._task(conn, task_id)
            if task["status"] != "CREATED":
                raise _error("ILLEGAL_TRANSITION", "Only CREATED tasks can be routed")
            wf = self._workflow(conn, task["workflow_id"])
            if wf["status"] != "OPEN" or wf["deadline"] <= time.time():
                raise _error("DEADLINE_EXCEEDED", "Workflow is no longer active")
            self._check_owner(owner, json.loads(task["required_json"]))
            for artifact_id in json.loads(task["input_artifacts_json"]):
                artifact = conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
                if artifact is None or artifact["workflow_id"] != task["workflow_id"] or not (
                        artifact["task_id"] == task_id and artifact["owner"] == owner
                        or owner in json.loads(artifact["grantees_json"])):
                    raise _error("ARTIFACT_FORBIDDEN", "Assigned agent cannot read an input artifact")
            conn.execute("UPDATE tasks SET owner=?,status='READY',next_kind='FIRST',updated=? WHERE id=?",
                         (owner, time.time(), task_id))
            self.event(conn, task["workflow_id"], task_id, "TaskRouted", {"owner": owner})
            return self._task(conn, task_id)

    def claim(self, task_id):
        with self.tx() as conn:
            task = self._task(conn, task_id)
            if task["status"] != "READY":
                return None
            wf = self._workflow(conn, task["workflow_id"])
            if wf["status"] != "OPEN":
                return None
            if wf["deadline"] <= time.time():
                self._terminal(conn, self._task(conn, wf["root_task_id"]), "TIMED_OUT", "DEADLINE")
                return None
            self._check_owner(task["owner"], json.loads(task["required_json"]))
            attempt_id = new_id("attempt")
            now = time.time()
            kind = task["next_kind"] or "FIRST"
            conn.execute("INSERT INTO attempts VALUES(?,?,?,'RUNNING',?,NULL)", (attempt_id, task_id, kind, now))
            conn.execute("UPDATE tasks SET status='RUNNING',active_attempt_id=?,next_kind=NULL,updated=? WHERE id=?",
                         (attempt_id, now, task_id))
            self.event(conn, task["workflow_id"], task_id, "AttemptStarted", {"attempt_id": attempt_id, "kind": kind})
            return self._task(conn, task_id)

    def snapshot(self, task_id):
        row = self.one("SELECT messages_json FROM snapshots WHERE task_id=?", (task_id,))
        return json.loads(row["messages_json"]) if row else []

    def save_snapshot(self, task_id, attempt_id, messages):
        if not isinstance(messages, list):
            raise _error("INVALID_ARGUMENT", "Snapshot must be a list of messages")
        encoded = dumps(messages)
        with self.tx() as conn:
            task, _ = self._active(conn, task_id, attempt_id)
            conn.execute("INSERT INTO snapshots VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET messages_json=excluded.messages_json", (task_id, encoded))
            self.event(conn, task["workflow_id"], task_id, "SnapshotSaved", {"attempt_id": attempt_id, "messages": len(messages)})

    def reserve_call(self, task_id, attempt_id, purpose, request_hash):
        _text(purpose, "purpose", 32)
        _text(request_hash, "request_hash", 256)
        with self.tx() as conn:
            if attempt_id is not None:
                task, wf = self._active(conn, task_id, attempt_id)
            else:
                task = self._task(conn, task_id)
                wf = self._workflow(conn, task["workflow_id"])
                allowed = (purpose == "BID" and task["status"] == "CREATED") or (purpose == "VERIFY" and task["status"] == "VERIFYING")
                if not allowed:
                    raise _error("STALE_ATTEMPT", "Only routing and verification calls can omit an attempt")
                if wf["status"] != "OPEN" or wf["deadline"] <= time.time():
                    raise _error("DEADLINE_EXCEEDED", "Workflow is no longer active")
            count = conn.execute("SELECT count(*) FROM llm_calls WHERE workflow_id=?", (wf["id"],)).fetchone()[0]
            bounds = self._bounds(wf)
            if count >= bounds.max_calls:
                raise _error("BUDGET_EXCEEDED", "Workflow model-call budget is exhausted")
            if task["kind"] == "REPLY":
                count = conn.execute("SELECT count(*) FROM llm_calls WHERE task_id=?", (task_id,)).fetchone()[0]
                if count >= bounds.max_reply_calls:
                    raise _error("BUDGET_EXCEEDED", "Reply model-call budget is exhausted")
            call_id = new_id("call")
            conn.execute("INSERT INTO llm_calls VALUES(?,?,?,?,?,'STARTED',?,NULL,NULL,?)",
                         (call_id, wf["id"], task_id, attempt_id, purpose, request_hash, time.time()))
            self.event(conn, wf["id"], task_id, "ModelCallReserved", {"call_id": call_id, "purpose": purpose})
            return call_id

    def finish_call(self, call_id, usage=None, error=None, *, status=None):
        status = ("FAILED" if error else "SUCCEEDED") if status is None else status
        if status not in ("SUCCEEDED", "FAILED", "UNKNOWN") or (error and status == "SUCCEEDED"):
            raise _error("INVALID_ARGUMENT", "Invalid model-call outcome")
        with self.tx() as conn:
            call = conn.execute("SELECT * FROM llm_calls WHERE id=?", (call_id,)).fetchone()
            if call is None:
                raise _error("CALL_NOT_FOUND", "Model call does not exist")
            if call["status"] not in ("STARTED", "RUNNING"):
                return
            conn.execute("UPDATE llm_calls SET status=?,usage_json=?,error=? WHERE id=?", (status, dumps(usage or {}), str(error) if error else None, call_id))
            self.event(conn, call["workflow_id"], call["task_id"], "ModelCallFinished", {"call_id": call_id, "status": status})

    def defer(self, task_id, attempt_id, tool_call_id, kind, args, policy):
        _text(tool_call_id, "tool_call_id", 256)
        if not isinstance(args, dict):
            raise _error("INVALID_ARGUMENT", "Deferred arguments must be an object")
        args_json = dumps(args)
        with self.tx() as conn:
            task, wf = self._active(conn, task_id, attempt_id)
            old = conn.execute("SELECT * FROM waits WHERE task_id=? AND tool_call_id=?", (task_id, tool_call_id)).fetchone()
            if old:
                if old["kind"] != kind or old["args_json"] != args_json:
                    raise _error("IDEMPOTENCY_CONFLICT", "Deferred call ID was reused with different arguments")
                return dict(old)
            wait_id = new_id("wait")
            deadline = wf["deadline"]
            conn.execute("INSERT INTO waits VALUES(?,?,?,?,NULL,'OPEN',NULL,?,?)", (wait_id, task_id, tool_call_id, kind, deadline, args_json))
            try:
                # A savepoint keeps rejection recording atomic without retaining a half-created child.
                conn.execute("SAVEPOINT deferred_business")
                ref_id = self._defer_business(conn, task, wf, wait_id, kind, args, self._bounds(wf, policy))
                conn.execute("UPDATE waits SET ref_id=? WHERE id=?", (ref_id, wait_id))
                conn.execute("RELEASE deferred_business")
                self.event(conn, wf["id"], task_id, "WaitCreated", {"wait_id": wait_id, "kind": kind, "ref_id": ref_id})
            except PlatformError as exc:
                conn.execute("ROLLBACK TO deferred_business")
                conn.execute("RELEASE deferred_business")
                result = {"error": {"code": exc.code, "message": str(exc)}}
                conn.execute("UPDATE waits SET status='RESOLVED',result_json=? WHERE id=?", (dumps(result), wait_id))
                self.event(conn, wf["id"], task_id, "DeferredRejected", {"wait_id": wait_id, **result})
            return dict(conn.execute("SELECT * FROM waits WHERE id=?", (wait_id,)).fetchone())

    def _defer_business(self, conn, task, wf, wait_id, kind, args, bounds):
        if task["kind"] == "REPLY":
            raise _error("REPLY_TOOL_FORBIDDEN", "Reply tasks cannot delegate or request another reply")
        if kind not in ("CHILD_TASK", "AGENT_REPLY", "HUMAN_CLARIFY", "HUMAN_APPROVAL"):
            raise _error("INVALID_ARGUMENT", "Unknown deferred tool kind")
        allowed = {
            "CHILD_TASK": {"goal", "required_capabilities", "target_agent", "input_artifact_ids", "acceptance"},
            "AGENT_REPLY": {"target_agent", "question"},
            "HUMAN_CLARIFY": {"question"},
            "HUMAN_APPROVAL": {"tool_name", "arguments"},
        }[kind]
        if set(args) - allowed:
            raise _error("INVALID_ARGUMENT", "Unexpected deferred argument")
        if kind in ("HUMAN_APPROVAL", "HUMAN_CLARIFY"):
            if kind == "HUMAN_APPROVAL":
                tool_name = _text(args.get("tool_name"), "tool_name", 128)
                if tool_name not in AGENTS[task["owner"]].tools:
                    raise _error("TOOL_FORBIDDEN", "Agent cannot request this tool")
                arguments = args.get("arguments")
                if not isinstance(arguments, dict):
                    raise _error("INVALID_ARGUMENT", "Approval arguments must be an object")
            else:
                tool_name, arguments = "ask_human", {"question": _text(args.get("question"), "question")}
            approval_id = new_id("approval")
            conn.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,'OPEN',NULL,?)",
                         (approval_id, wait_id, wf["id"], task["id"], tool_name, dumps(arguments), digest(arguments), time.time()))
            return approval_id
        if task["depth"] >= bounds.max_depth:
            raise _error("DEPTH_EXCEEDED", "Maximum task depth reached")
        children = conn.execute("SELECT count(*) FROM tasks WHERE parent_id=?", (task["id"],)).fetchone()[0]
        if children >= bounds.max_children:
            raise _error("CHILDREN_EXCEEDED", "Maximum child-task count reached")
        task_count = conn.execute("SELECT count(*) FROM tasks WHERE workflow_id=?", (wf["id"],)).fetchone()[0]
        if task_count >= bounds.max_tasks:
            raise _error("TASK_BUDGET_EXCEEDED", "Workflow task budget is exhausted")
        new_task_id = new_id("task")
        if kind == "CHILD_TASK":
            goal = _text(args.get("goal"), "goal")
            required = _strings(args.get("required_capabilities", []), "required_capabilities")
            inputs = _strings(args.get("input_artifact_ids", []), "input_artifact_ids")
            acceptance = args.get("acceptance", [])
            if not isinstance(acceptance, list):
                raise _error("INVALID_ARGUMENT", "acceptance must be a list")
            owner = args.get("target_agent")
            if owner is not None:
                self._check_owner(owner, required)
            if not any(set(required).issubset(spec.capabilities) for spec in AGENTS.values()):
                raise _error("NO_CANDIDATE", "No agent has the required capabilities")
            for artifact_id in inputs:
                artifact = conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
                if artifact is None or artifact["workflow_id"] != wf["id"] or not (
                        artifact["task_id"] == task["id"] and artifact["owner"] == task["owner"]
                        or task["owner"] in json.loads(artifact["grantees_json"])):
                    raise _error("ARTIFACT_FORBIDDEN", "Input artifact is not accessible to the delegating agent")
                if owner is not None and owner not in json.loads(artifact["grantees_json"]):
                    raise _error("ARTIFACT_FORBIDDEN", "Input artifact has not been granted to the target agent")
            self._insert_task(conn, new_task_id, wf["id"], goal, owner, required, acceptance, parent=task, inputs=inputs)
        else:
            target = args.get("target_agent")
            self._check_owner(target, ())
            if target == task["owner"]:
                raise _error("SELF_ASK_FORBIDDEN", "An agent cannot ask itself")
            question = _text(args.get("question"), "question")
            used = conn.execute("SELECT count(*) FROM messages WHERE workflow_id=? AND kind IN ('INFO_REQUEST','NOTIFICATION')", (wf["id"],)).fetchone()[0]
            if used >= bounds.max_a2a:
                raise _error("A2A_BUDGET_EXCEEDED", "Agent-message budget is exhausted")
            conversation = new_id("conversation")
            conn.execute("INSERT INTO conversations VALUES(?,?,'PRIVATE',?)", (conversation, wf["id"], dumps([task["owner"], target])))
            conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,'PRIVATE','INFO_REQUEST',?,NULL,?)",
                         (new_id("message"), wf["id"], conversation, task["owner"], target, question, time.time()))
            self._insert_task(conn, new_task_id, wf["id"], question, target, (), (), parent=task, kind="REPLY", conversation=conversation)
            conn.execute("UPDATE tasks SET status='READY' WHERE id=?", (new_task_id,))
            self.event(conn, wf["id"], new_task_id, "TaskRouted", {"owner": target, "without_bid": True})
        return new_task_id

    def _finish_attempt(self, conn, task, status="FINISHED"):
        if task["active_attempt_id"]:
            conn.execute("UPDATE attempts SET status=?,ended=? WHERE id=? AND status='RUNNING'", (status, time.time(), task["active_attempt_id"]))
        conn.execute("UPDATE tasks SET active_attempt_id=NULL WHERE id=?", (task["id"],))

    def suspend(self, task_id, attempt_id):
        with self.tx() as conn:
            task, wf = self._active(conn, task_id, attempt_id)
            self._finish_attempt(conn, task)
            open_wait = conn.execute("SELECT 1 FROM waits WHERE task_id=? AND status='OPEN' LIMIT 1", (task_id,)).fetchone()
            conn.execute("UPDATE tasks SET status='WAITING',updated=? WHERE id=?", (time.time(), task_id))
            if open_wait:
                self.event(conn, wf["id"], task_id, "TaskWaiting", {})
            else:
                self._wake(conn, self._task(conn, task_id))
            return self._task(conn, task_id)

    def _wake(self, conn, task):
        if task["status"] != "WAITING":
            return
        if conn.execute("SELECT 1 FROM waits WHERE task_id=? AND status='OPEN' LIMIT 1", (task["id"],)).fetchone():
            return
        wf = self._workflow(conn, task["workflow_id"])
        bounds = self._bounds(wf)
        if task["resumes"] >= bounds.max_resumes:
            self._terminal(conn, task, "FAILED", "RESUMES_EXHAUSTED")
            return
        conn.execute("UPDATE tasks SET status='READY',next_kind='RESUME',resumes=resumes+1,updated=? WHERE id=?", (time.time(), task["id"]))
        self.event(conn, wf["id"], task["id"], "TaskResumed", {})

    def _resolve(self, conn, wait, result):
        if wait["status"] == "RESOLVED":
            return
        conn.execute("UPDATE waits SET status='RESOLVED',result_json=? WHERE id=?", (dumps(result), wait["id"]))
        # Answers update the approval first. Any other trusted wait resolver closes
        # pending authority, so an expired wait can never retain a live approval.
        conn.execute("UPDATE approvals SET status='EXPIRED' WHERE wait_id=? AND status='OPEN'", (wait["id"],))
        task = self._task(conn, wait["task_id"])
        self.event(conn, task["workflow_id"], task["id"], "WaitResolved", {"wait_id": wait["id"], "kind": wait["kind"]})
        self._wake(conn, task)

    def resolve_wait(self, wait_id, result):
        with self.tx() as conn:
            wait = conn.execute("SELECT * FROM waits WHERE id=?", (wait_id,)).fetchone()
            if wait is None:
                raise _error("WAIT_NOT_FOUND", "Wait does not exist")
            self._resolve(conn, dict(wait), result)
            return dict(conn.execute("SELECT * FROM waits WHERE id=?", (wait_id,)).fetchone())

    def answer(self, approval_id, decision, answer=None, user_id="human:local"):
        normalized = str(decision).upper()
        normalized = {"APPROVE": "APPROVED", "DENY": "DENIED", "ANSWER": "ANSWERED"}.get(normalized, normalized)
        with self.tx() as conn:
            approval = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if approval is None:
                raise _error("APPROVAL_NOT_FOUND", "Approval does not exist")
            approval = dict(approval)
            wf = self._workflow(conn, approval["workflow_id"])
            if wf["user_id"] != user_id:
                raise _error("APPROVAL_FORBIDDEN", "Only the workflow owner may answer")
            if digest(json.loads(approval["args_json"])) != approval["args_hash"]:
                raise _error("APPROVAL_HASH_MISMATCH", "Approval arguments no longer match their hash")
            wait = dict(conn.execute("SELECT * FROM waits WHERE id=?", (approval["wait_id"],)).fetchone())
            if wait["kind"] == "HUMAN_CLARIFY":
                if normalized in ("APPROVED", "ANSWERED"):
                    normalized = "ANSWERED"
                else:
                    raise _error("INVALID_ARGUMENT", "Clarification requires an answer")
                if not isinstance(answer, str) or not answer.strip():
                    raise _error("INVALID_ARGUMENT", "Clarification answer must be nonempty text")
            elif normalized not in ("APPROVED", "DENIED"):
                raise _error("INVALID_ARGUMENT", "Approval decision must be APPROVED or DENIED")
            if approval["status"] != "OPEN":
                if approval["status"] == normalized and approval["answer_json"] == dumps(answer):
                    return approval
                if approval["status"] == "EXPIRED":
                    raise _error("APPROVAL_EXPIRED", "Approval has expired")
                raise _error("APPROVAL_ALREADY_ANSWERED", "Approval already has a different answer")
            if wait["status"] != "OPEN" or wf["status"] != "OPEN" or wait["deadline"] <= time.time():
                conn.execute("UPDATE approvals SET status='EXPIRED' WHERE id=?", (approval_id,))
                self._resolve(conn, wait, {"approval_id": approval_id, "decision": "EXPIRED", "error": {"code": "APPROVAL_EXPIRED", "message": "Approval expired"}})
            else:
                conn.execute("UPDATE approvals SET status=?,answer_json=? WHERE id=?", (normalized, dumps(answer), approval_id))
                self._resolve(conn, wait, {"approval_id": approval_id, "decision": normalized, "answer": answer,
                                           "tool_name": approval["tool_name"], "arguments": json.loads(approval["args_json"]), "args_hash": approval["args_hash"]})
                self.event(conn, wf["id"], approval["task_id"], "HumanAnswered", {"approval_id": approval_id, "decision": normalized})
            return dict(conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone())

    def verify(self, task_id, attempt_id, result):
        encoded = dumps(result)
        with self.tx() as conn:
            task, wf = self._active(conn, task_id, attempt_id)
            if conn.execute("SELECT 1 FROM waits WHERE task_id=? AND status='OPEN' LIMIT 1", (task_id,)).fetchone():
                raise _error("WAITS_ACTIVE", "Task has unresolved waits")
            self._finish_attempt(conn, task)
            conn.execute("UPDATE tasks SET status='VERIFYING',result_json=?,updated=? WHERE id=?", (encoded, time.time(), task_id))
            self.event(conn, wf["id"], task_id, "ResultSubmitted", {})
            return self._task(conn, task_id)

    def _descendants(self, conn, task_id):
        return [dict(r) for r in conn.execute("""WITH RECURSIVE children(id) AS (
          SELECT id FROM tasks WHERE parent_id=? UNION ALL
          SELECT t.id FROM tasks t JOIN children c ON t.parent_id=c.id)
          SELECT t.* FROM tasks t JOIN children c ON t.id=c.id ORDER BY t.depth DESC""", (task_id,))]

    def complete(self, task_id):
        with self.tx() as conn:
            task = self._task(conn, task_id)
            if task["status"] == "COMPLETED":
                return task
            if task["status"] != "VERIFYING":
                raise _error("ILLEGAL_TRANSITION", "Completion requires verification")
            if any(child["status"] not in TERMINAL for child in self._descendants(conn, task_id)):
                raise _error("CHILDREN_ACTIVE", "Task still has active descendants")
            wf = self._workflow(conn, task["workflow_id"])
            if wf["deadline"] <= time.time():
                self._terminal(conn, self._task(conn, wf["root_task_id"]), "TIMED_OUT", "DEADLINE")
            else:
                self._terminal(conn, task, "COMPLETED", None)
            return self._task(conn, task_id)

    def _terminal(self, conn, task, status, reason, *, notify=True):
        if task["status"] in TERMINAL:
            return
        if status not in TERMINAL:
            raise _error("INVALID_ARGUMENT", "Invalid terminal status")
        if status != "COMPLETED":
            for child in self._descendants(conn, task["id"]):
                if child["status"] not in TERMINAL:
                    self._terminal_single(conn, child, "CANCELLED", "PARENT_TERMINATED")
        self._terminal_single(conn, task, status, reason)
        if notify:
            for wait in conn.execute("SELECT * FROM waits WHERE ref_id=? AND status='OPEN' AND kind IN ('CHILD_TASK','AGENT_REPLY')", (task["id"],)).fetchall():
                wait = dict(wait)
                result = {"task_id": task["id"], "status": status, "result": json.loads(task["result_json"]) if task["result_json"] else None, "error": reason}
                if wait["kind"] == "AGENT_REPLY" and status == "COMPLETED":
                    submitted = result["result"]
                    answer = submitted.get("answer", submitted.get("summary", submitted.get("text", ""))) if isinstance(submitted, dict) else str(submitted)
                    message_id = new_id("message")
                    request = conn.execute("SELECT * FROM messages WHERE conversation_id=? AND kind='INFO_REQUEST' ORDER BY created LIMIT 1", (task["conversation_id"],)).fetchone()
                    if request:
                        conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,'PRIVATE','INFO_REPLY',?,?,?)", (message_id, task["workflow_id"], task["conversation_id"], task["owner"], request["sender"], str(answer), request["id"], time.time()))
                    result.update({"message_id": message_id, "answer": answer})
                self._resolve(conn, wait, result)

    def _terminal_single(self, conn, task, status, reason):
        self._finish_attempt(conn, task, "ABANDONED" if task["status"] == "RUNNING" else "FINISHED")
        now = time.time()
        conn.execute("UPDATE tasks SET status=?,error=?,next_kind=NULL,updated=? WHERE id=?", (status, reason, now, task["id"]))
        for wait in conn.execute("SELECT * FROM waits WHERE task_id=? AND status='OPEN'", (task["id"],)).fetchall():
            conn.execute("UPDATE waits SET status='RESOLVED',result_json=? WHERE id=?", (dumps({"error": {"code": status, "message": reason or status}}), wait["id"]))
            conn.execute("UPDATE approvals SET status='EXPIRED' WHERE wait_id=? AND status='OPEN'", (wait["id"],))
        conn.execute("UPDATE operations SET status='UNKNOWN',error=COALESCE(error,'TASK_TERMINATED') WHERE task_id=? AND status='RUNNING'", (task["id"],))
        self.event(conn, task["workflow_id"], task["id"], "TaskTerminated", {"status": status, "reason": reason})
        wf = self._workflow(conn, task["workflow_id"])
        if task["kind"] == "WORK" and task["owner"] in AGENTS:
            result = json.loads(task["result_json"]) if task["result_json"] else {}
            summary = result.get("summary", "") if isinstance(result, dict) else ""
            experience = {
                "type": "objective_experience", "task_id": task["id"],
                "goal": task["goal"][:2000], "outcome": status, "status": status,
                "acceptance": json.loads(task["acceptance_json"]),
                "summary": str(summary)[:4000], "error": reason,
            }
            conn.execute("INSERT OR IGNORE INTO memories(id,agent_id,user_id,workflow_id,text,created) VALUES(?,?,?,?,?,?)",
                         ("experience:" + task["id"], task["owner"], wf["user_id"], wf["id"], dumps(experience), now))
            self.event(conn, wf["id"], task["id"], "ObjectiveExperienceSaved", {"owner": task["owner"], "outcome": status})
        if wf["root_task_id"] == task["id"]:
            conn.execute("UPDATE workflows SET status=? WHERE id=?", (status, wf["id"]))
            self.event(conn, wf["id"], task["id"], "WorkflowTerminated", {"status": status})

    def fail(self, task_id, reason, status="FAILED"):
        if status not in ("FAILED", "CANCELLED", "TIMED_OUT"):
            raise _error("INVALID_ARGUMENT", "fail requires a failure terminal status")
        with self.tx() as conn:
            task = self._task(conn, task_id)
            self._terminal(conn, task, status, str(reason))
            return self._task(conn, task_id)

    def repair(self, task_id, feedback, policy):
        with self.tx() as conn:
            task = self._task(conn, task_id)
            if task["status"] != "VERIFYING":
                raise _error("ILLEGAL_TRANSITION", "Repair requires verification")
            wf = self._workflow(conn, task["workflow_id"])
            if task["repairs"] >= self._bounds(wf, policy).max_repairs:
                self._terminal(conn, task, "FAILED", "REPAIRS_EXHAUSTED")
            else:
                conn.execute("UPDATE tasks SET status='READY',next_kind='REPAIR',repairs=repairs+1,result_json=NULL,error=?,updated=? WHERE id=?", (str(feedback), time.time(), task_id))
                snapshot = conn.execute("SELECT messages_json FROM snapshots WHERE task_id=?", (task_id,)).fetchone()
                messages = json.loads(snapshot[0]) if snapshot else []
                messages.append({"role": "user", "content": "Verification feedback: " + str(feedback)})
                conn.execute("INSERT INTO snapshots VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET messages_json=excluded.messages_json", (task_id, dumps(messages)))
                self.event(conn, wf["id"], task_id, "RepairRequested", {"feedback": str(feedback)})
            return self._task(conn, task_id)

    def retry(self, task_id, attempt_id, error, policy):
        with self.tx() as conn:
            task, wf = self._active(conn, task_id, attempt_id)
            self._finish_attempt(conn, task, "ABANDONED")
            if task["retries"] >= self._bounds(wf, policy).max_retries:
                self._terminal(conn, task, "FAILED", "RETRIES_EXHAUSTED: " + str(error))
            else:
                conn.execute("UPDATE tasks SET status='READY',next_kind='RETRY',retries=retries+1,error=?,updated=? WHERE id=?", (str(error), time.time(), task_id))
                conn.execute("UPDATE operations SET status='UNKNOWN',error=COALESCE(error,'ATTEMPT_ABANDONED') WHERE attempt_id=? AND status='RUNNING'", (attempt_id,))
                self.event(conn, wf["id"], task_id, "TaskRetried", {"error": str(error)})
            return self._task(conn, task_id)

    def recover(self, policy, workflow_ids=None):
        if workflow_ids is not None and not isinstance(workflow_ids, str):
            workflow_ids = tuple(workflow_ids)
        scope, params = workflow_scope(workflow_ids)
        wf_scope, _ = workflow_scope(workflow_ids, "id")
        wait_scope = f" AND task_id IN (SELECT id FROM tasks WHERE 1=1{scope})" if workflow_ids is not None else ""
        with self.tx() as conn:
            now = time.time()
            conn.execute("UPDATE operations SET status='UNKNOWN',error=COALESCE(error,'PROCESS_RECOVERY') WHERE status IN ('RUNNING','PENDING')" + scope, params)
            conn.execute("UPDATE llm_calls SET status='UNKNOWN',error=COALESCE(error,'PROCESS_RECOVERY') WHERE status IN ('STARTED','RUNNING')" + scope, params)
            for wf in conn.execute("SELECT * FROM workflows WHERE status='OPEN' AND deadline<=?" + wf_scope, (now, *params)).fetchall():
                self._terminal(conn, self._task(conn, wf["root_task_id"]), "TIMED_OUT", "DEADLINE")
            running = conn.execute("SELECT * FROM tasks WHERE status='RUNNING'" + scope, params).fetchall()
            for row in running:
                task = dict(row)
                wf = self._workflow(conn, task["workflow_id"])
                self._finish_attempt(conn, task, "ABANDONED")
                if task["retries"] >= self._bounds(wf, policy).max_retries:
                    self._terminal(conn, task, "FAILED", "RETRIES_EXHAUSTED")
                else:
                    conn.execute("UPDATE tasks SET status='READY',next_kind='RETRY',retries=retries+1,error='PROCESS_RECOVERY',updated=? WHERE id=?", (now, task["id"]))
                    self.event(conn, wf["id"], task["id"], "TaskRecovered", {})
            for row in conn.execute("SELECT * FROM waits WHERE status='OPEN' AND deadline<=?" + wait_scope, (now, *params)).fetchall():
                wait = dict(row)
                conn.execute("UPDATE approvals SET status='EXPIRED' WHERE wait_id=? AND status='OPEN'", (wait["id"],))
                self._resolve(conn, wait, {"error": {"code": "WAIT_TIMEOUT", "message": "Wait deadline elapsed"}, "decision": "EXPIRED"})
            # Resolve-before-suspend/restart remains runnable even if an in-memory wake-up was lost.
            for row in conn.execute("SELECT * FROM tasks WHERE status='WAITING'" + scope, params).fetchall():
                self._wake(conn, dict(row))
            return {"recovered": len(running)}

    def begin_operation(self, task_id, attempt_id, call_id, tool, args, effect_key=None, *, web_cache_key=None):
        _text(call_id, "call_id", 256)
        _text(tool, "tool", 128)
        if not isinstance(args, dict):
            raise _error("INVALID_ARGUMENT", "Tool arguments must be an object")
        if effect_key is not None:
            _text(effect_key, "effect_key", 1024)
        if web_cache_key is not None:
            _text(web_cache_key, "web_cache_key", 128)
            if tool not in {"search_web", "fetch_web"} or effect_key is not None:
                raise _error("INVALID_ARGUMENT", "Only read-only web requests may reuse saved public evidence")
        args_hash = digest(args)
        call_key = task_id + ":" + call_id
        with self.tx() as conn:
            task, wf = self._active(conn, task_id, attempt_id)
            binding = conn.execute("SELECT * FROM operation_calls WHERE call_key=?", (call_key,)).fetchone()
            if binding is not None:
                if (binding["task_id"] != task_id or binding["tool_name"] != tool
                        or binding["args_hash"] != args_hash or binding["effect_key"] != effect_key):
                    raise _error("IDEMPOTENCY_CONFLICT", "Tool call ID was reused with different arguments")
                old = conn.execute("SELECT * FROM operations WHERE id=?", (binding["operation_id"],)).fetchone()
                return dict(old), False
            old = conn.execute("SELECT * FROM operations WHERE call_key=?", (call_key,)).fetchone()
            if old is None and effect_key is not None:
                old = conn.execute("SELECT * FROM operations WHERE effect_key=?", (effect_key,)).fetchone()
            if old is not None:
                if old["task_id"] != task_id or old["tool_name"] != tool or old["args_hash"] != args_hash or (old["call_key"] == call_key and old["effect_key"] != effect_key):
                    raise _error("IDEMPOTENCY_CONFLICT", "Operation key was reused with different arguments or owner")
                conn.execute("INSERT INTO operation_calls VALUES(?,?,?,?,?,?)", (call_key, old["id"], task_id, tool, args_hash, effect_key))
                return dict(old), False
            if web_cache_key is not None:
                # Shared web evidence is public within this workflow. Cache only
                # successful results from the same explicit provider/request key;
                # failures and unknown/in-flight outcomes never satisfy a new call.
                candidates = conn.execute("""SELECT o.* FROM operations o JOIN events e
                    ON json_extract(e.payload_json,'$.operation_id')=o.id
                    WHERE o.workflow_id=? AND o.tool_name=? AND o.status='SUCCEEDED'
                    AND e.type='OperationReserved' AND e.workflow_id=o.workflow_id
                    AND json_extract(e.payload_json,'$.web_cache_key')=?
                    ORDER BY o.created DESC,o.id DESC""", (wf["id"], tool, web_cache_key)).fetchall()
                cached = None
                for candidate in candidates:
                    if tool == "fetch_web":
                        try:
                            content = json.loads(candidate["result_json"])["page"]["content"]
                        except (TypeError, ValueError, KeyError):
                            continue
                        if not isinstance(content, str) or not content.strip():
                            continue
                    cached = candidate
                    break
                if cached is not None:
                    conn.execute("INSERT INTO operation_calls VALUES(?,?,?,?,?,?)", (call_key, cached["id"], task_id, tool, args_hash, None))
                    self.event(conn, wf["id"], task_id, "OperationReused", {"operation_id": cached["id"], "tool_name": tool,
                                                                         "tool_call_id": call_id, "source_task_id": cached["task_id"]})
                    return dict(cached), False
            if tool in {"search_web", "fetch_web"}:
                used = conn.execute("SELECT COUNT(*) FROM operations WHERE workflow_id=? AND tool_name IN ('search_web','fetch_web')", (wf["id"],)).fetchone()[0]
                if used >= self._bounds(wf).max_web_requests:
                    raise _error("WEB_BUDGET_EXHAUSTED", "Workflow web request budget is exhausted")
            operation_id = new_id("operation")
            conn.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,'RUNNING',NULL,NULL,?)",
                         (operation_id, wf["id"], task_id, attempt_id, call_key, effect_key, tool, args_hash, time.time()))
            conn.execute("INSERT INTO operation_calls VALUES(?,?,?,?,?,?)", (call_key, operation_id, task_id, tool, args_hash, effect_key))
            payload = {"operation_id": operation_id, "tool_name": tool}
            if web_cache_key is not None:
                payload["web_cache_key"] = web_cache_key
            self.event(conn, wf["id"], task_id, "OperationReserved", payload)
            return dict(conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()), True

    def finish_operation(self, operation_id, result, error=None, status="SUCCEEDED"):
        if status not in ("SUCCEEDED", "FAILED", "UNKNOWN"):
            raise _error("INVALID_ARGUMENT", "Invalid operation outcome")
        if error is not None and status == "SUCCEEDED":
            status = "FAILED"
        encoded = dumps(result)
        with self.tx() as conn:
            op = conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if op is None:
                raise _error("OPERATION_NOT_FOUND", "Operation does not exist")
            if op["status"] in ("SUCCEEDED", "FAILED"):
                if op["status"] == status and op["result_json"] == encoded and op["error"] == (str(error) if error else None):
                    return dict(op)
                raise _error("OPERATION_ALREADY_FINISHED", "Operation already has a different outcome")
            # UNKNOWN can be reconciled by a trusted caller; it is never re-executed automatically.
            conn.execute("UPDATE operations SET status=?,result_json=?,error=? WHERE id=?", (status, encoded, str(error) if error else None, operation_id))
            self.event(conn, op["workflow_id"], op["task_id"], "OperationFinished", {"operation_id": operation_id, "status": status})
            return dict(conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())
