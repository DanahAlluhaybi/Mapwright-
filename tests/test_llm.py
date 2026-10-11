import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

from mapwright.llm import CachedProvider, CacheMiss, LLMError, LLMRequest, LLMResponse, Usage, gemini
from mapwright.llm.fake import FakeProvider

REQUEST = LLMRequest(prompt="map these columns", system="you map columns", model="fake-model", prompt_version="map-v1")


class RequestTest(unittest.TestCase):
    def test_key_is_stable_and_covers_everything_that_changes_the_answer(self):
        self.assertEqual(REQUEST.key(), LLMRequest(**REQUEST.to_dict()).key())
        for change in [{"prompt": "x"}, {"system": "x"}, {"model": "other"}, {"prompt_version": "map-v2"}]:
            with self.subTest(change=change):
                self.assertNotEqual(REQUEST.key(), LLMRequest(**{**REQUEST.to_dict(), **change}).key())

    def test_response_json(self):
        self.assertEqual(LLMResponse('{"a": 1}', "m").json(), {"a": 1})
        with self.assertRaises(LLMError):
            LLMResponse("not json", "m").json()


class CacheTest(unittest.TestCase):
    def test_record_then_replay_without_the_provider(self):
        fake = FakeProvider(lambda r: {"target": "email"})
        with tempfile.TemporaryDirectory() as tmp:
            first = CachedProvider(tmp, fake).complete(REQUEST)
            again = CachedProvider(tmp, fake).complete(REQUEST)
            replayed = CachedProvider(tmp, model="fake-model").complete(REQUEST)
        self.assertEqual(len(fake.requests), 1)
        self.assertFalse(first.cached)
        self.assertTrue(again.cached and replayed.cached)
        self.assertEqual(replayed.json(), {"target": "email"})

    def test_replay_miss_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CacheMiss):
                CachedProvider(tmp, model="fake-model").complete(REQUEST)

    def test_cached_requests_must_name_a_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(LLMError):
                CachedProvider(tmp, FakeProvider(lambda r: {})).complete(LLMRequest(prompt="x"))


class UsageTest(unittest.TestCase):
    def test_totals(self):
        usage = Usage()
        usage.record(REQUEST, LLMResponse("{}", "m", 100, 20))
        usage.record(REQUEST, LLMResponse("{}", "m", 50, 10, cached=True))
        self.assertEqual(usage.summary(), {"llm_calls": 2, "input_tokens": 150, "output_tokens": 30})
        self.assertEqual(usage.cached_calls, 1)


class GeminiTest(unittest.TestCase):
    def test_body(self):
        body = gemini.build_body(REQUEST, "gemini-2.5-flash")
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "map these columns")
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "you map columns")
        self.assertEqual(body["generationConfig"], {"temperature": 0.0, "responseMimeType": "application/json"})

    def test_gemini_3_gets_no_sampling_parameters_and_low_thinking(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            body = gemini.build_body(REQUEST, "gemini-3.8-flash")
        self.assertEqual(
            body["generationConfig"],
            {"thinkingConfig": {"thinkingLevel": "low"}, "responseMimeType": "application/json"},
        )
        with mock.patch.dict(os.environ, {"MAPWRIGHT_THINKING_LEVEL": "default"}, clear=True):
            self.assertNotIn("thinkingConfig", gemini.build_body(REQUEST, "gemini-3.8-flash")["generationConfig"])

    def test_parse_response(self):
        data = {
            "candidates": [{"content": {"parts": [{"text": '{"ok": '}, {"text": "true}"}]}}],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 4},
            "modelVersion": "gemini-2.5-flash",
        }
        response = gemini.parse_response(data, "gemini-2.5-flash")
        self.assertEqual((response.json(), response.input_tokens, response.output_tokens), ({"ok": True}, 12, 4))

    def test_blocked_response(self):
        with self.assertRaises(LLMError):
            gemini.parse_response({"promptFeedback": {"blockReason": "SAFETY"}}, "m")

    def test_missing_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(LLMError):
                gemini.GeminiProvider()

    def test_key_goes_in_a_header_and_rate_limits_are_retried(self):
        reply = io.BytesIO(json.dumps({"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}).encode())
        limited = urllib.error.HTTPError("url", 429, "Too Many Requests", {}, io.BytesIO(b"{}"))
        calls = []

        def fake_urlopen(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                raise limited
            return mock.MagicMock(__enter__=lambda s: reply, __exit__=lambda *a: False)

        with (
            mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=True),
            mock.patch("urllib.request.urlopen", fake_urlopen),
            mock.patch("time.sleep"),
        ):
            gemini.GeminiProvider("gemini-2.5-flash").complete(REQUEST)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1].get_header("X-goog-api-key"), "test-key")
        self.assertNotIn("test-key", calls[-1].full_url)

    def test_read_timeouts_are_retried(self):
        reply = io.BytesIO(json.dumps({"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}).encode())
        calls = []

        def fake_urlopen(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                raise TimeoutError("The read operation timed out")
            return mock.MagicMock(__enter__=lambda s: reply, __exit__=lambda *a: False)

        with (
            mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=True),
            mock.patch("urllib.request.urlopen", fake_urlopen),
            mock.patch("time.sleep"),
        ):
            gemini.GeminiProvider("gemini-3.8-flash").complete(REQUEST)
        self.assertEqual(len(calls), 2)

    def test_errors_never_contain_the_key(self):
        denied = urllib.error.HTTPError(
            "url", 401, "Unauthorized", {}, io.BytesIO(json.dumps({"error": {"message": "API key not valid"}}).encode())
        )
        with (
            mock.patch.dict(os.environ, {"GEMINI_API_KEY": "secret-key"}, clear=True),
            mock.patch("urllib.request.urlopen", side_effect=denied),
        ):
            with self.assertRaises(LLMError) as raised:
                gemini.GeminiProvider("gemini-2.5-flash").complete(REQUEST)
        self.assertIn("401", str(raised.exception))
        self.assertNotIn("secret-key", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
