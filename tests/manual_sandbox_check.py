"""Opt-in real Docker smoke check; never runs during unittest discovery.

Run from the repository: python3 tests/manual_sandbox_check.py
Requires a running Docker daemon and an explicitly preinstalled Python image.
Creates unique temporary task resources and removes only those exact resources.
No model API calls, image downloads, bind mounts, or user workflow access.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maplatform.domain import PlatformError
from maplatform.tools import DockerSandbox


_PROBE = r'''
import errno, json, os, pathlib, socket
assert os.getuid() == os.getgid() == 65534
interfaces = sorted(name for _, name in socket.if_nameindex())
interface_flags = {name: int(pathlib.Path("/sys/class/net", name, "flags").read_text(), 16)
                   for name in interfaces}
# Docker Desktop kernels may create dormant tunnel devices in a fresh network
# namespace. They do not provide connectivity; non-loopback links must be down.
assert all(name == "lo" or not flags & 1 for name, flags in interface_flags.items()), interface_flags
with socket.socket() as sock:
    sock.settimeout(0.5)
    try:
        sock.connect(("198.51.100.1", 9))
    except OSError as exc:
        assert exc.errno == errno.ENETUNREACH, repr(exc)
    else:
        raise AssertionError("Network connection unexpectedly succeeded")
root = next(line.split()[3].split(",") for line in pathlib.Path("/proc/mounts").read_text().splitlines()
            if line.split()[1] == "/")
assert "ro" in root, root
try:
    pathlib.Path("/maplatform-readonly-check").write_text("unexpected")
except OSError as exc:
    assert exc.errno in (errno.EROFS, errno.EACCES), repr(exc)
else:
    raise AssertionError("Root filesystem unexpectedly writable")
assert not pathlib.Path("/var/run/docker.sock").exists()
assert not any(key in os.environ for key in ("DEEPSEEK_API_KEY", "BRAVE_SEARCH_API_KEY"))
limits = {}
for name, paths in {
    "memory": ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    "pids": ("/sys/fs/cgroup/pids.max", "/sys/fs/cgroup/pids/pids.max"),
    "cpu": ("/sys/fs/cgroup/cpu.max",),
}.items():
    for path in paths:
        if pathlib.Path(path).exists():
            limits[name] = pathlib.Path(path).read_text().strip()
            break
if "memory" in limits: assert int(limits["memory"]) <= 268435456
if "pids" in limits: assert int(limits["pids"]) <= 64
if "cpu" in limits:
    quota, period = map(int, limits["cpu"].split())
    assert 0 < quota <= period
print(json.dumps({"uid": os.getuid(), "gid": os.getgid(), "interfaces": interfaces,
                  "non_loopback_interfaces_down": True,
                  "root_read_only": True, "network_unreachable": True,
                  "docker_socket_absent": True, "api_keys_absent": True, "cgroup_limits": limits}))
'''


async def check(image: str) -> dict:
    sandbox = DockerSandbox(image=image)
    task = {"workflow_id": "manual-sandbox-" + uuid.uuid4().hex, "id": "smoke", "owner": "coder"}
    name, volume = sandbox._names(task)
    report = {"image": image, "container": name, "volume": volume, "checks": {}}
    checks = report["checks"]
    try:
        await sandbox.write(task, "solution.py", "def add(a, b):\n    return a + b\n")
        await sandbox.write(task, "tests/test_solution.py",
                            "import unittest\nfrom solution import add\n\n"
                            "class Addition(unittest.TestCase):\n"
                            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
        assert "return a + b" in await sandbox.read(task, "solution.py")
        assert await sandbox.list(task) == ["solution.py", "tests/test_solution.py"]
        checks["file_read_write_list"] = True

        inspected = await sandbox._required(["inspect", "--format", "{{json .HostConfig}}", name],
                                             output_limit=262_144)
        host = json.loads(inspected["stdout"])
        expected = {"NetworkMode": "none", "ReadonlyRootfs": True, "Memory": 268435456,
                    "MemorySwap": 268435456, "NanoCpus": 1_000_000_000, "PidsLimit": 64}
        assert all(host.get(key) == value for key, value in expected.items()), "Docker limits differ"
        assert "ALL" in host.get("CapDrop", [])
        assert any(value.startswith("no-new-privileges") for value in host.get("SecurityOpt", []))
        checks["docker_configuration"] = expected

        probe = await sandbox.run(task, ["/usr/local/bin/python", "-I", "-c", _PROBE])
        assert probe["exit_code"] == 0, probe["stderr"]
        checks["runtime_isolation_smoke"] = json.loads(probe["stdout"])

        tested = await sandbox.run(task, ["/usr/local/bin/python", "-m", "unittest", "discover", "-s", "tests", "-v"])
        assert tested["exit_code"] == 0 and "Ran 1 test" in tested["stderr"], tested
        checks["python_unittest_and_volume_persistence"] = True

        for code, argv, timeout in (
            ("SANDBOX_TIMEOUT", ["/usr/local/bin/python", "-c", "import time; time.sleep(30)"], 0.25),
            ("SANDBOX_OUTPUT_LIMIT", ["/usr/local/bin/python", "-c", "print('x' * 1000000)"], 5),
        ):
            try:
                await sandbox.run(task, argv, timeout=timeout)
            except PlatformError as exc:
                assert exc.code == code, exc.code
            else:
                raise AssertionError("Expected " + code)
            remaining = await sandbox._required([
                "container", "ls", "--all", "--filter", "name=^/" + name + "$", "--format", "{{.ID}}"])
            assert not remaining["stdout"].strip(), "Task container survived failure"
            assert not sandbox._poisoned, "Cleanup outcome is uncertain"
            checks[code.lower() + "_container_removed"] = True

        assert "return a + b" in await sandbox.read(task, "solution.py")
        checks["workspace_reopens_after_failures"] = True
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        try:
            await sandbox.cleanup()
            if volume in sandbox._volumes:
                await sandbox._required(["volume", "rm", volume])
            checks["temporary_resources_removed"] = True
        except Exception as exc:
            report["status"] = "failed"
            report["cleanup_error"] = {"type": type(exc).__name__, "message": str(exc)}
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=os.environ.get("MULTI_AGENT_SANDBOX_IMAGE", "python:3.12-slim"))
    args = parser.parse_args()
    report = asyncio.run(check(args.image))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
