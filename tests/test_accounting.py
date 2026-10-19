import json
import unittest

from maplatform.accounting import request_shape, summarize_usage


class AccountingTests(unittest.TestCase):
    def test_cache_hits_are_input_not_extra_tokens(self):
        report = summarize_usage([{"usage_json": json.dumps({
            "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
            "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": 15},
        })}])
        self.assertEqual(report["reported_totals"]["total_tokens"], 120)
        self.assertTrue(report["complete"])
        self.assertEqual(report["cache"]["hit_rate_on_reported_prompt"], .8)
        self.assertEqual(report["reasoning_tokens"]["reported_total"], 15)

    def test_missing_invalid_or_inconsistent_cache_is_not_free(self):
        calls = [{"usage_json": json.dumps(u)} for u in [
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 10},
            {"prompt_tokens": True, "prompt_cache_hit_tokens": True, "prompt_cache_miss_tokens": -1},
            [],
        ]] + [{"usage_json": "broken"}]
        report = summarize_usage(calls)
        self.assertFalse(report["complete"])
        self.assertEqual(report["cache"]["calls_with_consistent_breakdown"], 0)
        self.assertIsNone(report["cache"]["hit_rate_on_reported_prompt"])
        self.assertEqual(report["cache"]["calls_reporting"]["prompt_cache_hit_tokens"], 1)
        self.assertIsNone(report["reasoning_tokens"]["reported_total"])

    def test_empty_usage_remains_unknown(self):
        report = summarize_usage([{"usage_json": "{}"}])
        self.assertIsNone(report["reported_totals"]["total_tokens"])
        self.assertIsNone(report["cache"]["reported_totals"]["prompt_cache_hit_tokens"])

    def test_request_measurements_contain_no_private_text(self):
        messages = [{"role": "user", "content": "private-user-text"},
                    {"role": "assistant", "content": "", "reasoning_content": "private-thought"}]
        tools = [{"function": {"name": "secret-tool"}}]
        before = json.dumps(messages)
        report = request_shape(messages, tools)
        self.assertEqual(report["reasoning_text_chars"], len("private-thought"))
        self.assertEqual(report["message_count"], 2)
        self.assertNotIn("private", json.dumps(report))
        self.assertNotIn("secret", json.dumps(report))
        self.assertEqual(before, json.dumps(messages))
        self.assertEqual(report["unit"], "serialized_json_characters_not_tokens")


if __name__ == "__main__":
    unittest.main()
