"""Human-facing conversations. ACLs are checked before reading or injecting text."""
import time
from .domain import AGENTS, PlatformError, dumps, new_id


class CommunicationService:
    def __init__(self, store):
        self.store = store

    def create(self, goal, *, recipients, user_id="human:local", group=False, policy=None):
        recipients = list(dict.fromkeys(recipients))
        if not recipients or any(r not in AGENTS for r in recipients):
            raise PlatformError("UNKNOWN_AGENT", "Specify at least one registered agent")
        if not group and len(recipients) != 1:
            raise PlatformError("INVALID_RECIPIENT", "Private chats have one agent recipient")
        # Each mentioned agent owns a bounded root REPLY workflow. No broadcast-trigger loops.
        cid = new_id("group" if group else "private")
        result = []
        for recipient in recipients:
            wf = self.store.create_workflow(goal, user_id=user_id, owner=recipient,
                                            acceptance=[{"kind": "nonempty"}], policy=policy)
            with self.store.tx() as conn:
                conn.execute("UPDATE tasks SET kind='REPLY' WHERE id=?", (wf["root_task_id"],))
                # A group has one shared timeline; each mention still owns its task budget.
                conversation = cid
                conn.execute("INSERT OR IGNORE INTO conversations(id,workflow_id,visibility,members_json) VALUES(?,?,?,?)",
                             (conversation, wf["id"], "GROUP" if group else "PRIVATE", dumps([user_id] + recipients)))
                conn.execute("INSERT INTO messages(id,workflow_id,conversation_id,sender,recipient,visibility,kind,content,reply_to,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (new_id("message"), wf["id"], conversation, user_id, recipient, "GROUP" if group else "PRIVATE", "HUMAN", goal, None, time.time()))
                self.store.event(conn, wf["id"], wf["root_task_id"], "HumanMessageSubmitted", {"conversation": conversation, "recipient": recipient})
            result.append(wf)
        return {"group_id": cid if group else None, "workflows": result}

    def history(self, conversation_id, *, principal="human:local"):
        conversation = self.store.one("SELECT * FROM conversations WHERE id=?", (conversation_id,))
        import json
        if not conversation or principal not in json.loads(conversation["members_json"]):
            raise PlatformError("ACCESS_DENIED", "Conversation is not accessible")
        return self.store.all("SELECT id,sender,recipient,kind,content,created FROM messages WHERE conversation_id=? ORDER BY created,id", (conversation_id,))

    def materialize_root_reply(self, task):
        task = self.store.one("SELECT * FROM tasks WHERE id=?", (task["id"],))
        if not task or task["status"] != "COMPLETED":
            return
        if task["kind"] != "REPLY" or task["parent_id"] is not None:
            return
        import json
        conversation = self.store.one("SELECT c.* FROM conversations c JOIN messages m ON m.conversation_id=c.id WHERE m.workflow_id=? ORDER BY m.created LIMIT 1", (task["workflow_id"],))
        if not conversation:
            return
        user = self.store.one("SELECT user_id FROM workflows WHERE id=?", (task["workflow_id"],))["user_id"]
        result = json.loads(task["result_json"])
        with self.store.tx() as conn:
            conn.execute("INSERT OR IGNORE INTO messages(id,workflow_id,conversation_id,sender,recipient,visibility,kind,content,reply_to,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         ("reply:" + task["id"], task["workflow_id"], conversation["id"], task["owner"], user, conversation["visibility"], "REPLY", result["summary"], None, time.time()))
