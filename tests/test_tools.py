"""Offline contract tests, not evidence of Docker isolation on a real daemon."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from maplatform.domain import AGENTS, PlatformError, Policy, new_id
from maplatform.store import Store
from maplatform.tools import (DockerSandbox, MemorySandbox, ToolGateway,
                              MAX_FILE_BYTES, _FILE_SCRIPT, _path)


class PathsAndFileHelperTests(unittest.TestCase):
    def test_relative_paths_reject_escape_and_ambiguous_components(self):
        for value in ("", "/etc/passwd", "../key", "a/../key", "a//b", "./a", "a/.", "a\\b", "a\x00b", 1):
            with self.subTest(value=value), self.assertRaises(PlatformError):
                _path(value)
        self.assertEqual(_path("src/中文.py"), "src/中文.py")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def helper(self, action, **args):
        # This executes only the trusted file helper against a host test fixture.
        # Production never substitutes /workspace or executes task code on host.
        script = _FILE_SCRIPT.replace('os.open("/workspace",', "os.open(" + repr(str(self.root)) + ",")
        result = subprocess.run([sys.executable, "-I", "-c", script],
                                input=json.dumps({"action": action, **args}),
                                text=True, capture_output=True, timeout=3, check=True)
        return json.loads(result.stdout)

    def test_helper_regular_nested_utf8_files(self):
        self.assertEqual(self.helper("write", path="a/b.txt", content="內容")["result"]["bytes"], 6)
        self.assertEqual(self.helper("read", path="a/b.txt"), {"result": "內容"})
        self.assertEqual(self.helper("list"), {"result": ["a/b.txt"]})

    def test_helper_never_follows_directory_or_leaf_symlink(self):
        secret = Path(self.temp.name) / "secret.txt"
        secret.write_text("secret")
        (self.root / "leaf").symlink_to(secret)
        (self.root / "jump").symlink_to(self.temp.name, target_is_directory=True)
        for path in ("leaf", "jump/secret.txt"):
            with self.subTest(path=path):
                self.assertIn("error", self.helper("read", path=path))
                self.assertIn("error", self.helper("write", path=path, content="stolen"))
        self.assertEqual(secret.read_text(), "secret")
        self.assertEqual(self.helper("list"), {"result": []})

    def test_helper_rejects_fifo_hardlink_and_oversized_content(self):
        secret = Path(self.temp.name) / "secret.txt"
        secret.write_text("secret")
        os.link(secret, self.root / "hard")
        os.mkfifo(self.root / "fifo")
        for name in ("hard", "fifo"):
            self.assertEqual(self.helper("read", path=name)["error"]["code"], "UNSAFE_FILE")
            self.assertEqual(self.helper("write", path=name, content="x")["error"]["code"], "UNSAFE_FILE")
        (self.root / "big").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
        self.assertEqual(self.helper("read", path="big")["error"]["code"], "FILE_LIMIT")


class MemoryFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_is_task_private_and_cannot_execute(self):
        sandbox = MemorySandbox()
        a = {"id": "a", "workflow_id": "w", "owner": "coder"}
        b = {"id": "b", "workflow_id": "w", "owner": "coder"}
        await sandbox.write(a, "a.txt", "hello")
        self.assertEqual(await sandbox.list(b), [])
        with self.assertRaises(PlatformError) as denied:
            await sandbox.run(a, ["python", "-c", "print('x')"])
        self.assertEqual(denied.exception.code, "EXECUTION_UNAVAILABLE")


class FakeDocker(DockerSandbox):
    def __init__(self):
        super().__init__()
        self.docker = "/fake/docker"
        self.commands = []
        self.raise_exec = None

    async def _docker(self, args, **kwargs):
        self.commands.append((args, kwargs))
        if args[:2] == ["container", "inspect"]:
            return {"exit_code": 1, "stdout": "", "stderr": "not found"}
        if args[0] == "exec":
            if self.raise_exec:
                raise self.raise_exec
            if "stdin" in kwargs and kwargs["stdin"]:
                arg = json.loads(kwargs["stdin"])
                value = "hello" if arg["action"] == "read" else {"path": arg.get("path"), "bytes": 5}
                return {"exit_code": 0, "stdout": json.dumps({"result": value}), "stderr": ""}
            return {"exit_code": 0, "stdout": "ok\n", "stderr": ""}
        return {"exit_code": 0, "stdout": "", "stderr": ""}


class DockerConstructionTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_code_uses_shell_free_limited_nonroot_container(self):
        sandbox = FakeDocker()
        task = {"id": "task-1", "workflow_id": "workflow-1", "owner": "coder"}
        command = ["python", "-c", "print('$(touch /host)')"]
        result = await sandbox.run(task, command, timeout=2)
        self.assertEqual(result["stdout"], "ok\n")
        main = next(a for a, _ in sandbox.commands if a[:2] == ["run", "--detach"])
        for flag, value in (("--network", "none"), ("--user", "65534:65534"),
                            ("--memory", "256m"), ("--memory-swap", "256m"),
                            ("--cpus", "1"), ("--pids-limit", "64"),
                            ("--cap-drop", "ALL"), ("--pull", "never")):
            self.assertEqual(main[main.index(flag) + 1], value)
        self.assertIn("--read-only", main)
        self.assertIn("no-new-privileges:true", main)
        self.assertIn("/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777", main)
        self.assertFalse(any("type=bind" in a or "docker.sock" in a for a in main))
        execute, opts = next((a, k) for a, k in sandbox.commands if a[0] == "exec")
        self.assertEqual(execute[3:], command)
        self.assertEqual(opts["timeout"], 2)
        name, volume = sandbox._names(task)
        # Success also kills detached children. The persistent volume survives.
        self.assertIn((["rm", "--force", name], {"timeout": 10}), sandbox.commands)
        self.assertNotIn(name, sandbox._active)
        await sandbox.cleanup()
        self.assertFalse(any(a[:2] == ["volume", "rm"] for a, _ in sandbox.commands))
        self.assertIn(volume, sandbox._volumes)

    async def test_timeout_and_output_overflow_remove_container_before_reuse(self):
        for code in ("SANDBOX_TIMEOUT", "SANDBOX_OUTPUT_LIMIT"):
            with self.subTest(code=code):
                sandbox = FakeDocker()
                task = {"id": "a", "workflow_id": "w", "owner": "coder"}
                sandbox.raise_exec = PlatformError(code, "fixture")
                with self.assertRaises(PlatformError):
                    await sandbox.run(task, ["python"])
                name, _ = sandbox._names(task)
                self.assertEqual(sandbox.commands[-1][0], ["rm", "--force", name])
                sandbox.raise_exec = None
                await sandbox.read(task, "a.txt")
                self.assertEqual(sum(a[:2] == ["run", "--detach"] for a, _ in sandbox.commands), 2)

    async def test_interrupted_startup_removes_named_helper_or_task_container(self):
        task = {"id": "a", "workflow_id": "w", "owner": "coder"}
        for stage in ("--rm", "--detach"):
            for failure in (PlatformError("SANDBOX_TIMEOUT", "fixture"), asyncio.CancelledError()):
                with self.subTest(stage=stage, failure=type(failure).__name__):
                    sandbox = FakeDocker()
                    name, _ = sandbox._names(task)
                    expected = name + "-init" if stage == "--rm" else name
                    original = sandbox._docker

                    async def interrupt(args, **kwargs):
                        if args[:2] == ["run", stage]:
                            self.assertIn(expected, sandbox._active)
                            self.assertEqual(args[args.index("--name") + 1], expected)
                            raise failure
                        return await original(args, **kwargs)

                    with patch.object(sandbox, "_docker", side_effect=interrupt):
                        with self.assertRaises(type(failure)):
                            await sandbox.run(task, ["python"])
                    self.assertIn((["rm", "--force", expected], {"timeout": 10}), sandbox.commands)
                    self.assertFalse(sandbox._active)
                    self.assertFalse(sandbox._poisoned)

    async def test_cleanup_confirms_absence_when_auto_removed_helper_is_gone(self):
        sandbox = FakeDocker()
        name = "maplatform-fixture-init"
        sandbox._active.add(name)
        original = sandbox._docker

        async def already_removed(args, **kwargs):
            if args[0] == "rm":
                return {"exit_code": 1, "stdout": "", "stderr": "not found"}
            return await original(args, **kwargs)

        with patch.object(sandbox, "_docker", side_effect=already_removed):
            await sandbox._discard(name)
        self.assertNotIn(name, sandbox._poisoned)
        self.assertIn("name=^/" + name + "$", sandbox.commands[-1][0])

    async def test_cleanup_keeps_uncertain_container_poisoned(self):
        sandbox = FakeDocker()
        name = "maplatform-fixture"
        sandbox._active.add(name)

        async def unavailable(args, **kwargs):
            return {"exit_code": 1, "stdout": "", "stderr": "daemon unavailable"}

        with patch.object(sandbox, "_docker", side_effect=unavailable):
            await sandbox._discard(name)
        self.assertIn(name, sandbox._poisoned)
        with self.assertRaises(PlatformError) as error:
            await sandbox.cleanup()
        self.assertEqual(error.exception.code, "SANDBOX_CLEANUP_FAILED")

    async def test_output_overflow_drains_subprocess_pipes_before_reaping(self):
        # Exercise pipe backpressure using trusted fixture code only. This is a
        # subprocess transport test, not generated-code execution or Docker QA.
        sandbox = DockerSandbox()
        sandbox.docker = sys.executable
        for stream in (1, 2):
            with self.subTest(stream=stream), self.assertRaises(PlatformError) as error:
                await asyncio.wait_for(sandbox._docker([
                    "-c", "import os; os.write(" + str(stream) + ", b'x' * 10000000)"], timeout=1), 3)
            self.assertEqual(error.exception.code, "SANDBOX_OUTPUT_LIMIT")

    @unittest.skipUnless(os.name == "posix", "POSIX process-group cleanup")
    async def test_timeout_kills_children_after_cli_parent_has_exited(self):
        sandbox = DockerSandbox()
        sandbox.docker = sys.executable
        script = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', "
                  "'import time; time.sleep(10)'])")
        with self.assertRaises(PlatformError) as error:
            await asyncio.wait_for(sandbox._docker(["-c", script], timeout=0.1), 3)
        self.assertEqual(error.exception.code, "SANDBOX_TIMEOUT")

    async def test_missing_docker_fails_without_host_fallback(self):
        sandbox = DockerSandbox(docker="nonexistent-maplatform-docker-binary")
        with self.assertRaises(PlatformError) as error:
            await sandbox.run({"id": "a", "workflow_id": "w", "owner": "coder"}, [sys.executable, "-V"])
        self.assertEqual(error.exception.code, "SANDBOX_UNAVAILABLE")

    async def test_identity_changes_create_separate_volumes(self):
        sandbox = FakeDocker()
        base = {"id": "a", "workflow_id": "w", "owner": "coder"}
        names = {sandbox._names(base)}
        for key in base:
            names.add(sandbox._names({**base, key: "different"}))
        self.assertEqual(len(names), 4)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.policy = Policy()
        self.sandbox = MemorySandbox()
        self.gateway = ToolGateway(self.store, self.sandbox, self.policy)

    def tearDown(self):
        self.store.close()

    def task(self, owner="coder", user="human:local", *, policy=None):
        workflow = self.store.create_workflow("fixture", owner=owner, user_id=user, policy=policy)
        self.store.route(workflow["root_task_id"], owner)
        return self.store.claim(workflow["root_task_id"])

    def sibling(self, task, owner="coder", *, kind="WORK"):
        child_id = new_id("task")
        with self.store.tx() as conn:
            self.store._insert_task(conn, child_id, task["workflow_id"], "fixture child", owner, [], [],
                                    parent=task, kind=kind)
        self.store.route(child_id, owner)
        return self.store.claim(child_id)

    async def test_strict_arguments_and_agent_allowlist(self):
        coder = self.task()
        for name, arguments in (("read_file", {"path": "a", "owner": "reviewer"}),
                                ("read_file", {}), ("run_code", {"argv": "sh -c pwd"}),
                                ("run_code", {"argv": ["python"], "timeout": True}),
                                ("write_file", {"path": "../outside", "content": "x"}),
                                ("write_file", {"path": "a", "content": "\ud800"})):
            with self.subTest(name=name, arguments=arguments), self.assertRaises(PlatformError):
                await self.gateway.execute(coder, name, arguments)
        reviewer = self.task("reviewer")
        with self.assertRaises(PlatformError) as denied:
            await self.gateway.execute(reviewer, "run_code", {"argv": ["python"]})
        self.assertEqual(denied.exception.code, "FORBIDDEN")
        with self.assertRaises(PlatformError):
            await self.gateway.execute({**coder, "owner": "reviewer"}, "read_file", {"path": "a"})

    async def test_reply_tasks_have_only_readonly_tools_even_when_forged(self):
        parent = self.task()
        reply = self.sibling(parent, kind="REPLY")
        names = {s["function"]["name"] for s in self.gateway.schemas(AGENTS["coder"], "REPLY")}
        self.assertTrue(names <= {"list_files", "read_file", "search_memory", "search_web", "read_web_result", "git_status"})
        # An application snapshot claiming WORK does not override persisted REPLY.
        with self.assertRaises(PlatformError) as denied:
            await self.gateway.execute({**reply, "kind": "WORK"}, "write_file", {"path": "a", "content": "x"})
        self.assertEqual(denied.exception.code, "FORBIDDEN")
        with self.assertRaises(PlatformError):
            await self.gateway.execute(reply, "remember", {"text": "mutate"})

    async def test_artifact_acl_same_workflow_and_owner_task(self):
        owner = self.task()
        other = self.sibling(owner, "reviewer")
        same_role = self.sibling(owner)
        await self.gateway.execute(owner, "write_file", {"path": "a.py", "content": "print(1)"})
        artifact = await self.gateway.execute(owner, "publish_artifact", {"path": "a.py", "name": "a.py", "grantees": ["reviewer"]})
        result = await self.gateway.execute(other, "import_artifact", {"artifact_id": artifact["artifact_id"], "path": "review/a.py"})
        self.assertEqual(result["bytes"], 8)
        self.assertEqual(await self.sandbox.read(other, "review/a.py"), "print(1)")
        for denied_task in (same_role, self.task("reviewer")):
            with self.assertRaises(PlatformError) as denied:
                await self.gateway.execute(denied_task, "import_artifact", {"artifact_id": artifact["artifact_id"], "path": "stolen.py"})
            self.assertEqual(denied.exception.code, "FORBIDDEN")

    async def test_default_parent_grant_and_invalid_grantee(self):
        parent = self.task("planner")
        child = self.sibling(parent)
        await self.gateway.execute(child, "write_file", {"path": "a", "content": "hi"})
        result = await self.gateway.execute(child, "publish_artifact", {"path": "a", "name": "a"})
        self.assertEqual(result["grantees"], ["planner"])
        with self.assertRaises(PlatformError):
            await self.gateway.execute(child, "publish_artifact", {"path": "a", "name": "a", "grantees": ["human:other"]})

    async def test_memory_is_scoped_to_agent_and_human(self):
        original = self.task()
        await self.gateway.execute(original, "remember", {"text": "private preference"})
        for task, expected in ((self.task("coder"), 1), (self.task("reviewer"), 0),
                               (self.task("coder", user="human:other"), 0)):
            result = await self.gateway.execute(task, "search_memory", {"query": "private"})
            self.assertEqual(len(result["memories"]), expected)

    async def test_objective_terminal_experience_has_same_memory_boundaries(self):
        original = self.task()
        self.store.fail(original["id"], "fixture-terminal-evidence")
        for task, expected in ((self.task("coder"), 1), (self.task("reviewer"), 0),
                               (self.task("coder", user="human:other"), 0)):
            result = await self.gateway.execute(task, "search_memory", {"query": "fixture-terminal-evidence"})
            self.assertEqual(len(result["memories"]), expected)
            if expected:
                experience = json.loads(result["memories"][0]["text"])
                self.assertEqual(experience["outcome"], "FAILED")
                self.assertEqual(experience["task_id"], original["id"])

    async def test_stale_attempt_and_cancel_during_file_read_cannot_publish(self):
        task = self.task()
        await self.gateway.execute(task, "write_file", {"path": "a", "content": "x"})
        original_read = self.sandbox.read

        async def cancelling_read(current, path):
            data = await original_read(current, path)
            self.store.fail(current["id"], "cancelled in fixture", status="CANCELLED")
            return data

        self.sandbox.read = cancelling_read
        with self.assertRaises(PlatformError) as denied:
            await self.gateway.execute(task, "publish_artifact", {"path": "a", "name": "a"})
        self.assertEqual(denied.exception.code, "STALE_ATTEMPT")
        self.assertEqual(self.store.all("SELECT * FROM artifacts"), [])
        with self.assertRaises(PlatformError):
            await self.gateway.execute(task, "remember", {"text": "stale"})

    async def test_missing_external_services_explicitly_fail(self):
        coder, researcher = self.task(), self.task("researcher")
        for task, name, args in ((coder, "git_status", {}),
                                  (coder, "submit_patch", {"patch": "diff"}),
                                  (coder, "git_push", {"branch": "main", "commit_sha": "a" * 40}),
                                  (researcher, "search_web", {"query": "evidence"})):
            with self.subTest(name=name), self.assertRaises(PlatformError) as unavailable:
                await self.gateway.execute(task, name, args)
            self.assertEqual(unavailable.exception.code, "TOOL_UNAVAILABLE")
        names = {x["function"]["name"] for x in self.gateway.schemas(AGENTS["coder"])}
        self.assertFalse(names & {"git_status", "submit_patch", "git_push"})

    async def test_injected_search_is_bounded_and_cannot_override_actor(self):
        async def search(query, *, limit):
            return [{"title": query, "url": "https://example.test/source"}][:limit]

        self.gateway.search = search
        task = self.task("researcher")
        result = await self.gateway.execute(task, "search_web", {"query": "evidence", "limit": 1})
        self.assertEqual(result["results"][0]["title"], "evidence")
        with self.assertRaises(PlatformError):
            await self.gateway.execute(task, "search_web", {"query": "evidence", "agent_id": "coder"})

    async def test_resumed_workflow_uses_saved_code_and_test_timeout(self):
        received = []

        async def run(task, argv, timeout):
            received.append(timeout)
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        self.sandbox.run = run
        self.gateway.policy = Policy(tool_timeout=2)
        task = self.task(policy=Policy(tool_timeout=7))
        await self.gateway.execute(task, "run_code", {"argv": ["python"], "timeout": 30})
        await self.gateway.execute(task, "run_tests", {})
        await self.gateway.execute(task, "run_tests", {"timeout": 1})
        other = self.task(policy=Policy(tool_timeout=1))
        await self.gateway.execute(other, "run_code", {"argv": ["python"]})
        self.assertEqual(received, [7, 7, 1, 1])

    async def test_resumed_workflow_times_out_web_using_saved_policy(self):
        cancelled = []

        class SlowWeb:
            async def search(self, query, limit):
                try:
                    await asyncio.sleep(1.1)
                    return []
                except asyncio.CancelledError:
                    cancelled.append("search")
                    raise

            async def fetch(self, url):
                try:
                    await asyncio.sleep(1.1)
                    return {"content": "fixture"}
                except asyncio.CancelledError:
                    cancelled.append("fetch")
                    raise

        self.gateway.search = SlowWeb()
        self.gateway.policy = Policy(tool_timeout=5)
        task = self.task("researcher", policy=Policy(tool_timeout=1))
        for name, args in (("search_web", {"query": "fixture"}),
                           ("fetch_web", {"url": "https://example.com/"})):
            with self.subTest(tool=name), self.assertRaises(PlatformError) as failure:
                await self.gateway.execute(task, name, args)
            self.assertEqual(failure.exception.code, "TOOL_TIMEOUT")
        self.assertEqual(cancelled, ["search", "fetch"])

    async def test_git_adapter_requires_live_exact_task_approval_for_push(self):
        class GitFixture:
            def __init__(self):
                self.calls = []

            async def execute(self, task, name, args):
                self.calls.append((task["id"], name, args))
                return {"fixture": name}

        git = GitFixture()
        self.gateway.git = git
        task = self.task()
        args = {"branch": "maplatform/task-branch", "commit_sha": "a" * 40}
        names = {x["function"]["name"] for x in self.gateway.schemas(AGENTS["coder"])}
        self.assertTrue({"git_status", "submit_patch", "git_push"} <= names)
        self.assertEqual(await self.gateway.execute(task, "git_status", {}), {"fixture": "git_status"})
        self.assertEqual(await self.gateway.execute(task, "submit_patch", {"patch": "diff"}), {"fixture": "submit_patch"})
        with self.assertRaises(PlatformError) as missing:
            await self.gateway.execute(task, "git_push", args)
        self.assertEqual(missing.exception.code, "APPROVAL_REQUIRED")

        wait = self.store.defer(task["id"], task["active_attempt_id"], "push-call", "HUMAN_APPROVAL",
                                {"tool_name": "git_push", "arguments": args}, self.policy)
        self.store.answer(wait["ref_id"], "APPROVED")
        self.assertEqual(await self.gateway.execute(task, "git_push", args), {"fixture": "git_push"})
        calls_before = len(git.calls)
        for caller, arguments in ((task, {**args, "commit_sha": "b" * 40}),
                                  (self.sibling(task), args)):
            with self.assertRaises(PlatformError) as mismatch:
                await self.gateway.execute(caller, "git_push", arguments)
            self.assertEqual(mismatch.exception.code, "APPROVAL_REQUIRED")
        with self.store.tx() as conn:
            conn.execute("UPDATE waits SET deadline=? WHERE id=?", (time.time() - 1, wait["id"]))
        with self.assertRaises(PlatformError) as expired:
            await self.gateway.execute(task, "git_push", args)
        self.assertEqual(expired.exception.code, "APPROVAL_REQUIRED")
        self.assertEqual(len(git.calls), calls_before)

    async def test_git_push_full_sha_and_unmodified_approval_payload_required(self):
        class GitFixture:
            async def execute(self, task, name, args):
                return {"fixture": name}

        self.gateway.git = GitFixture()
        task = self.task()
        args = {"branch": "maplatform/task-branch", "commit_sha": "a" * 40}
        for bad in ({"branch": "main"}, {**args, "commit_sha": "main"},
                    {**args, "commit_sha": "a" * 41}):
            with self.assertRaises(PlatformError) as invalid:
                await self.gateway.execute(task, "git_push", bad)
            self.assertEqual(invalid.exception.code, "INVALID_ARGUMENT")
        wait = self.store.defer(task["id"], task["active_attempt_id"], "push-call", "HUMAN_APPROVAL",
                                {"tool_name": "git_push", "arguments": args}, self.policy)
        self.store.answer(wait["ref_id"], "APPROVED")
        with self.store.tx() as conn:
            conn.execute("UPDATE approvals SET args_json=? WHERE id=?",
                         (json.dumps({**args, "commit_sha": "b" * 40}), wait["ref_id"]))
        with self.assertRaises(PlatformError) as tampered:
            await self.gateway.execute(task, "git_push", args)
        self.assertEqual(tampered.exception.code, "APPROVAL_REQUIRED")

    async def test_git_guard_rechecks_after_adapter_wait(self):
        store = self.store

        class ExpiringGit:
            async def execute(self, task, name, args):
                # Simulate expiry while waiting for a host staging lock.
                with store.tx() as conn:
                    conn.execute("UPDATE waits SET deadline=? WHERE task_id=?", (time.time() - 1, task["id"]))
                task["_assert_active"]()
                raise AssertionError("A guarded external effect should never be reached")

        self.gateway.git = ExpiringGit()
        task = self.task()
        args = {"branch": "maplatform/task-branch", "commit_sha": "a" * 40}
        wait = self.store.defer(task["id"], task["active_attempt_id"], "push-call", "HUMAN_APPROVAL",
                                {"tool_name": "git_push", "arguments": args}, self.policy)
        self.store.answer(wait["ref_id"], "APPROVED")
        with self.assertRaises(PlatformError) as expired:
            await self.gateway.execute(task, "git_push", args)
        self.assertEqual(expired.exception.code, "APPROVAL_REQUIRED")


if __name__ == "__main__":
    unittest.main()
