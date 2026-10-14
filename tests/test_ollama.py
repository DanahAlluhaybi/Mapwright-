import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from mapwright.llm import LLMError, LLMRequest, make_provider
from mapwright.llm.gemini import GeminiProvider
from mapwright.llm.ollama import OllamaProvider, build_body, installed_models
from mapwright.server import Service, Settings

ROOT = Path(__file__).resolve().parents[1]


def model_answer(prompt: str, version: str) -> dict:
    """Valid, empty answers for the pipeline's prompts."""
    body = json.loads(prompt)
    if version.startswith("map"):
        return {"mappings": [], "drop": []}
    if version.startswith("values"):
        return {"fields": {e["field"]["name"]: {"actions": []} for e in body["fields"]}}
    return {"actions": []}


class FakeOllama(BaseHTTPRequestHandler):
    models = ["qwen2.5:7b"]
    requests: list = []

    def log_message(self, *args):
        pass

    def _send(self, status, data):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/api/tags":
            return self._send(200, {"models": [{"name": m} for m in self.models]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.requests.append(body)
        if body["model"] not in self.models:
            return self._send(404, {"error": f"model '{body['model']}' not found"})
        prompt = body["messages"][-1]["content"]
        try:
            answer = model_answer(prompt, "values" if '"fields"' in prompt else "map")
        except (json.JSONDecodeError, KeyError, TypeError):
            answer = {"ok": True}
        self._send(200, {"message": {"content": json.dumps(answer)}, "prompt_eval_count": 12, "eval_count": 5})


class OllamaTest(unittest.TestCase):
    def setUp(self):
        FakeOllama.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.env = mock.patch.dict(os.environ, {"OLLAMA_HOST": f"127.0.0.1:{self.server.server_address[1]}"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()

    def test_routing_by_name(self):
        self.assertIsInstance(make_provider("ollama/qwen2.5:7b"), OllamaProvider)
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test"}):
            self.assertIsInstance(make_provider("gemini-3.8-flash"), GeminiProvider)

    def test_request_shape(self):
        body = build_body(LLMRequest(prompt="p", system="s", sample=2), "qwen2.5:7b")
        self.assertEqual(body["messages"], [{"role": "system", "content": "s"}, {"role": "user", "content": "p"}])
        self.assertEqual(body["format"], "json")
        self.assertEqual(body["options"], {"temperature": 0.0, "seed": 2})
        self.assertFalse(body["stream"])

    def test_answer_and_tokens(self):
        provider = OllamaProvider("ollama/qwen2.5:7b")
        with mock.patch("builtins.print"):
            response = provider.complete(LLMRequest(prompt="hi", model=provider.model))
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual((response.input_tokens, response.output_tokens), (12, 5))
        self.assertEqual(response.model, "ollama/qwen2.5:7b")
        self.assertEqual(FakeOllama.requests[-1]["model"], "qwen2.5:7b")

    def test_missing_model_says_how_to_pull_it(self):
        with mock.patch("builtins.print"), self.assertRaisesRegex(LLMError, "ollama pull llama3.2"):
            OllamaProvider("ollama/llama3.2").complete(LLMRequest(prompt="hi", model="ollama/llama3.2"))

    def test_not_running(self):
        self.server.shutdown()
        self.server.server_close()
        self.assertEqual(installed_models(), [])
        with mock.patch("builtins.print"), self.assertRaisesRegex(LLMError, "not running"):
            OllamaProvider().complete(LLMRequest(prompt="hi", model="ollama/qwen2.5:7b"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)  # tearDown shuts it down
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def test_page_lists_local_models_and_runs_with_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = Service(
                Settings(runs_dir=Path(tmp) / "runs", cache_dir=Path(tmp) / "cache", contracts_dir=ROOT / "contracts")
            )
            self.assertEqual(service.models()["local"], ["ollama/qwen2.5:7b"])
            data = (ROOT / "examples" / "messy_customers.csv").read_bytes()
            with mock.patch("builtins.print"):
                run = service.start(data, "customer", "hybrid", "messy.csv", model="ollama/qwen2.5:7b")
            self.assertEqual(run["model"], "ollama/qwen2.5:7b")
            self.assertGreater(run["usage"]["llm_calls"], 0)
            self.assertTrue(any(Path(tmp, "cache").glob("*.json")))  # answers are cached like Gemini's


if __name__ == "__main__":
    unittest.main()
