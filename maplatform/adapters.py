"""Optional operator-configured adapters; model input never selects a remote.

Git operates on private clones of a trusted baseline, never task sandbox files.
Only text patches cross that boundary. Search snippets are untrusted evidence.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import time
import urllib.error
import urllib.parse
import urllib.request

from .domain import PlatformError, dumps


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PlatformError("SEARCH_REDIRECT", "Search endpoint redirected; request rejected.")


class BraveSearchProvider:
    """Brave Web Search: fixed HTTPS endpoint, no redirects or page fetching.

    API contract: https://api-dashboard.search.brave.com/app/documentation/web-search/get-started
    The key stays in this host-side adapter and never enters sandbox environments.
    """

    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str | None = None, *, timeout: float = 10,
                 max_response_bytes: int = 1_048_576):
        self._api_key = api_key if api_key is not None else os.environ.get("BRAVE_SEARCH_API_KEY", "")
        if (not isinstance(self._api_key, str) or not self._api_key
                or len(self._api_key) > 4096 or any(ord(c) < 32 for c in self._api_key)):
            raise PlatformError("SEARCH_UNAVAILABLE", "Configure BRAVE_SEARCH_API_KEY before enabling web search.")
        if not 0 < timeout <= 30 or not 1024 <= max_response_bytes <= 2_097_152:
            raise PlatformError("INVALID_CONFIG", "Search limits are invalid.")
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        if (not isinstance(query, str) or not query.strip() or len(query) > 4096
                or "\x00" in query or type(limit) is not int or not 1 <= limit <= 10):
            raise PlatformError("INVALID_ARGUMENT", "Search requires a bounded query and a limit between 1 and 10.")
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._search, query, limit), self.timeout * 2 + 1)
        except asyncio.TimeoutError:
            raise PlatformError("SEARCH_TIMEOUT", "Search request exceeded its time limit.") from None

    def _search(self, query: str, limit: int) -> list[dict]:
        url = self.ENDPOINT + "?" + urllib.parse.urlencode({"q": query, "count": limit})
        request = urllib.request.Request(url, headers={
            "Accept": "application/json", "X-Subscription-Token": self._api_key,
            "User-Agent": "maplatform/1.0"})
        # Do not inherit proxy environment variables or forward credentials across redirects.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            started = time.monotonic()
            with opener.open(request, timeout=self.timeout) as response:
                chunks, length = [], 0
                while length <= self.max_response_bytes:
                    if time.monotonic() - started >= self.timeout:
                        raise PlatformError("SEARCH_TIMEOUT", "Search response exceeded its time limit.")
                    # read1 performs at most one underlying read, avoiding an
                    # unbounded stream of tiny receives inside read(n).
                    chunk = response.read1(min(4096, self.max_response_bytes + 1 - length))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    length += len(chunk)
                raw = b"".join(chunks)
            if len(raw) > self.max_response_bytes:
                raise PlatformError("SEARCH_RESPONSE_LIMIT", "Search response exceeded the byte limit.")
            payload = json.loads(raw.decode("utf-8"))
            results = payload.get("web", {}).get("results", [])
            if not isinstance(results, list):
                raise ValueError("invalid search results")
            out = []
            for entry in results[:limit]:
                if not isinstance(entry, dict):
                    continue
                link = entry.get("url", "")
                if not isinstance(link, str) or len(link) > 4096:
                    continue
                parsed = urllib.parse.urlsplit(link)
                if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                    continue
                out.append({"title": str(entry.get("title", ""))[:512], "url": link,
                            "description": str(entry.get("description", ""))[:4096]})
            return out
        except PlatformError:
            raise
        except (OSError, ValueError, TypeError, AttributeError, urllib.error.URLError):
            # Provider error bodies and URLs may contain secrets: never expose them.
            raise PlatformError("SEARCH_FAILED", "Search provider request failed; check host-side configuration.") from None


_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_HEADER = re.compile(r"diff --git a/(\S+) b/(\S+)\Z")
_HUNK = re.compile(r"@@ -([0-9]{1,10})(?:,([0-9]{1,10}))? \+([0-9]{1,10})(?:,([0-9]{1,10}))? @@(?:.*)\Z")


def _patch_path(path: str) -> str:
    parts = path.split("/")
    if (not path or len(path) > 512 or path.startswith("/") or "\\" in path
            or ":" in path or any(c.isspace() or ord(c) < 32 for c in path)
            or any(p in ("", ".", "..") or p.casefold() in (".git", ".gitattributes", ".gitmodules") for p in parts)):
        raise PlatformError("UNSAFE_PATCH", "Patch paths must be ordinary relative files without Git control files.")
    return path


def _parse_patch(patch: str) -> tuple[bytes, list[str]]:
    """Accept a deliberately small subset of git unified diff, fail closed.

    Quoted/whitespace paths, binary patches, copies, renames, special modes and
    metadata-only patches are unsupported. git apply --check validates hunks too.
    """
    if not isinstance(patch, str) or not patch or "\x00" in patch or "\r" in patch:
        raise PlatformError("UNSAFE_PATCH", "Supply a UTF-8 git unified diff with LF line endings.")
    try:
        raw = patch.encode("utf-8")
    except UnicodeError:
        raise PlatformError("UNSAFE_PATCH", "Patch must be valid UTF-8 text.") from None
    if len(raw) > 262_144:
        raise PlatformError("PATCH_LIMIT", "Patch exceeds 256 KiB.")
    lines = patch.splitlines()
    i, hunks, paths = 0, 0, []
    while i < len(lines):
        header = _HEADER.fullmatch(lines[i])
        if not header or header[1] != header[2]:
            raise PlatformError("UNSAFE_PATCH", "Only same-path git unified diffs are supported.")
        path = _patch_path(header[1])
        if path in paths or len(paths) >= 64:
            raise PlatformError("PATCH_LIMIT", "Patch has duplicate paths or too many files.")
        paths.append(path)
        i += 1
        while i < len(lines) and not lines[i].startswith("--- "):
            line = lines[i]
            if re.fullmatch(r"index [0-9a-f]+\.\.[0-9a-f]+(?: (?:100644|100755))?", line):
                pass
            elif re.fullmatch(r"(?:new file|deleted file|old|new) mode (?:100644|100755)", line):
                pass
            else:
                raise PlatformError("UNSAFE_PATCH", "Patch metadata is unsupported or changes a special file.")
            i += 1
        if i + 1 >= len(lines):
            raise PlatformError("UNSAFE_PATCH", "Patch is missing unified file headers.")
        old, new = lines[i], lines[i + 1]
        if (old not in ("--- a/" + path, "--- /dev/null")
                or new not in ("+++ b/" + path, "+++ /dev/null")
                or (old == "--- /dev/null" and new == "+++ /dev/null")):
            raise PlatformError("UNSAFE_PATCH", "Unified headers must agree with the diff path.")
        i += 2
        file_hunks = 0
        while i < len(lines) and not lines[i].startswith("diff --git "):
            hunk = _HUNK.fullmatch(lines[i])
            if not hunk:
                raise PlatformError("UNSAFE_PATCH", "Patch has invalid unified hunk syntax.")
            old_count = int(hunk[2]) if hunk[2] is not None else 1
            new_count = int(hunk[4]) if hunk[4] is not None else 1
            i += 1
            consumed_old = consumed_new = 0
            previous_content = False
            while i < len(lines) and not lines[i].startswith(("@@ ", "diff --git ")):
                line = lines[i]
                if line == "\\ No newline at end of file" and previous_content:
                    previous_content = False
                elif line.startswith(" "):
                    consumed_old += 1
                    consumed_new += 1
                    previous_content = True
                elif line.startswith("-"):
                    consumed_old += 1
                    previous_content = True
                elif line.startswith("+"):
                    consumed_new += 1
                    previous_content = True
                else:
                    raise PlatformError("UNSAFE_PATCH", "Patch contains unsupported hunk content.")
                i += 1
            if (consumed_old, consumed_new) != (old_count, new_count):
                raise PlatformError("UNSAFE_PATCH", "Unified hunk line counts do not match.")
            file_hunks += 1
            hunks += 1
            if hunks > 1024:
                raise PlatformError("PATCH_LIMIT", "Patch has too many hunks.")
        if not file_hunks:
            raise PlatformError("UNSAFE_PATCH", "Each patch file must contain a text hunk.")
    return raw, paths


class GitGateway:
    """Controlled text patch -> private commit -> approved exact commit push.

    base_repository and staging_root are trusted operator configuration. Keep
    staging_root inaccessible to task containers and untrusted local users.
    Local directory remotes are accepted only as explicit operator configuration
    (primarily for offline tests). No remote or command is accepted from a model.
    Caller owns durable effect reservation and exact-argument human approval.
    """

    def __init__(self, base_repository: Path, staging_root: Path, remote_url: str | None = None):
        self.base_repository = Path(base_repository).resolve(strict=True)
        self.staging_root = Path(staging_root).resolve()
        if (not self.base_repository.is_dir() or self.staging_root == self.base_repository
                or self.base_repository in self.staging_root.parents):
            raise PlatformError("INVALID_CONFIG", "Git staging must be outside the trusted baseline repository.")
        self.staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.git = shutil.which("git")
        self.remote_url = self._remote(remote_url)
        self._locks: dict[str, asyncio.Lock] = {}
        self._poisoned: set[str] = set()

    @staticmethod
    def _remote(value):
        if value is None:
            return None
        if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
            raise PlatformError("INVALID_CONFIG", "Git remote configuration is invalid.")
        if value.startswith("/"):
            target = Path(value).resolve(strict=True)
            if not target.is_dir():
                raise PlatformError("INVALID_CONFIG", "Local Git remote must be a configured directory.")
            return str(target)
        url = urllib.parse.urlsplit(value)
        if (url.scheme not in ("https", "ssh") or not url.hostname or not url.path
                or url.password or url.query or url.fragment or (url.scheme == "https" and url.username)):
            raise PlatformError("INVALID_CONFIG", "Use an HTTPS/SSH Git remote without embedded credentials.")
        return value

    @staticmethod
    def _identity(task):
        try:
            identity = dumps([task["workflow_id"], task["id"], task["owner"]])
        except (KeyError, TypeError, ValueError):
            raise PlatformError("INVALID_ARGUMENT", "Git requires an application-owned task identity.") from None
        return hashlib.sha256(identity.encode()).hexdigest()[:24]

    def branch_for(self, task):
        return "maplatform/" + self._identity(task)

    def _directory(self, task):
        return self.staging_root / self._identity(task)

    async def _run(self, args, *, cwd=None, stdin=None, timeout=20, uncertain=False):
        if self.git is None:
            raise PlatformError("GIT_UNAVAILABLE", "Git is not installed on the gateway host.")
        config = ["--no-optional-locks", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                  "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
                  "-c", "core.attributesFile=/dev/null", "-c", "core.protectNTFS=true",
                  "-c", "core.protectHFS=true", "-c", "credential.helper=",
                  "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                  "-c", "protocol.ssh.allow=always", "-c", "protocol.file.allow=always"]
        env = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false",
               "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=yes",
               "GIT_AUTHOR_NAME": "Multi-Agent Platform", "GIT_AUTHOR_EMAIL": "agent@localhost",
               "GIT_COMMITTER_NAME": "Multi-Agent Platform", "GIT_COMMITTER_EMAIL": "agent@localhost"}
        try:
            proc = await asyncio.create_subprocess_exec(
                self.git, *config, *args, cwd=cwd, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
        except OSError:
            raise PlatformError("GIT_UNAVAILABLE", "Git subprocess could not be started.") from None
        total = [0]

        async def read(stream):
            chunks = []
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return b"".join(chunks).decode("utf-8", "replace")
                total[0] += len(chunk)
                if total[0] > 65_536:
                    raise PlatformError("GIT_OUTPUT_LIMIT", "Git output exceeded its byte limit.")
                chunks.append(chunk)

        async def write():
            try:
                if stdin:
                    proc.stdin.write(stdin)
                    await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

        workers = [asyncio.create_task(job) for job in
                   (write(), read(proc.stdout), read(proc.stderr), proc.wait())]
        try:
            _, out, err, code = await asyncio.wait_for(asyncio.gather(*workers), timeout)
            return code, out, err
        except BaseException as exc:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            try:
                # The parent may already have exited while an SSH child keeps
                # stdout/stderr open; terminate the whole session either way.
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # Reap with both pipes drained, including the pipe whose bounded
            # reader stopped on overflow. Waiting first can deadlock forever.
            async def drain(stream):
                while await stream.read(4096):
                    pass
            await asyncio.gather(drain(proc.stdout), drain(proc.stderr), proc.wait())
            if uncertain:
                raise PlatformError("EFFECT_UNKNOWN", "Git push was interrupted; reconcile its remote ref before further action.") from None
            if isinstance(exc, asyncio.TimeoutError):
                raise PlatformError("GIT_TIMEOUT", "Git command exceeded its time limit.") from None
            raise

    async def _required(self, args, **kwargs):
        code, out, _ = await self._run(args, **kwargs)
        if code:
            raise PlatformError("GIT_FAILED", "Controlled Git operation failed; inspect trusted repository configuration.")
        return out

    async def _ensure(self, task):
        token = self._identity(task)
        directory = self._directory(task)
        if token in self._poisoned:
            raise PlatformError("GIT_STAGE_UNSAFE", "Git stage requires operator recovery after an interrupted mutation.")
        if not directory.exists():
            try:
                await self._required(["clone", "--no-hardlinks", "--no-checkout", "--", str(self.base_repository), str(directory)])
                await self._required(["checkout", "-b", self.branch_for(task), "HEAD"], cwd=directory)
                await self._required(["remote", "remove", "origin"], cwd=directory)
            except BaseException:
                self._poisoned.add(token)
                raise
        if directory.is_symlink() or not (directory / ".git").is_dir():
            raise PlatformError("GIT_STAGE_UNSAFE", "Git stage is not an ordinary private clone.")
        branch = (await self._required(["symbolic-ref", "--short", "HEAD"], cwd=directory)).strip()
        if branch != self.branch_for(task):
            raise PlatformError("GIT_STAGE_UNSAFE", "Git stage is on an unexpected branch.")
        return directory

    def _ordinary_paths(self, directory, paths):
        for path in paths:
            current = directory
            for index, part in enumerate(path.split("/")):
                current /= part
                try:
                    info = current.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise PlatformError("UNSAFE_PATCH", "Patch cannot touch symlinks or special filesystem entries.")
                if index < len(path.split("/")) - 1 and not stat.S_ISDIR(info.st_mode):
                    raise PlatformError("UNSAFE_PATCH", "Patch parent must be an ordinary directory.")
                if index == len(path.split("/")) - 1 and not stat.S_ISREG(info.st_mode):
                    raise PlatformError("UNSAFE_PATCH", "Patch target must be an ordinary file.")

    async def _head(self, directory):
        return (await self._required(["rev-parse", "--verify", "HEAD"], cwd=directory)).strip()

    async def _guard(self, task):
        guard = task.get("_assert_active")
        if guard is not None:
            result = guard()
            if inspect.isawaitable(result):
                await result

    def _push_args(self, task, args):
        if (not isinstance(args, dict) or set(args) != {"branch", "commit_sha"} or args.get("branch") != self.branch_for(task)
                or not isinstance(args.get("commit_sha"), str) or not _SHA.fullmatch(args["commit_sha"])):
            raise PlatformError("INVALID_ARGUMENT", "Push requires this task branch and its exact full commit SHA.")

    async def execute(self, task: dict, name: str, args: dict) -> dict:
        if name not in ("git_status", "submit_patch", "git_push") or not isinstance(args, dict):
            raise PlatformError("INVALID_ARGUMENT", "Unknown controlled Git operation.")
        expected = {"git_status": set(), "submit_patch": {"patch"}, "git_push": {"branch", "commit_sha"}}[name]
        if set(args) != expected:
            raise PlatformError("INVALID_ARGUMENT", "Controlled Git arguments do not match the tool contract.")
        if name == "git_push":
            self._push_args(task, args)
            if self.remote_url is None:
                raise PlatformError("GIT_PUSH_UNAVAILABLE", "No operator-configured Git remote is enabled.")
        patch_data, paths = _parse_patch(args["patch"]) if name == "submit_patch" else (None, [])
        async with self._locks.setdefault(self._identity(task), asyncio.Lock()):
            await self._guard(task)
            if name == "git_status" and not self._directory(task).exists():
                # A read-only reply must not lazily create a clone or checkout.
                return {"branch": self.branch_for(task), "commit_sha": await self._head(self.base_repository),
                        "changes": "", "stage_created": False}
            directory = await self._ensure(task)
            if name == "git_status":
                changes = await self._required(["status", "--porcelain=v1", "--untracked-files=all"], cwd=directory)
                return {"branch": self.branch_for(task), "commit_sha": await self._head(directory),
                        "changes": changes, "stage_created": True}
            if name == "submit_patch":
                clean = await self._required(["status", "--porcelain=v1", "--untracked-files=all"], cwd=directory)
                if clean:
                    raise PlatformError("GIT_STAGE_DIRTY", "Git stage is dirty; operator recovery is required.")
                self._ordinary_paths(self.base_repository, paths)
                self._ordinary_paths(directory, paths)
                # Reject paths under Gitlinks even where the checkout is an empty directory.
                ancestors = sorted({"/".join(p.split("/")[:i]) for p in paths for i in range(1, len(p.split("/")) + 1)})
                tree = await self._required(["ls-tree", "HEAD", "--", *ancestors], cwd=directory)
                if any(line.startswith(("120000 ", "160000 ")) for line in tree.splitlines()):
                    raise PlatformError("UNSAFE_PATCH", "Patch cannot touch symlinks or submodules.")
                await self._required(["apply", "--check", "--index", "--whitespace=error", "-"], cwd=directory, stdin=patch_data)
                await self._guard(task)
                try:
                    await self._required(["apply", "--index", "--whitespace=error", "-"], cwd=directory, stdin=patch_data)
                    await self._required(["commit", "--no-verify", "--no-gpg-sign", "-m", "Apply task patch"], cwd=directory)
                except BaseException:
                    self._poisoned.add(self._identity(task))
                    raise
                return {"branch": self.branch_for(task), "commit_sha": await self._head(directory), "changed_paths": paths}
            if await self._head(directory) != args["commit_sha"]:
                raise PlatformError("GIT_COMMIT_CHANGED", "Approved commit no longer matches the task branch HEAD.")
            if await self._required(["status", "--porcelain=v1", "--untracked-files=all"], cwd=directory):
                raise PlatformError("GIT_STAGE_DIRTY", "Cannot push a dirty Git stage.")
            await self._guard(task)
            # No force, delete, arbitrary refs, credentials in args, or model-selected remote.
            code, _, _ = await self._run(["push", "--porcelain", "--no-verify", "--", self.remote_url,
                                         args["commit_sha"] + ":refs/heads/" + args["branch"]],
                                        cwd=directory, timeout=30, uncertain=True)
            if code:
                raise PlatformError("EFFECT_UNKNOWN", "Git push did not confirm success; reconcile the remote ref.")
            return {"status": "pushed", "branch": args["branch"], "commit_sha": args["commit_sha"]}

    async def reconcile_push(self, task: dict, args: dict) -> dict:
        self._push_args(task, args)
        result = {"branch": args["branch"], "commit_sha": args["commit_sha"], "matched": False}
        if self.remote_url is None:
            return {**result, "status": "unknown"}
        # Read-only remote inspection; never retries a push, including on timeout.
        try:
            code, out, _ = await self._run(["ls-remote", "--exit-code", "--refs", "--", self.remote_url,
                                           "refs/heads/" + args["branch"]], timeout=15)
        except PlatformError:
            return {**result, "status": "unknown"}
        if code == 2 and not out.strip():
            return {**result, "status": "not_pushed"}
        lines = out.splitlines()
        if code or len(lines) != 1:
            return {**result, "status": "unknown"}
        parts = lines[0].split("\t")
        if len(parts) != 2 or not _SHA.fullmatch(parts[0]) or parts[1] != "refs/heads/" + args["branch"]:
            return {**result, "status": "unknown"}
        matched = parts[0] == args["commit_sha"]
        return {**result, "matched": matched, "status": "pushed" if matched else "diverged", "remote_commit_sha": parts[0]}
