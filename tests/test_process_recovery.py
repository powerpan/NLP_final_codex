"""Real process-boundary checks; these still use no remote model or Docker."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from maplatform.domain import Policy
from maplatform.provider import ScriptedProvider
from maplatform.runtime import Runtime
from maplatform.store import Store
from maplatform.tools import MemorySandbox, ToolGateway


class ProcessTests(unittest.TestCase):
    def test_killed_writer_recovers_snapshot_and_does_not_repeat_unknown_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "recovery.sqlite"
            marker = Path(tmp) / "ready.json"
            code = '''
import json,sys,threading
from pathlib import Path
from maplatform.store import Store
from maplatform.domain import Policy,dumps
from maplatform.demo import tool
s=Store(sys.argv[1]); w=s.create_workflow('crash fixture',policy=Policy())
s.route(w['root_task_id'],'planner'); t=s.claim(w['root_task_id'])
c=tool('durable-call','remember',{'text':'uncertain fixture effect'})
s.save_snapshot(t['id'],t['active_attempt_id'],[{'role':'user','content':'fixture'},{'role':'assistant','content':'','reasoning_content':'','tool_calls':[c]}])
s.begin_operation(t['id'],t['active_attempt_id'],c['id'],'remember',{'text':'uncertain fixture effect'})
Path(sys.argv[2]).write_text(json.dumps({'task_id':t['id'],'attempt_id':t['active_attempt_id']}))
threading.Event().wait()
'''
            process = subprocess.Popen([sys.executable, "-c", code, str(db), str(marker)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), "Writer did not reach durable checkpoint")
                checkpoint = json.loads(marker.read_text())
                process.kill()
                process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
            store = Store(db)
            try:
                gateway = ToolGateway(store, MemorySandbox(), Policy())
                provider = ScriptedProvider([{"role": "assistant", "content": '{"summary":"Recovered with unknown effect recorded","artifact_ids":[]}'}])
                asyncio.run(Runtime(store, provider, gateway).run_until_idle(max_seconds=3))
                task = store.one("SELECT * FROM tasks WHERE id=?", (checkpoint["task_id"],))
                self.assertEqual(task["status"], "COMPLETED")
                self.assertEqual(task["retries"], 1)
                old = store.one("SELECT * FROM attempts WHERE id=?", (checkpoint["attempt_id"],))
                self.assertEqual(old["status"], "ABANDONED")
                self.assertEqual(store.one("SELECT * FROM operations")["status"], "UNKNOWN")
                self.assertFalse(store.one("SELECT * FROM memories WHERE text='uncertain fixture effect'"))
            finally:
                store.close()

    def test_cli_demo_export_and_trace_are_usable_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "demo.sqlite")
            prefix = [sys.executable, "-m", "maplatform", "--db", db]
            result = subprocess.run(prefix + ["demo", "--auto-answer"], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = json.loads(result.stdout)
            self.assertEqual(result["workflows"][0]["status"], "COMPLETED")
            root = next(t for t in result["tasks"] if t["parent_id"] is None)
            aid = json.loads(root["result_json"])["artifact_ids"][0]
            output = Path(tmp) / "add.py"
            exported = subprocess.run(prefix + ["export", aid, "--output", str(output)], text=True, capture_output=True, timeout=5)
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertEqual(output.read_text(), "def add(a, b):\n    return a + b\n")
            repeated = subprocess.run(prefix + ["export", aid, "--output", str(output)], text=True, capture_output=True, timeout=5)
            self.assertEqual(repeated.returncode, 2)
            trace = Path(tmp) / "events.jsonl"
            exported = subprocess.run(prefix + ["events", "--output", str(trace)], text=True, capture_output=True, timeout=5)
            self.assertEqual(exported.returncode, 0, exported.stderr)
            rows = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertTrue(any(r["type"] == "RunConfiguration" for r in rows))


if __name__ == "__main__":
    unittest.main()
