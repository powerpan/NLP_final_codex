"""Deterministic terminal walkthrough. Never contacts a model, web or Git server."""
import asyncio
import json

from .demo import tool
from .domain import dumps
from .provider import Completion


class DemoGitGateway:
    async def execute(self, task, name, args):
        task["_assert_active"]()
        return {"fixture": True, "performed": False, "message": "離線審批示範；沒有連線或推送 Git。"}


class TerminalDemoProvider:
    model = "offline-fixture"

    async def complete(self, messages, tools, *, task, purpose="AGENT", max_output_tokens=None):
        responses = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m["role"] == "tool"}
        goal = task["goal"]
        def result(summary, artifacts=()):
            return Completion({"role": "assistant", "content": dumps({"summary": summary, "artifact_ids": list(artifacts)})}, {})
        def call(*calls):
            return Completion({"role": "assistant", "content": "", "tool_calls": list(calls)}, {})
        if purpose == "BID":
            return Completion({"role": "assistant", "content": dumps({"confidence": .8, "plan": "Offline fixture"})}, {})
        if goal == "demo:research":
            if "nested" not in responses:
                return call(tool("nested", "delegate_task", {"target_agent": "reviewer", "goal": "demo:review",
                    "required_capabilities": ["review"], "acceptance": [{"kind": "nonempty"}]}))
            return result("researcher 已收到子任務 reviewer 的結果。這是父 → 子 → 孫層級的離線演示。")
        if goal == "demo:review":
            return result("reviewer：已檢查演示契約；未呼叫模型、搜尋或執行程式。")
        if goal == "demo:code":
            if "question" not in responses:
                return call(tool("question", "ask_human", {"question": "子 Agent coder 想確認示範文件的用途。請輸入一句話回答。"}))
            if "push" not in responses:
                return call(tool("push", "git_push", {"branch": "demo/approval-only", "commit_sha": "a" * 40}))
            decision = "已拒絕" if "error" in responses["push"] else "已批准（沒有真實推送）"
            return result("coder：問題已回答，審批" + decision + "。子任務結果已交回父任務。")
        if task["kind"] == "REPLY":
            return result("reviewer 的簡短諮詢回覆：REPLY 是有預算的唯讀葉節點，不會再次委派。")
        if any(word in goal.lower() for word in ("協作", "协作", "multi", "demo", "父子")):
            if "research" not in responses:
                return call(
                    tool("research", "delegate_task", {"target_agent": "researcher", "goal": "demo:research", "required_capabilities": ["research"], "acceptance": [{"kind": "nonempty"}]}),
                    tool("code", "delegate_task", {"target_agent": "coder", "goal": "demo:code", "required_capabilities": [], "acceptance": [{"kind": "nonempty"}]}),
                    tool("peer", "ask_agent", {"target_agent": "reviewer", "question": "請說明唯讀諮詢和工作委派的差別。"}))
            return result("主 Agent 已收到兩個子任務和一個諮詢回覆。researcher 委派了 reviewer；coder 在同一個介面取得答案和操作審批。所有內容皆為離線腳本，未呼叫 API、執行程式或推送 Git。你可以繼續提問，或用 /tree 和 /focus ID 查看各 Agent。")
        context = json.loads(messages[1]["content"])
        session = context.get("session", {})
        history = session.get("previous_turns", [])
        return result(f"收到：{goal}\n這是持續對話的離線示範。當前任務收到 {len(history)} 則歷史訊息。輸入「協作」可體驗並行父子任務、孫任務、提問和審批。")

    async def stream_complete(self, messages, tools, *, task, purpose="AGENT", max_output_tokens=None, on_delta=None):
        completion = await self.complete(messages, tools, task=task, purpose=purpose, max_output_tokens=max_output_tokens)
        if on_delta:
            content = completion.message.get("content", "")
            for index in range(0, len(content), 5):
                on_delta({"kind": "content", "text": content[index:index+5]})
                await asyncio.sleep(.012)
        return completion
