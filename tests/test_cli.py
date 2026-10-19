import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from maplatform.__main__ import execute, integrations, main, parser, probe
from maplatform.demo import tool
from maplatform.domain import PlatformError, dumps
from maplatform.preflight import diagnose
from maplatform.provider import Completion, CompletionError, ScriptedProvider
from maplatform.store import Store
from maplatform.tools import MemorySandbox


def final(text):
    return {"role": "assistant", "content": dumps({"summary": text, "artifact_ids": []})}


class CLITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "case.sqlite"
        self.store = Store(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_web_adapter_uses_cli_tool_timeout(self):
        args = parser().parse_args(["shell", "--web", "deepseek", "--tool-timeout", "37"])
        with patch.dict("os.environ", {}, clear=True):
            web, git = integrations(args, api_key="offline-test-placeholder")
        self.assertEqual(web.timeout, 37)
        self.assertIsNone(git)

    async def test_probe_does_not_accept_negated_or_empty_success(self):
        for content, code in (("OK", 0), ("NOT OK", 1), (None, 1)):
            provider = ScriptedProvider([
                {"role": "assistant", "content": None, "reasoning_content": None,
                 "tool_calls": [tool("echo", "probe_echo", {"text": "roundtrip"})]},
                {"role": "assistant", "content": content},
            ])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(await probe(provider), code)
            self.assertFalse(json.loads(output.getvalue())["reasoning_roundtrip"])
            self.assertEqual(len(provider.calls), 2)

    async def test_probe_report_saves_only_accounting_and_status(self):
        secret = "raw-reasoning-or-extension-must-not-be-logged"
        usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5, "extension": secret}
        for response, code, status in (("OK", 0, "PASS"), ("NOT OK", 1, "FAIL")):
            provider = ScriptedProvider([
                Completion({"role": "assistant", "content": None, "reasoning_content": secret,
                            "tool_calls": [tool("echo", "probe_echo", {"text": "roundtrip"})]}, usage),
                Completion({"role": "assistant", "content": response}, usage),
            ])
            path = Path(self.tmp.name) / f"probe-{status}.json"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(await probe(provider, report=path), code)
            report = json.loads(path.read_text())
            self.assertEqual(report, json.loads(output.getvalue()))
            self.assertEqual((report["complete"], report["status"], report["calls_attempted"]), (True, status, 2))
            self.assertTrue(report["usage_complete"])
            self.assertEqual(report["usage"], [{"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}] * 2)
            self.assertNotIn(secret, path.read_text() + output.getvalue())

    async def test_probe_failure_preserves_first_call_usage_without_second_request(self):
        path = Path(self.tmp.name) / "probe-failed.json"
        provider = ScriptedProvider([Completion({"role": "assistant", "content": "No tool call"}, {"total_tokens": 7})])
        with self.assertRaises(PlatformError) as raised:
            await probe(provider, report=path)
        report = json.loads(path.read_text())
        self.assertEqual(raised.exception.code, "PROBE_FAILED")
        self.assertEqual((report["status"], report["calls_attempted"], len(provider.calls)), ("FAIL", 1, 1))
        self.assertEqual(report["usage"], [{"total_tokens": 7}])

    async def test_probe_report_preserves_known_partial_usage_and_marks_unknown_billing(self):
        for error in (CompletionError("PROVIDER_TRUNCATED", "Output reached its limit", {"total_tokens": 11}),
                      PlatformError("PROVIDER_TRANSIENT", "Timeout; billing unknown")):
            path = Path(self.tmp.name) / f"{error.code}.json"
            def script(messages, tools, **kwargs):
                if len(messages) == 1:
                    # A placeholder exists and marks the attempted request before
                    # entering a provider that could take payment and then fail.
                    pending = json.loads(path.read_text())
                    self.assertEqual((pending["status"], pending["calls_attempted"]), ("NOT_FINISHED", 1))
                    return Completion({"role": "assistant", "content": None,
                                       "tool_calls": [tool("echo", "probe_echo", {"text": "roundtrip"})]}, {"total_tokens": 7})
                raise error
            provider = ScriptedProvider(script)
            with self.assertRaises(PlatformError):
                await probe(provider, report=path)
            report = json.loads(path.read_text())
            self.assertEqual((report["status"], report["calls_attempted"], len(provider.calls)), ("FAIL", 2, 2))
            self.assertEqual(report["usage"][0], {"total_tokens": 7})
            self.assertEqual(report["usage"][1], {"total_tokens": 11} if hasattr(error, "usage") else None)
            self.assertEqual(report["usage_complete"], hasattr(error, "usage"))
            self.assertEqual(report["error"]["code"], error.code)

    async def test_probe_report_collision_never_calls_provider_or_overwrites(self):
        original = Path(self.tmp.name) / "existing.json"
        original.write_text("keep existing evidence")
        alias = Path(self.tmp.name) / "alias.json"
        alias.symlink_to(original)
        for path in (original, alias, self.db):
            provider = ScriptedProvider([])
            with self.assertRaises(FileExistsError):
                await probe(provider, report=path)
            self.assertEqual(provider.calls, [])
        self.assertEqual(original.read_text(), "keep existing evidence")
        self.assertEqual(self.store.one("PRAGMA integrity_check")["integrity_check"], "ok")

    async def test_demo_does_not_execute_or_auto_answer_unrelated_work(self):
        workflow = self.store.create_workflow("unrelated live task")
        before = self.store.all("SELECT * FROM workflows")
        args = parser().parse_args(["demo", "--auto-answer"])
        with self.assertRaises(PlatformError) as raised:
            await execute(args, self.store)
        self.assertEqual(raised.exception.code, "DATABASE_BUSY")
        self.assertEqual(before, self.store.all("SELECT * FROM workflows"))
        self.assertEqual(self.store.one("SELECT status FROM tasks WHERE id=?", (workflow["root_task_id"],))["status"], "CREATED")
        self.assertEqual(self.store.all("SELECT * FROM llm_calls"), [])

    async def test_cli_persists_budget_and_reports_failed_task(self):
        provider = ScriptedProvider([final("too short"), final("should never be called")])
        provider.max_output_tokens = 512
        provider.max_context_chars = 120000
        acceptance = Path(self.tmp.name) / "acceptance.json"
        acceptance.write_text('[{"kind":"contains","text":"required output"}]')
        args = parser().parse_args(["run", "small task", "--max-calls", "1", "--max-tasks", "2", "--tool-timeout", "37", "--acceptance", str(acceptance)])
        with patch("maplatform.provider.DeepSeekProvider.from_env", return_value=provider), \
             patch("maplatform.tools.DockerSandbox", return_value=MemorySandbox()), \
             patch("maplatform.__main__.integrations", return_value=(None, None)), \
             contextlib.redirect_stdout(io.StringIO()):
            code = await execute(args, self.store)
        self.assertEqual(code, 1)
        self.assertEqual(len(provider.calls), 1)
        workflow = self.store.one("SELECT * FROM workflows")
        policy = json.loads(workflow["policy_json"])
        self.assertEqual((policy["max_calls"], policy["max_tasks"], policy["max_output_tokens"]), (1, 2, 512))
        self.assertEqual(policy["tool_timeout"], 37)
        self.assertEqual(workflow["status"], "FAILED")

    async def test_live_resume_refuses_session_fixture_before_scheduler_starts(self):
        from maplatform.sessions import SessionService
        service = SessionService(self.store)
        session = service.create()
        service.submit(session["id"], "fixture", execution_mode="fixture")
        with patch("maplatform.provider.DeepSeekProvider.from_env") as provider:
            with self.assertRaises(PlatformError) as caught:
                await execute(parser().parse_args(["resume"]), self.store)
        self.assertEqual(caught.exception.code, "FIXTURE_DATABASE")
        provider.assert_not_called()

    async def test_live_resume_refuses_unknown_session_mode(self):
        from maplatform.sessions import SessionService
        service = SessionService(self.store)
        session = service.create()
        service.submit(session["id"], "unconfigured turn")
        with patch("maplatform.provider.DeepSeekProvider.from_env") as provider:
            with self.assertRaises(PlatformError) as caught:
                await execute(parser().parse_args(["resume"]), self.store)
        self.assertEqual(caught.exception.code, "SESSION_MODE_UNKNOWN")
        provider.assert_not_called()

    async def test_live_resume_refuses_unfinished_demo(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(await execute(parser().parse_args(["demo"]), self.store), 1)
        provider = ScriptedProvider([])
        with patch("maplatform.provider.DeepSeekProvider.from_env", return_value=provider):
            with self.assertRaises(PlatformError) as raised:
                await execute(parser().parse_args(["resume"]), self.store)
        self.assertEqual(raised.exception.code, "FIXTURE_DATABASE")
        self.assertEqual(provider.calls, [])

    async def test_report_collision_rejected_before_model_calls(self):
        provider = ScriptedProvider([])
        provider.max_output_tokens = 512
        provider.max_context_chars = 120000
        suite = Path(__file__).parents[1] / "examples/text_smoke.json"
        for destination in (self.db, suite):
            args = parser().parse_args(["evaluate", str(suite), "--report", str(destination)])
            with patch("maplatform.provider.DeepSeekProvider.from_env", return_value=provider), \
                 patch("maplatform.tools.DockerSandbox", return_value=MemorySandbox()), \
                 patch("maplatform.__main__.integrations", return_value=(None, None)):
                with self.assertRaises(FileExistsError):
                    await execute(args, self.store)
            self.assertEqual(self.store.all("SELECT * FROM workflows"), [])
            self.assertEqual(provider.calls, [])
        self.assertEqual(self.store.one("PRAGMA integrity_check")["integrity_check"], "ok")


class DiagnosticTests(unittest.TestCase):
    def test_no_credentials_or_docker_are_required_for_local_diagnostic(self):
        with patch("maplatform.preflight.shutil.which", return_value=None), \
             patch("maplatform.provider.DeepSeekProvider.complete", side_effect=AssertionError("No API calls allowed")):
            report = diagnose(environ={})
            self.assertTrue(report["ready"])
            self.assertFalse(report["live_configuration_ready"])
            self.assertFalse(report["sandbox_prerequisites_ready"])
            self.assertFalse(diagnose(environ={}, require_live=True)["ready"])
            self.assertFalse(diagnose(environ={}, require_docker=True)["ready"])

    def test_diagnostic_hides_secrets_and_docker_error_text(self):
        secret = "secret-sentinel-never-print"
        env = {"DEEPSEEK_API_KEY": secret, "DEEPSEEK_MODEL": "test-model", "DEEPSEEK_BASE_URL": "https://user:" + secret + "@example.invalid"}
        with patch("maplatform.preflight.shutil.which", return_value="docker"), \
             patch("maplatform.preflight.subprocess.run", side_effect=OSError(secret)):
            report = diagnose(environ=env, require_live=True)
        self.assertNotIn(secret, json.dumps(report))
        self.assertFalse(report["ready"])

    def test_doctor_and_probe_do_not_create_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "unused" / "db.sqlite"
            with patch("sys.argv", ["maplatform", "--db", str(db), "doctor"]), \
                 patch.dict("os.environ", {}, clear=True), \
                 patch("maplatform.preflight.shutil.which", return_value=None), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            with patch("sys.argv", ["maplatform", "--db", str(db), "probe"]), \
                 patch.dict("os.environ", {}, clear=True), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 2)
            self.assertFalse(db.parent.exists())

    def test_probe_report_records_configuration_failure_without_database_or_api_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "unused" / "db.sqlite"
            path = Path(tmp) / "probe.json"
            with patch("sys.argv", ["maplatform", "--db", str(db), "probe", "--report", str(path)]), \
                 patch.dict("os.environ", {}, clear=True), \
                 patch("maplatform.provider.DeepSeekProvider.complete", side_effect=AssertionError("No API calls allowed")), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 2)
            report = json.loads(path.read_text())
            self.assertEqual((report["status"], report["calls_attempted"], report["usage"]), ("FAIL", 0, []))
            self.assertEqual(report["error"]["code"], "PROVIDER_CONFIG")
            self.assertFalse(db.parent.exists())


if __name__ == "__main__":
    unittest.main()
