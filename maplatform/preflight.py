"""Local readiness checks: no API requests, image pulls or workflow mutations."""
from __future__ import annotations

from contextlib import closing
import os
import platform
import shutil
import sqlite3
import subprocess
import sys

from .domain import PlatformError
from .provider import DeepSeekProvider


def diagnose(*, require_live=False, require_docker=False, environ=None):
    env = os.environ if environ is None else environ
    checks = []

    def check(name, ok, message, required=True):
        checks.append({"name": name, "status": "PASS" if ok else "FAIL" if required else "WARN", "message": message})

    check("python", sys.version_info >= (3, 11), f"Python {platform.python_version()} on {platform.system()}")
    try:
        with closing(sqlite3.connect(":memory:")) as conn:
            ok = conn.execute("SELECT json_extract('{\"ok\":1}', '$.ok')").fetchone()[0] == 1
        check("sqlite", ok, f"SQLite {sqlite3.sqlite_version}; JSON support checked in memory")
    except sqlite3.Error:
        check("sqlite", False, "SQLite with JSON support is required")

    key_present = bool(env.get("DEEPSEEK_API_KEY", "").strip())
    model_present = bool(env.get("DEEPSEEK_MODEL", "").strip())
    check("api_key", key_present, "Configured (value hidden)" if key_present else "Set DEEPSEEK_API_KEY in your terminal", require_live)
    check("model", model_present, "Explicit model configured" if model_present else "Set DEEPSEEK_MODEL to a model available to your account", require_live)
    config_valid = False
    try:
        # Placeholders only allow validation of remaining settings before the
        # user enters a key. They are never used for a request or written out.
        overrides = {}
        if not key_present:
            overrides["api_key"] = "local-preflight-placeholder"
        if not model_present:
            overrides["model"] = "local-preflight-placeholder"
        provider = DeepSeekProvider.from_env(env, **overrides)
        config_valid = True
        check("provider_config", True, f"Valid local settings; thinking={provider.thinking}, output token cap={provider.max_output_tokens}, timeout={provider.timeout:g}s. Account access is untested.")
    except PlatformError as exc:
        # Provider configuration errors contain fixed safe text only.
        check("provider_config", False, str(exc), require_live)

    docker = shutil.which("docker")
    daemon_ok = image_ok = False
    image = env.get("MULTI_AGENT_SANDBOX_IMAGE", "python:3.12-slim")
    image_valid = bool(image) and not image.startswith("-") and "\x00" not in image
    check("docker_cli", docker is not None, "Docker CLI found" if docker else "Install Docker Desktop/Engine for file and code tasks", require_docker)

    def succeeds(arguments):
        try:
            result = subprocess.run([docker, *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=5, check=False)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    if docker:
        daemon_ok = succeeds(["info", "--format", "{{.ServerVersion}}"])
        if daemon_ok and image_valid:
            image_ok = succeeds(["image", "inspect", image])
    check("docker_daemon", daemon_ok, "Docker daemon reachable" if daemon_ok else "Docker daemon is unavailable; start a working Docker installation", require_docker)
    check("sandbox_image", image_ok, "Configured sandbox image exists locally" if image_ok else "Sandbox image is not verified; start Docker and explicitly install the configured image", require_docker)
    web = env.get("MULTI_AGENT_WEB_PROVIDER", "brave")
    web_ready = key_present if web == "deepseek" else bool(env.get("BRAVE_SEARCH_API_KEY")) if web == "brave" else web == "off"
    check("search", web_ready, f"Batch web mode={web}; interactive default=deepseek. Configuration only, not contacted.", False)
    check("git", bool(env.get("MULTI_AGENT_GIT_REPO")), "Git configured (repository/remote untested)" if env.get("MULTI_AGENT_GIT_REPO") else "Optional Git integration disabled", False)
    return {
        "ready": not any(item["status"] == "FAIL" for item in checks),
        "live_configuration_ready": key_present and model_present and config_valid,
        "sandbox_prerequisites_ready": daemon_ok and image_ok,
        "checks": checks,
        "scope": "Local prerequisites only. No API calls, credential validation, container isolation test, or image downloads were performed.",
    }
