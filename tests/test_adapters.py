"""Offline adapter contracts; Git fixtures never contact an external service."""
import asyncio
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from maplatform.adapters import BraveSearchProvider, GitGateway, _NoRedirect, _parse_patch
from maplatform.domain import PlatformError


PATCH = """diff --git a/hello.txt b/hello.txt
index ce01362..94954ab 100644
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello
+updated
"""


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_endpoint_encoded_query_and_minimal_results(self):
        provider = BraveSearchProvider("private-key")
        payload = {"web": {"results": [
            {"title": "Doc", "url": "https://example.com/doc", "description": "Evidence", "secret": "ignored"},
            {"title": "Bad", "url": "file:///etc/passwd", "description": "ignored"}]}}
        requests = []

        class Opener:
            def open(self, request, timeout):
                requests.append((request, timeout))
                return io.BytesIO(json.dumps(payload).encode())

        with patch("maplatform.adapters.urllib.request.build_opener", return_value=Opener()):
            result = await provider.search("a & b? #c", limit=2)
        self.assertEqual(result, [{"title": "Doc", "url": "https://example.com/doc", "description": "Evidence"}])
        request, timeout = requests[0]
        self.assertTrue(request.full_url.startswith(provider.ENDPOINT + "?q=a+%26+b%3F+%23c&count=2"))
        self.assertEqual(request.get_header("X-subscription-token"), "private-key")
        self.assertEqual(timeout, 10)

    async def test_missing_key_invalid_query_oversized_and_secret_errors(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(PlatformError):
            BraveSearchProvider()
        provider = BraveSearchProvider("private-key", max_response_bytes=1024)
        for args in (("", 1), ("x", 11), ("x", True), ("x" * 4097, 1)):
            with self.subTest(args=args), self.assertRaises(PlatformError):
                await provider.search(*args)
        class Opener:
            def open(self, request, timeout):
                return io.BytesIO(b"x" * 1025)
        with patch("maplatform.adapters.urllib.request.build_opener", return_value=Opener()):
            with self.assertRaises(PlatformError) as error:
                await provider.search("bounded")
            self.assertEqual(error.exception.code, "SEARCH_RESPONSE_LIMIT")
        with patch("maplatform.adapters.urllib.request.build_opener") as factory:
            factory.return_value.open.side_effect = OSError("private-key was rejected")
            with self.assertRaises(PlatformError) as error:
                await provider.search("bounded")
            self.assertNotIn("private-key", str(error.exception))

    def test_redirect_is_rejected_instead_of_forwarding_key(self):
        with self.assertRaises(PlatformError) as error:
            _NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://attacker.example")
        self.assertEqual(error.exception.code, "SEARCH_REDIRECT")


class PatchParserTests(unittest.TestCase):
    def test_regular_text_diff(self):
        raw, paths = _parse_patch(PATCH)
        self.assertEqual(raw, PATCH.encode())
        self.assertEqual(paths, ["hello.txt"])

    def test_rejects_control_paths_traversal_special_modes_and_malformed_hunks(self):
        bad = [PATCH.replace("hello.txt", path) for path in (
            "../outside", "/absolute", ".git/config", "x/.gitattributes", ".gitmodules", "a\\b", "x:y")]
        bad += [PATCH.replace("100644", "120000"), PATCH.replace("100644", "160000"),
                PATCH.replace("index ce01362..94954ab 100644", "rename from hello.txt"),
                PATCH.replace("@@ -1 +1 @@", "@@ -2,10 +1 @@"),
                PATCH.replace("--- a/hello.txt", "--- a/other.txt"),
                PATCH.replace("index ce01362..94954ab 100644", "GIT binary patch"),
                PATCH + PATCH, PATCH.replace("\n", "\r\n")]
        for value in bad:
            with self.subTest(value=value[:100]), self.assertRaises(PlatformError):
                _parse_patch(value)


@unittest.skipUnless(shutil.which("git"), "Git is needed for local repository fixtures")
class GitTests(unittest.IsolatedAsyncioTestCase):
    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                              cwd=cwd or self.base, text=True, capture_output=True, check=True, timeout=5).stdout.strip()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base = self.root / "baseline"
        self.base.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@localhost")
        (self.base / "hello.txt").write_text("hello\n")
        self.git("add", "hello.txt")
        self.git("commit", "-qm", "Baseline")
        self.baseline_sha = self.git("rev-parse", "HEAD")
        self.remote = self.root / "offline-remote.git"
        self.git("init", "--bare", "-q", str(self.remote))
        self.gateway = GitGateway(self.base, self.root / "staging", str(self.remote))
        self.task = {"workflow_id": "workflow", "id": "task", "owner": "coder"}

    def tearDown(self):
        self.temp.cleanup()

    async def test_patch_creates_private_commit_and_preserves_baseline(self):
        result = await self.gateway.execute(self.task, "submit_patch", {"patch": PATCH})
        self.assertEqual(result["changed_paths"], ["hello.txt"])
        self.assertEqual(result["branch"], self.gateway.branch_for(self.task))
        self.assertNotEqual(result["commit_sha"], self.baseline_sha)
        self.assertEqual((self.base / "hello.txt").read_text(), "hello\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.baseline_sha)
        self.assertEqual(self.git("status", "--porcelain"), "")
        other = {**self.task, "id": "other"}
        status = await self.gateway.execute(other, "git_status", {})
        self.assertEqual(status["commit_sha"], self.baseline_sha)
        self.assertNotEqual(status["branch"], result["branch"])
        directory = self.gateway._directory(self.task)
        self.assertEqual(self.git("remote", cwd=directory), "")
        # No hardlinks from baseline object storage to the private clone.
        for path in (directory / ".git" / "objects").rglob("*"):
            if path.is_file():
                self.assertEqual(path.stat().st_nlink, 1)

    async def test_local_only_push_requires_exact_head_and_task_branch(self):
        staged = await self.gateway.execute(self.task, "submit_patch", {"patch": PATCH})
        args = {k: staged[k] for k in ("branch", "commit_sha")}
        before = await self.gateway.reconcile_push(self.task, args)
        self.assertEqual(before["status"], "not_pushed")
        for bad in ({**args, "branch": "main"}, {**args, "commit_sha": args["commit_sha"][:7]},
                    {**args, "commit_sha": self.baseline_sha}, {**args, "remote": "https://evil.example"}):
            with self.subTest(bad=bad), self.assertRaises(PlatformError):
                await self.gateway.execute(self.task, "git_push", bad)
        result = await self.gateway.execute(self.task, "git_push", args)
        self.assertEqual(result["status"], "pushed")
        self.assertEqual(self.git("rev-parse", "refs/heads/" + args["branch"], cwd=self.remote), args["commit_sha"])
        self.assertEqual((await self.gateway.reconcile_push(self.task, args))["status"], "pushed")

    async def test_unconfigured_push_fails_closed(self):
        unconfigured = GitGateway(self.base, self.root / "unconfigured")
        with self.assertRaises(PlatformError) as error:
            await unconfigured.execute(self.task, "git_push", {
                "branch": unconfigured.branch_for(self.task), "commit_sha": self.baseline_sha})
        self.assertEqual(error.exception.code, "GIT_PUSH_UNAVAILABLE")
        self.assertFalse(unconfigured._directory(self.task).exists())

    async def test_private_hooks_never_execute_and_task_guard_prevents_mutation(self):
        # Explicit creation here is fixture setup; read-only git_status cannot clone.
        await self.gateway._ensure(self.task)
        stage = self.gateway._directory(self.task)
        marker = self.root / "HOOK_EXECUTED"
        hook = stage / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\ntouch " + str(marker) + "\n")
        hook.chmod(0o755)
        await self.gateway.execute(self.task, "submit_patch", {"patch": PATCH})
        self.assertFalse(marker.exists())
        def denied():
            raise PlatformError("STALE_OPERATION", "Task no longer active")
        guarded = {**self.task, "id": "guarded", "_assert_active": denied}
        with self.assertRaises(PlatformError):
            await self.gateway.execute(guarded, "submit_patch", {"patch": PATCH})
        self.assertFalse(self.gateway._directory(guarded).exists())

    async def test_status_on_new_task_does_not_create_clone(self):
        before = sorted(self.gateway.staging_root.iterdir())
        status = await self.gateway.execute(self.task, "git_status", {})
        self.assertFalse(status["stage_created"])
        self.assertEqual(status["commit_sha"], self.baseline_sha)
        self.assertEqual(sorted(self.gateway.staging_root.iterdir()), before)

    async def test_guard_rechecked_after_staging_before_push(self):
        staged = await self.gateway.execute(self.task, "submit_patch", {"patch": PATCH})
        args = {k: staged[k] for k in ("branch", "commit_sha")}
        calls = []
        def guard():
            calls.append(1)
            if len(calls) > 1:
                raise PlatformError("STALE_OPERATION", "Approval expired")
        with self.assertRaises(PlatformError) as error:
            await self.gateway.execute({**self.task, "_assert_active": guard}, "git_push", args)
        self.assertEqual(error.exception.code, "STALE_OPERATION")
        self.assertEqual((await self.gateway.reconcile_push(self.task, args))["status"], "not_pushed")

    async def test_symlink_paths_and_gitlink_ancestors_rejected(self):
        (self.base / "jump").symlink_to(self.root, target_is_directory=True)
        self.git("add", "jump")
        self.git("update-index", "--add", "--cacheinfo", "160000," + self.baseline_sha + ",submodule")
        self.git("commit", "-qm", "Special entries")
        added = """diff --git a/NEW b/NEW
new file mode 100644
--- /dev/null
+++ b/NEW
@@ -0,0 +1 @@
+text
"""
        for name in ("jump/escape.txt", "submodule/escape.txt"):
            with self.subTest(name=name), self.assertRaises(PlatformError) as error:
                await self.gateway.execute(self.task, "submit_patch", {"patch": added.replace("NEW", name)})
            self.assertEqual(error.exception.code, "UNSAFE_PATCH")
        self.assertFalse((self.root / "escape.txt").exists())

    async def test_invalid_context_does_not_dirty_stage(self):
        with self.assertRaises(PlatformError):
            await self.gateway.execute(self.task, "submit_patch", {"patch": PATCH.replace("-hello", "-missing")})
        status = await self.gateway.execute(self.task, "git_status", {})
        self.assertEqual(status["changes"], "")
        self.assertEqual(status["commit_sha"], self.baseline_sha)

    async def test_stage_inside_baseline_and_credential_urls_rejected(self):
        with self.assertRaises(PlatformError):
            GitGateway(self.base, self.base / "stage")
        for remote in ("https://user:private@example.com/repo", "file:///tmp/repo", "ext::evil", "-bad"):
            with self.subTest(remote=remote), self.assertRaises(PlatformError):
                GitGateway(self.base, self.root / "config", remote)

    async def test_reconciliation_never_pushes_on_failure(self):
        args = {"branch": self.gateway.branch_for(self.task), "commit_sha": self.baseline_sha}
        commands = []
        async def fail(argv, **kwargs):
            commands.append(argv)
            raise PlatformError("GIT_TIMEOUT", "Unavailable")
        with patch.object(self.gateway, "_run", side_effect=fail):
            result = await self.gateway.reconcile_push(self.task, args)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual([c[0] for c in commands], ["ls-remote"])

    async def test_interrupted_push_process_group_is_reaped_and_outcome_unknown(self):
        class Writer:
            def close(self):
                pass
        class Process:
            def __init__(self):
                self.pid = 43210
                self.returncode = None
                self.done = asyncio.Event()
                self.stdin = Writer()
                self.stdout = self.stderr = self
            async def read(self, count):
                await self.done.wait()
                return b""
            async def wait(self):
                await self.done.wait()
                return self.returncode
        proc = Process()
        def kill(pid, sig):
            self.assertEqual(pid, proc.pid)
            proc.returncode = -9
            proc.done.set()
        with patch("maplatform.adapters.asyncio.create_subprocess_exec", return_value=proc) as start:
            with patch("maplatform.adapters.os.killpg", side_effect=kill) as kill_group:
                with self.assertRaises(PlatformError) as error:
                    await self.gateway._run(["push"], timeout=0.01, uncertain=True)
        self.assertEqual(error.exception.code, "EFFECT_UNKNOWN")
        self.assertTrue(proc.done.is_set())
        self.assertTrue(start.call_args.kwargs["start_new_session"])
        kill_group.assert_called_once()

    async def test_output_overflow_reaps_without_blocking_on_full_pipes(self):
        # No Git or task code runs here: a trusted local process creates enough
        # output to fill pipe buffers and exercise the gateway's cleanup path.
        start = asyncio.create_subprocess_exec
        for stream, uncertain in ((1, False), (2, False), (1, True)):
            async def fixture(*args, **kwargs):
                return await start(sys.executable, "-c",
                                   "import os; os.write(" + str(stream) + ", b'x' * 10000000)", **kwargs)

            with self.subTest(stream=stream, uncertain=uncertain):
                with patch("maplatform.adapters.asyncio.create_subprocess_exec", side_effect=fixture):
                    with self.assertRaises(PlatformError) as error:
                        await asyncio.wait_for(self.gateway._run(["status"], timeout=1, uncertain=uncertain), 3)
                self.assertEqual(error.exception.code, "EFFECT_UNKNOWN" if uncertain else "GIT_OUTPUT_LIMIT")

    async def test_timeout_kills_children_when_parent_already_exited(self):
        start = asyncio.create_subprocess_exec
        script = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', "
                  "'import time; time.sleep(10)'])")

        async def fixture(*args, **kwargs):
            return await start(sys.executable, "-c", script, **kwargs)

        with patch("maplatform.adapters.asyncio.create_subprocess_exec", side_effect=fixture):
            with self.assertRaises(PlatformError) as error:
                await asyncio.wait_for(self.gateway._run(["status"], timeout=0.1), 3)
        self.assertEqual(error.exception.code, "GIT_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
