"""Application-owned contracts. Model output never determines actor identity."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
import uuid


class PlatformError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)

    def as_dict(self):
        return {"error": {"code": self.code, "message": self.message}}


@dataclass(frozen=True)
class Policy:
    max_calls: int = 60
    max_tasks: int = 20
    max_depth: int = 4
    max_children: int = 6
    max_retries: int = 2
    max_repairs: int = 1
    max_resumes: int = 20
    max_reply_calls: int = 3
    deadline_seconds: int = 600
    max_output_tokens: int = 8192
    max_context_chars: int = 120000
    tool_timeout: int = 20
    max_a2a: int = 12
    max_web_requests: int = 20

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if not self.max_calls or not self.max_tasks or not self.deadline_seconds:
            raise ValueError("calls, tasks and deadline must be positive")


@dataclass(frozen=True)
class AgentSpec:
    id: str
    role: str
    capabilities: tuple[str, ...]
    tools: tuple[str, ...]


MEMORY = ("search_memory", "remember")
WEB_EVIDENCE = ("read_web_result",)
COORDINATION = ("delegate_task", "ask_agent", "ask_human", "notify_agent")
FILES = ("list_files", "read_file", "write_file", "publish_artifact", "import_artifact")
AGENTS = {
    "planner": AgentSpec("planner", "Plan bounded work, negotiate assignments, ask for clarification, integrate authorized child artifacts.",
                         ("planning", "coordination"), MEMORY + COORDINATION + WEB_EVIDENCE),
    "researcher": AgentSpec("researcher", "Investigate source-backed questions; publish findings with explicit sources and uncertainties.",
                            ("research", "writing"), MEMORY + COORDINATION + FILES + WEB_EVIDENCE + ("search_web", "fetch_web")),
    "coder": AgentSpec("coder", "Implement and test code in your private sandbox; publish files explicitly for collaborators.",
                       ("coding", "testing"), MEMORY + COORDINATION + FILES + WEB_EVIDENCE + ("run_code", "run_tests", "git_status", "submit_patch", "git_push")),
    "reviewer": AgentSpec("reviewer", "Independently review evidence and code; report specific issues. Never infer a test passed from a claim.",
                          ("review", "testing", "writing"), MEMORY + COORDINATION + FILES + WEB_EVIDENCE + ("run_tests",)),
}

TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"})


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"
