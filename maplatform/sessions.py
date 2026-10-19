"""Durable human sessions over independently budgeted WORK workflows.

Only public user input, accepted root replies and server execution counts become context.
Snapshots, peer messages and model reasoning remain in their original tasks.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from .domain import AGENTS, PlatformError, Policy, dumps, new_id
from .store import _text, workflow_scope


ARTIFACT_BOUNDARY = (
    "Each turn uses a new workflow and private task workspaces. Only artifacts explicitly "
    "returned in a completed public answer are copied into this turn. Historical artifact "
    "IDs are references only; use the current input_artifact_ids and artifact_continuity "
    "mapping, import them into the current workspace, and pass the new IDs to delegates. "
    "Unpublished files and private peer messages are not carried over."
)

EXECUTION_BOUNDARY = (
    "These counts come from server execution records, not agent claims. COMPLETED means "
    "the workflow ended and its declared acceptance checks passed; it does not establish "
    "that the user's substantive goal was met. Successful web requests do not establish "
    "source relevance, freshness, or factual correctness. Inspect the public answer and "
    "its evidence before claiming success or diagnosing a missing capability."
)


class SessionService:
    def __init__(self, store):
        self.store = store

    def _get(self, conn, session_id, user_id):
        session = conn.execute("SELECT * FROM sessions WHERE id=? AND user_id=?", (session_id, user_id)).fetchone()
        if session is None:
            raise PlatformError("SESSION_NOT_FOUND", "Session is not accessible to this user")
        return dict(session)

    def create(self, title="New session", owner="planner", user_id="human:local"):
        _text(title, "title", 256)
        _text(user_id, "user_id", 256)
        self.store._check_owner(owner, ())
        now, session_id = time.time(), new_id("session")
        with self.store.tx() as conn:
            conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", (session_id, user_id, title, owner, now, now))
        return self.get(session_id, user_id=user_id)

    def get(self, session_id, user_id="human:local"):
        with self.store.tx() as conn:
            session = self._get(conn, session_id, user_id)
            session["turn_count"] = conn.execute("SELECT COUNT(*) FROM session_turns WHERE session_id=?", (session_id,)).fetchone()[0]
            return session

    def list(self, user_id="human:local"):
        return self.store.all("""SELECT s.*,COUNT(t.id) AS turn_count FROM sessions s
            LEFT JOIN session_turns t ON t.session_id=s.id WHERE s.user_id=?
            GROUP BY s.id ORDER BY s.updated DESC,s.id""", (user_id,))

    def _materialize(self, conn, workflow_ids=None):
        scope, params = workflow_scope(workflow_ids, "w.id")
        rows = conn.execute("""SELECT t.id,w.status,r.result_json,r.error FROM session_turns t
            JOIN workflows w ON w.id=t.workflow_id JOIN tasks r ON r.id=w.root_task_id
            WHERE t.status='OPEN' AND w.status<>'OPEN'""" + scope, params).fetchall()
        for row in rows:
            result = json.loads(row["result_json"] or "{}") if row["status"] == "COMPLETED" else {}
            conn.execute("""UPDATE session_turns SET status=?,assistant_text=?,artifact_ids_json=?,error=?,updated=? WHERE id=? AND status='OPEN'""",
                         (row["status"], result.get("summary"), dumps(result.get("artifact_ids", [])), row["error"], time.time(), row["id"]))
        return len(rows)

    def materialize(self, workflow_ids=None):
        """Idempotent projection, including recovery after completion-before-projection."""
        with self.store.tx() as conn:
            return self._materialize(conn, workflow_ids)

    def _execution_summary(self, conn, workflow_id):
        """Expose lifecycle evidence, never descendant goals, results or snapshots."""
        workflow = conn.execute("SELECT status FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        tasks = {"total": 0, "completed": 0, "failed": 0, "cancelled": 0,
                 "timed_out": 0, "active": 0, "failed_children": 0, "cancelled_children": 0}
        errors = set()
        def error_code(value):
            # Store.error may also contain operator prose. Forward only code-shaped
            # diagnostics, never arbitrary private error text.
            if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value):
                errors.add(value)
        for row in conn.execute("SELECT parent_id,status,error FROM tasks WHERE workflow_id=?", (workflow_id,)):
            tasks["total"] += 1
            state = row["status"].lower()
            tasks[state if state in {"completed", "failed", "cancelled", "timed_out"} else "active"] += 1
            if row["parent_id"] is not None:
                tasks["failed_children"] += row["status"] in {"FAILED", "TIMED_OUT"}
                tasks["cancelled_children"] += row["status"] == "CANCELLED"
            error_code(row["error"])
        web = {"total": 0, "succeeded": 0, "failed": 0, "unknown": 0, "running": 0}
        for row in conn.execute("SELECT tool_name,status,error FROM operations WHERE workflow_id=?", (workflow_id,)):
            error_code(row["error"])
            if row["tool_name"] in {"search_web", "fetch_web"}:
                web["total"] += 1
                web[row["status"].lower()] += 1
        for row in conn.execute("SELECT error FROM llm_calls WHERE workflow_id=? AND error IS NOT NULL", (workflow_id,)):
            error_code(row["error"])
        return {"source": "server_execution_records", "lifecycle_status": workflow["status"],
                "tasks": tasks, "web": web, "error_codes": sorted(errors),
                "verification_boundary": EXECUTION_BOUNDARY}

    def execution_summary(self, session_id, workflow_id, user_id="human:local"):
        """Read aggregate execution evidence only after session ownership checks."""
        with self.store.tx() as conn:
            self._get(conn, session_id, user_id)
            if conn.execute("SELECT 1 FROM session_turns WHERE session_id=? AND workflow_id=?",
                            (session_id, workflow_id)).fetchone() is None:
                raise PlatformError("SESSION_NOT_FOUND", "Workflow is not part of this session")
            return self._execution_summary(conn, workflow_id)

    def _history(self, conn, session_id, before=None):
        sql = "SELECT * FROM session_turns WHERE session_id=?"
        params = (session_id,)
        if before is not None:
            sql += " AND ordinal<?"
            params += (before,)
        entries = []
        for turn in conn.execute(sql + " ORDER BY ordinal", params):
            base = {"owner": turn["owner"], "workflow_id": turn["workflow_id"], "status": turn["status"], "created": turn["created"]}
            user_entry = {**base, "id": turn["id"] + ":user", "role": "user", "content": turn["user_text"], "artifact_ids": []}
            # Failed/cancelled turns have no public assistant answer, but their
            # server diagnostics must not disappear from next-turn context.
            execution = self._execution_summary(conn, turn["workflow_id"]) if turn["status"] != "OPEN" else None
            if execution is not None and turn["status"] != "COMPLETED":
                user_entry["execution_summary"] = execution
            entries.append(user_entry)
            # Human clarification answers are public inputs too. Retain their
            # question so a later turn does not receive an unexplained "yes".
            for answer in conn.execute("SELECT * FROM approvals WHERE workflow_id=? AND tool_name='ask_human' AND status='ANSWERED' ORDER BY created,id", (turn["workflow_id"],)):
                question = json.loads(answer["args_json"])["question"]
                text = json.loads(answer["answer_json"])
                entries.append({**base, "id": answer["id"], "role": "user", "kind": "clarification", "content": f"Question: {question}\nMy answer: {text}", "artifact_ids": [], "created": answer["created"]})
            if turn["status"] == "COMPLETED":
                entries.append({**base, "id": turn["id"] + ":assistant", "role": "assistant", "content": turn["assistant_text"],
                                "artifact_ids": json.loads(turn["artifact_ids_json"]), "created": turn["updated"],
                                "execution_summary": execution})
        return entries

    def history(self, session_id, user_id="human:local"):
        with self.store.tx() as conn:
            self._get(conn, session_id, user_id)
            ids = [r[0] for r in conn.execute("SELECT workflow_id FROM session_turns WHERE session_id=?", (session_id,))]
            self._materialize(conn, ids)
            return self._history(conn, session_id)

    def active_workflows(self, session_id, user_id="human:local"):
        with self.store.tx() as conn:
            self._get(conn, session_id, user_id)
            return [dict(row) for row in conn.execute("""SELECT w.* FROM workflows w JOIN session_turns t ON t.workflow_id=w.id
                WHERE t.session_id=? AND w.status='OPEN' ORDER BY t.ordinal""", (session_id,))]

    def _copy_public_artifacts(self, conn, session_id, wf, owner):
        mapping, copied = [], {}
        turns = conn.execute("SELECT * FROM session_turns WHERE session_id=? AND status='COMPLETED' ORDER BY ordinal", (session_id,)).fetchall()
        for turn in turns:
            previous = self.store._workflow(conn, turn["workflow_id"])
            for old_id in json.loads(turn["artifact_ids_json"]):
                old = conn.execute("SELECT * FROM artifacts WHERE id=? AND workflow_id=?", (old_id, turn["workflow_id"])).fetchone()
                if old is None or not (old["task_id"] == previous["root_task_id"] and old["owner"] == turn["owner"] or turn["owner"] in json.loads(old["grantees_json"])):
                    raise PlatformError("ARTIFACT_FORBIDDEN", "A prior public artifact is no longer authorized")
                content = bytes(old["content"])
                if hashlib.sha256(content).hexdigest() != old["sha256"]:
                    raise PlatformError("ARTIFACT_CORRUPT", "A prior public artifact failed its checksum")
                grants = sorted(({old["owner"], owner} | set(json.loads(old["grantees_json"]))) & set(AGENTS))
                # Repeated publication of unchanged public bytes shares one copy;
                # differing versions remain separate and have explicit mappings.
                key = (old["name"], old["sha256"], old["media_type"], tuple(grants))
                artifact_id = copied.get(key)
                if artifact_id is None:
                    artifact_id = new_id("artifact")
                    copied[key] = artifact_id
                    conn.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 (artifact_id, wf["id"], wf["root_task_id"], owner, old["name"], old["sha256"], old["media_type"], content, dumps(grants), time.time()))
                item = {"source_artifact_id": old_id, "source_workflow_id": old["workflow_id"], "artifact_id": artifact_id,
                        "name": old["name"], "sha256": old["sha256"], "grantees": grants}
                mapping.append(item)
                self.store.event(conn, wf["id"], wf["root_task_id"], "SessionArtifactCopied", {"session_id": session_id, **item})
        conn.execute("UPDATE tasks SET input_artifacts_json=? WHERE id=?", (dumps(list(copied.values())), wf["root_task_id"]))
        return mapping

    def submit(self, session_id, text, *, owner=None, policy=None, user_id="human:local", execution_mode=None):
        _text(text, "text")
        if execution_mode not in (None, "fixture", "live"):
            raise PlatformError("INVALID_ARGUMENT", "execution_mode must be fixture or live")
        chosen = policy or Policy()
        with self.store.tx() as conn:
            session = self._get(conn, session_id, user_id)
            owner = owner or session["owner"]
            self.store._check_owner(owner, ())
            active = conn.execute("SELECT 1 FROM session_turns t JOIN workflows w ON w.id=t.workflow_id WHERE t.session_id=? AND w.status='OPEN'", (session_id,)).fetchone()
            if active:
                raise PlatformError("SESSION_BUSY", "Finish, answer, or cancel the active turn before submitting another")
            ids = [r[0] for r in conn.execute("SELECT workflow_id FROM session_turns WHERE session_id=?", (session_id,))]
            self._materialize(conn, ids)
            history = self._history(conn, session_id)
            if len(dumps({"previous_turns": history, "goal": text})) > chosen.max_context_chars:
                raise PlatformError("CONTEXT_LIMIT", "Session history exceeds the context limit. Start a new session; the complete original history remains saved")
            wf = self.store._create_workflow(conn, text, user_id=user_id, owner=owner, acceptance=[{"kind": "nonempty"}], policy=chosen)
            self._copy_public_artifacts(conn, session_id, wf, owner)
            ordinal = conn.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM session_turns WHERE session_id=?", (session_id,)).fetchone()[0]
            turn_id, now = new_id("turn"), time.time()
            conn.execute("""INSERT INTO session_turns(id,session_id,ordinal,workflow_id,owner,user_text,status,created,updated)
                VALUES(?,?,?,?,?,?,'OPEN',?,?)""", (turn_id, session_id, ordinal, wf["id"], owner, text, now, now))
            conn.execute("UPDATE sessions SET owner=?,updated=? WHERE id=?", (owner, now, session_id))
            self.store.event(conn, wf["id"], wf["root_task_id"], "SessionTurnSubmitted", {"session_id": session_id, "turn_id": turn_id, "ordinal": ordinal})
            if execution_mode is not None:
                self.store.event(conn, wf["id"], wf["root_task_id"], "ExecutionMode", {"mode": execution_mode})
            return {**wf, "session_id": session_id, "turn_id": turn_id}

    def context_for(self, task):
        """Server-only context for this session's root owner, never child tasks."""
        with self.store.tx() as conn:
            row = conn.execute("""SELECT t.*,w.root_task_id,w.user_id,s.user_id AS session_user FROM session_turns t
                JOIN workflows w ON w.id=t.workflow_id JOIN sessions s ON s.id=t.session_id WHERE t.workflow_id=?""", (task["workflow_id"],)).fetchone()
            if row is None or row["root_task_id"] != task["id"]:
                return None
            if row["owner"] != task["owner"] or row["user_id"] != row["session_user"]:
                raise PlatformError("ACCESS_DENIED", "Session context is not accessible to this task")
            continuity = [json.loads(event[0]) for event in conn.execute("SELECT payload_json FROM events WHERE workflow_id=? AND type='SessionArtifactCopied' ORDER BY id", (task["workflow_id"],))]
            return {"session_id": row["session_id"], "previous_turns": self._history(conn, row["session_id"], before=row["ordinal"]),
                    "artifact_boundary": ARTIFACT_BOUNDARY, "artifact_continuity": continuity}
