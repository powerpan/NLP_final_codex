"""Two-request opt-in live check of JSON mode with native tools and SSE."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.live_collaboration_check import read_key
from maplatform.domain import PlatformError, dumps
from maplatform.evaluation import _token_usage
from maplatform.provider import DeepSeekProvider
from maplatform.runtime import function, parse_final


async def run(path):
    report = {"status": "RUNNING", "started_at": datetime.now(timezone.utc).isoformat(), "usage": [],
              "scope": "Two real SSE calls; local echo only, no web/Docker/external action.",
              "provider_sha256": hashlib.sha256(Path("maplatform/provider.py").read_bytes()).hexdigest()}
    def save():
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    save()
    try:
        provider = DeepSeekProvider(read_key(), "deepseek-flash", reasoning_effort="low", max_output_tokens=8192)
        expected = '第一行\nSecond line "quoted"'
        messages = [{"role": "system", "content": 'Call echo once with the exact supplied text, then return one valid JSON object only: {"summary":"the echoed text unchanged","artifact_ids":[]}. No other keys or prose.'},
                    {"role": "user", "content": dumps({"text": expected})}]
        tools = [function("echo", "Echo text unchanged.", {"text": {"type": "string"}}, ("text",))]
        first = await provider.stream_complete(messages, tools, task={}, purpose="AGENT")
        report["usage"].append(first.usage)
        calls = first.message.get("tool_calls", [])
        if len(calls) != 1 or calls[0]["function"]["name"] != "echo" or json.loads(calls[0]["function"]["arguments"]) != {"text": expected}:
            raise PlatformError("JSON_PROBE_FAILED", "Expected one valid echo tool call.")
        messages.extend([first.message, {"role": "tool", "tool_call_id": calls[0]["id"], "content": dumps({"text": expected})}])
        second = await provider.stream_complete(messages, tools, task={}, purpose="AGENT")
        report["usage"].append(second.usage)
        if second.message.get("tool_calls") or parse_final(second.message.get("content")) != {"summary": expected, "artifact_ids": []}:
            raise PlatformError("JSON_PROBE_FAILED", "Final JSON did not preserve the test text.")
        report.update(status="PASS", native_tool_roundtrip=True, json_unicode_newline_quotes=True)
    except BaseException as exc:
        report.update(status="FAIL", error=exc.as_dict() if isinstance(exc, PlatformError) else {"type": type(exc).__name__})
        if getattr(exc, "usage", None):
            report["usage"].append(exc.usage)
        raise
    finally:
        report["ended_at"] = datetime.now(timezone.utc).isoformat()
        report["token_usage"] = _token_usage([{"usage_json": dumps(u)} for u in report["usage"]])
        save()
        print(dumps({"status": report["status"], "report": str(path), "token_usage": report["token_usage"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    directory = Path(args.run_dir).resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    asyncio.run(run(directory / "report.json"))
