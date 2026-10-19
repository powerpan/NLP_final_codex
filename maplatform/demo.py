"""Offline protocol fixture; it does not measure model intelligence or execute code."""
import json
from .provider import Completion
from .domain import dumps


def tool(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": dumps(arguments)}}


class DemoProvider:
    async def complete(self, messages, tools, *, task, purpose="AGENT", max_output_tokens=None):
        if purpose == "BID":
            name = messages[0]["content"]
            return Completion({"role": "assistant", "content": dumps({"confidence": 0.9 if "coder" in name else 0.6,
                                                                         "plan": "Produce and publish a small UTF-8 source fixture."})}, {})
        responses = {}
        for message in messages:
            if message["role"] == "tool":
                responses[message["tool_call_id"]] = json.loads(message["content"])
        calls = []
        if task["owner"] == "planner":
            if "delegate" not in responses:
                calls = [tool("delegate", "delegate_task", {"goal": "Create an add(a,b) source fixture; offline file operations only.",
                          "required_capabilities": ["writing"], "acceptance": [{"kind": "artifact", "name": "add.py"}]}),
                         tool("consult", "ask_agent", {"target_agent": "reviewer", "question": "What is the boundary condition for adding negative numbers?"}),
                         tool("human", "ask_human", {"question": "Use a simple add(a,b) example? Reply yes to continue the fixture."})]
            else:
                child = responses["delegate"]
                # Resolve diverse result envelopes without substituting invented artifact IDs.
                def find_ids(value):
                    if isinstance(value, dict):
                        if isinstance(value.get("artifact_ids"), list):
                            return value["artifact_ids"]
                        for v in value.values():
                            ids = find_ids(v)
                            if ids:
                                return ids
                    return []
                artifacts = find_ids(child)
                if not artifacts:
                    return Completion({"role": "assistant", "content": dumps({"summary": "Fixture child failed; inspect its trace.", "artifact_ids": []}), "reasoning_content": ""}, {})
                return Completion({"role": "assistant", "content": dumps({"summary": "Offline collaboration fixture complete. add.py was exchanged; no code was executed and no real model was called.", "artifact_ids": artifacts}), "reasoning_content": ""}, {})
        elif task["kind"] == "REPLY":
            return Completion({"role": "assistant", "content": dumps({"summary": "Include negative inputs and zero in actual acceptance tests.", "artifact_ids": []}), "reasoning_content": ""}, {})
        else:
            if "write" not in responses:
                calls = [tool("write", "write_file", {"path": "add.py", "content": "def add(a, b):\n    return a + b\n"})]
            elif "publish" not in responses:
                calls = [tool("publish", "publish_artifact", {"path": "add.py", "name": "add.py", "grantees": ["planner"]})]
            else:
                aid = responses["publish"].get("artifact_id")
                return Completion({"role": "assistant", "content": dumps({"summary": "Created a file fixture; execution is not part of this offline demo.", "artifact_ids": [aid] if aid else []}), "reasoning_content": ""}, {})
        return Completion({"role": "assistant", "content": "", "reasoning_content": "", "tool_calls": calls}, {})
