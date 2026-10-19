from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.audit_token_usage import audit_database, measured_request, measured_summary, snapshot_analysis, usage_summary


class TokenUsageAuditTests(unittest.TestCase):
    def test_cache_ratios_ignore_inconsistent_and_malformed_usage(self):
        result = usage_summary([
            {"prompt_tokens": 100, "prompt_cache_hit_tokens": 101},
            {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 40},
            {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": -1},
            {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": "20"},
            {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20},
            {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 70}},
        ])
        self.assertEqual(result["cache_hit_fraction_calls"], 2)
        self.assertEqual(result["cache_hit_fraction_of_reported_prompt"], .75)

    def test_measurements_are_whitelisted_and_missing_is_not_inferred(self):
        payload = {"call_id": "call_1", "unit": "serialized_json_characters_not_tokens",
                   "request_chars": 120, "messages_chars": 90, "tools_chars": 5,
                   "message_chars_by_role": {"system": 50, "user": 30},
                   "reasoning_text_chars": 0, "tool_calls_chars": 0,
                   "message_count": 2, "tool_schema_count": 0}
        measurement = measured_request(payload)
        self.assertEqual(measurement["request_chars"], 120)
        summary = measured_summary([measurement, None])
        self.assertEqual(summary["call_count"], 2)
        self.assertEqual(summary["measured_calls"], 1)
        self.assertEqual(summary["sum_reported"]["request_chars"], 120)
        self.assertIsNone(measured_request(payload | {"reasoning": "PRIVATE_REASONING"}))
        self.assertIsNone(measured_request(payload | {"message_chars_by_role": {"PRIVATE_SECRET": 8}}))
        self.assertIsNone(measured_request(payload | {"request_chars": True}))

    def test_usage_missing_is_unknown_and_reasoning_is_completion_subset(self):
        result = usage_summary([{"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                                 "prompt_tokens_details": {"cached_tokens": 80},
                                 "completion_tokens_details": {"reasoning_tokens": 10}}, {}])
        self.assertEqual(result["calls"], 2)
        self.assertEqual(result["metrics"]["total_tokens"], {"sum_reported": 120, "reported_calls": 1})
        self.assertEqual(result["metrics"]["reasoning_tokens"]["sum_reported"], 10)
        self.assertEqual(result["metrics"]["prompt_cache_miss_tokens"]["reported_calls"], 0)
        self.assertEqual(result["cache_hit_fraction_of_reported_prompt"], .8)

    def test_replay_counts_json_characters_without_exporting_payload(self):
        messages = [{"role": "system", "content": "PRIVATE_RULE"},
                    {"role": "assistant", "reasoning_content": "PRIVATE_REASONING", "content": "",
                     "tool_calls": [{"id": "1", "function": {"name": "search_web", "arguments": '{"query":"PRIVATE_QUERY"}'}}]},
                    {"role": "tool", "tool_call_id": "1", "content": "PRIVATE_RESULT"}]
        result = snapshot_analysis(messages, [1, 3])
        self.assertEqual(result["replayed_json_chars_after_first_inclusion"]["system_content"], len('"PRIVATE_RULE"'))
        self.assertEqual(result["replayed_json_chars_after_first_inclusion"].get("assistant_reasoning_content", 0), 0)
        self.assertGreater(result["cumulative_json_chars"]["tool_result:search_web"], 0)
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_database_filter_readonly_and_does_not_export_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "saved.sqlite")
            with closing(sqlite3.connect(path)) as db:
                db.executescript("""
                    CREATE TABLE tasks(id,workflow_id,owner,kind,status,depth);
                    CREATE TABLE llm_calls(id,workflow_id,task_id,purpose,status,created,usage_json);
                    CREATE TABLE events(id,workflow_id,task_id,type,payload_json);
                    CREATE TABLE snapshots(task_id,messages_json);
                    CREATE TABLE operations(tool_name,status,args_hash,workflow_id);
                """)
                db.executemany("INSERT INTO tasks VALUES(?,?,?,?,?,?)", [("task_1", "wf_1", "researcher", "WORK", "COMPLETED", 0), ("task_2", "wf_2", "planner", "WORK", "COMPLETED", 0)])
                db.executemany("INSERT INTO llm_calls VALUES(?,?,?,?,?,?,?)", [("call_1", "wf_1", "task_1", "AGENT", "SUCCEEDED", 1, '{"prompt_tokens":10}'), ("call_2", "wf_2", "task_2", "AGENT", "SUCCEEDED", 2, '{"prompt_tokens":99}')])
                measurement = {"call_id": "call_1", "unit": "serialized_json_characters_not_tokens",
                               "request_chars": 80, "messages_chars": 55, "tools_chars": 2,
                               "message_chars_by_role": {"user": 53}, "reasoning_text_chars": 0,
                               "tool_calls_chars": 0, "message_count": 1, "tool_schema_count": 0}
                db.executemany("INSERT INTO events VALUES(?,?,?,?,?)", [(1, "wf_1", "task_1", "SnapshotSaved", '{"messages":1}'), (2, "wf_1", "task_1", "ModelCallReserved", '{"call_id":"call_1"}'), (3, "wf_1", "task_1", "ModelRequestMeasured", json.dumps(measurement))])
                db.execute("INSERT INTO snapshots VALUES(?,?)", ("task_1", '[{"role":"user","content":"PRIVATE_SECRET"}]'))
                db.commit()
            before = path.read_bytes()
            report = audit_database(path, "test", "wf_1")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(report["usage"]["metrics"]["prompt_tokens"]["sum_reported"], 10)
            self.assertEqual(report["tasks"][0]["history"]["calls_with_inferred_prefix"], 1)
            self.assertEqual(report["measured_requests"]["measured_calls"], 1)
            self.assertEqual(report["tasks"][0]["measured_requests"]["sum_reported"]["request_chars"], 80)
            self.assertEqual(report["calls"][0]["measured_request"]["request_chars"], 80)
            self.assertNotIn("PRIVATE_SECRET", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
