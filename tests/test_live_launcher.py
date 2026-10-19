"""Exercise launcher ordering and failure exits using a local fake executable."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(shutil.which("zsh"), "launcher requires zsh")
class LiveLauncherTests(unittest.TestCase):
    def run_launcher(self, fail_stage=""):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "scripts").mkdir()
        launcher = root / "scripts" / "live_smoke.zsh"
        shutil.copyfile(Path(__file__).parents[1] / "scripts" / "live_smoke.zsh", launcher)
        fake = root / "fake-python"
        fake.write_text(f"#!{sys.executable}\n" + '''import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
stage = next(item for item in ("doctor", "probe", "evaluate") if item in args)
with Path("invocations.jsonl").open("a") as handle:
    handle.write(json.dumps(args) + "\\n")
failed = stage == os.environ.get("FAIL_STAGE")
report = {"stage": stage, "status": "FAIL" if failed else "PASS"}
if "--report" in args:
    with Path(args[args.index("--report") + 1]).open("x") as handle:
        json.dump(report, handle)
print(json.dumps(report))
raise SystemExit(17 if failed else 0)
''')
        fake.chmod(0o700)
        # Synthetic key only; this executable cannot import the real provider.
        result = subprocess.run([shutil.which("zsh"), str(launcher)], env={
            "PATH": os.defpath, "MULTI_AGENT_PYTHON": str(fake), "DEEPSEEK_API_KEY": "offline-fixture",
            "DEEPSEEK_MODEL": "offline-fixture", "FAIL_STAGE": fail_stage,
        }, capture_output=True, text=True, timeout=10)
        invocations = [json.loads(line) for line in (root / "invocations.jsonl").read_text().splitlines()]
        runs = list((root / "data" / "live-tests").iterdir())
        self.assertEqual(len(runs), 1)
        return result, invocations, runs[0]

    def test_success_saves_all_reports_and_keeps_call_budget(self):
        result, invocations, run = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(invocations), 3)
        for stage, name in (("doctor", "doctor-report.json"), ("probe", "probe-report.json"), ("evaluate", "text-report.json")):
            self.assertEqual(json.loads((run / name).read_text()), {"stage": stage, "status": "PASS"})
            self.assertIn(f'"stage": "{stage}"', result.stdout)
        args = invocations[-1]
        self.assertEqual(args[args.index("--max-calls") + 1], "6")
        self.assertEqual(args[args.index("--max-tasks") + 1], "2")
        self.assertEqual(args[args.index("--deadline") + 1], "180")

    def test_failed_doctor_saves_report_and_stops_before_probe(self):
        result, invocations, run = self.run_launcher("doctor")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(len(invocations), 1)
        self.assertEqual(json.loads((run / "doctor-report.json").read_text())["status"], "FAIL")
        self.assertFalse((run / "probe-report.json").exists())
        self.assertFalse((run / "text-report.json").exists())

    def test_failed_probe_preserves_evidence_and_stops_before_evaluation(self):
        result, invocations, run = self.run_launcher("probe")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(len(invocations), 2)
        self.assertEqual(json.loads((run / "doctor-report.json").read_text())["status"], "PASS")
        self.assertEqual(json.loads((run / "probe-report.json").read_text())["status"], "FAIL")
        self.assertIn('"status": "FAIL"', result.stdout)
        self.assertFalse((run / "text-report.json").exists())


if __name__ == "__main__":
    unittest.main()
