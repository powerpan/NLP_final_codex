"""Known accounting survives provider failures without retaining extensions."""
import asyncio
import copy
import json
import unittest

from maplatform.domain import PlatformError
from maplatform.provider import CompletionError, DeepSeekProvider


MESSAGES = [{"role": "user", "content": "Return JSON."}]
USAGE = {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22,
         "prompt_cache_hit_tokens": 12, "prompt_cache_miss_tokens": 5,
         "completion_tokens_details": {"reasoning_tokens": 3}}
DONE = b"data: [DONE]\n\n"


def frame(usage, *, finish="stop"):
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "{}"},
                                               "finish_reason": finish}], "usage": usage}) + "\n\n").encode()


def buffered_provider(usage, *, finish="stop", choices=None):
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "{}"},
                                    "finish_reason": finish}] if choices is None else choices,
                       "usage": usage}).encode()
    return DeepSeekProvider("fixture-key", "fixture", transport=lambda *args: (200, body))


def streaming_provider(chunks):
    async def transport(*args):
        for chunk in chunks:
            yield 200, chunk
    return DeepSeekProvider("fixture-key", "fixture", stream_transport=transport)


class ProviderUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_cache_and_reasoning_counters_survive_both_transports(self):
        usage = copy.deepcopy(USAGE)
        usage["untrusted_extension"] = {"private": "must-not-persist"}
        usage["completion_tokens_details"]["untrusted_extension"] = "must-not-persist"
        for provider, method in ((buffered_provider(usage), "complete"),
                                 (streaming_provider([frame(usage), DONE]), "stream_complete")):
            with self.subTest(method=method):
                result = await getattr(provider, method)(MESSAGES, [], task={})
                self.assertEqual(result.usage, USAGE)
                self.assertNotIn("must-not-persist", str(result.usage))
        self.assertIn("untrusted_extension", usage["completion_tokens_details"])

    async def test_missing_optional_usage_stays_unknown_and_zero_is_preserved(self):
        for usage in ({"total_tokens": 22}, {}, {"prompt_cache_hit_tokens": 0},
                      {"completion_tokens_details": {"reasoning_tokens": 0}}):
            for provider, method in ((buffered_provider(usage), "complete"),
                                     (streaming_provider([frame(usage), DONE]), "stream_complete")):
                with self.subTest(usage=usage, method=method):
                    result = await getattr(provider, method)(MESSAGES, [], task={})
                    self.assertEqual(result.usage, usage)

    async def test_optional_counters_reject_non_integer_negative_or_boolean_values(self):
        for name in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "reasoning_tokens"):
            for invalid in (True, -1, 1.5, "12", None):
                usage = copy.deepcopy(USAGE)
                if name == "reasoning_tokens":
                    usage["completion_tokens_details"][name] = invalid
                else:
                    usage[name] = invalid
                for provider, method in ((buffered_provider(usage), "complete"),
                                         (streaming_provider([frame(usage), DONE]), "stream_complete")):
                    with self.subTest(name=name, invalid=invalid, method=method):
                        with self.assertRaises(PlatformError) as caught:
                            await getattr(provider, method)(MESSAGES, [], task={})
                        self.assertEqual(caught.exception.code, "PROVIDER_PROTOCOL")

    async def test_malformed_known_details_rejected_without_persisting_payload(self):
        for details in (None, [], "private-data"):
            usage = {"completion_tokens_details": details}
            for provider, method in ((buffered_provider(usage), "complete"),
                                     (streaming_provider([frame(usage), DONE]), "stream_complete")):
                with self.subTest(details=details, method=method):
                    with self.assertRaises(PlatformError) as caught:
                        await getattr(provider, method)(MESSAGES, [], task={})
                    self.assertEqual(caught.exception.code, "PROVIDER_PROTOCOL")
                    self.assertNotIn("private-data", str(caught.exception))

    async def test_length_and_protocol_failures_preserve_validated_counters(self):
        cases = ((buffered_provider(USAGE, finish="length"), "complete", "PROVIDER_TRUNCATED"),
                 (buffered_provider(USAGE, choices=[]), "complete", "PROVIDER_PROTOCOL"),
                 (streaming_provider([frame(USAGE, finish="length"), DONE]), "stream_complete", "PROVIDER_TRUNCATED"),
                 (streaming_provider([frame(USAGE)]), "stream_complete", "PROVIDER_TRUNCATED"))
        for provider, method, code in cases:
            with self.subTest(method=method, code=code):
                with self.assertRaises(CompletionError) as caught:
                    await getattr(provider, method)(MESSAGES, [], task={})
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.usage, USAGE)

    async def test_stream_partial_usage_merges_without_inventing_missing_counters(self):
        first = frame({"prompt_tokens": 17, "prompt_cache_hit_tokens": 12}, finish=None)
        last = ("data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                      "usage": {"prompt_cache_miss_tokens": 5}}) + "\n\n").encode()
        result = await streaming_provider([first, last, DONE]).stream_complete(MESSAGES, [], task={})
        self.assertEqual(result.usage, {"prompt_tokens": 17, "prompt_cache_hit_tokens": 12,
                                       "prompt_cache_miss_tokens": 5})

    async def test_stream_transport_timeout_and_cancellation_keep_known_counters(self):
        for cancelled in (False, True):
            async def transport(*args):
                yield 200, frame(USAGE, finish=None)
                if cancelled:
                    raise asyncio.CancelledError()
                raise TimeoutError()
            provider = DeepSeekProvider("fixture-key", "fixture", stream_transport=transport)
            with self.subTest(cancelled=cancelled):
                with self.assertRaises(asyncio.CancelledError if cancelled else CompletionError) as caught:
                    await provider.stream_complete(MESSAGES, [], task={})
                self.assertEqual(caught.exception.usage, USAGE)

    def test_completion_error_never_retains_unvalidated_counters_or_extensions(self):
        usage = {**USAGE, "prompt_cache_hit_tokens": True, "prompt_cache_miss_tokens": -1,
                 "extension": "private-data", "completion_tokens_details": {"reasoning_tokens": "private-data"}}
        error = CompletionError("FIXTURE", "Safe diagnostic", usage)
        self.assertEqual(error.usage, {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22})
        self.assertNotIn("private-data", str(error.usage))


if __name__ == "__main__":
    unittest.main()
