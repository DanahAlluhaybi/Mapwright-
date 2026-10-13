import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from mapwright.llm import LLMError
from mapwright.llm.fake import FakeProvider
from mapwright.llmplan import LLMSession
from mapwright.server import Settings, make_server

ROOT = Path(__file__).resolve().parents[1]
D05 = ROOT / "benchmark" / "datasets" / "D05" / "source.csv"  # has one held deduplication
EXAMPLE = ROOT / "examples" / "messy_customers.csv"
os.environ.setdefault("MAPWRIGHT_API_QUIET", "1")  # keep request logs out of test output


class Client:
    def __init__(self, base: str, token: str | None = None):
        self.base = base
        self.token = token

    def call(self, method: str, path: str, body: bytes | None = None, content_type: str | None = None):
        request = urllib.request.Request(self.base + path, data=body, method=method)
        if content_type:
            request.add_header("Content-Type", content_type)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read(), response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers.get("Content-Type", "")

    def json(self, method: str, path: str, data: dict | None = None):
        body = json.dumps(data).encode("utf-8") if data is not None else None
        status, raw, _ = self.call(method, path, body, "application/json" if body else None)
        return status, json.loads(raw)

    def upload(self, path: Path, query: str):
        status, raw, _ = self.call("POST", f"/runs?{query}", path.read_bytes(), "text/csv")
        return status, json.loads(raw)


class ServerTest(unittest.TestCase):
    token = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            runs_dir=Path(self.tmp.name) / "runs",
            contracts_dir=ROOT / "contracts",
            data_root=ROOT,
            token=self.token,
        )
        self.server = make_server(self.settings, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(f"http://127.0.0.1:{self.server.server_address[1]}", self.token)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()


class RunAndReviewTest(ServerTest):
    def test_health_and_contracts(self):
        self.assertEqual(self.client.json("GET", "/health"), (200, {"ok": True}))
        status, data = self.client.json("GET", "/contracts")
        self.assertEqual(status, 200)
        self.assertEqual(data["contracts"], ["customer", "sales_order"])

    def test_upload_hold_then_approve(self):
        status, run = self.client.upload(D05, "contract=customer&filename=D05.csv")
        self.assertEqual(status, 201, run)
        self.assertEqual(run["status"], "awaiting_review")
        self.assertEqual(run["filename"], "D05.csv")
        self.assertEqual(len(run["held_ids"]), 1)
        held = run["held_actions"][0]
        self.assertGreater(held["rows_removed"], 0)
        self.assertIn(held["id"], run["review_text"])

        status, done = self.client.json(
            "POST", f"/runs/{run['id']}/review", {"decision": "approve", "approve": "all", "reviewer": "Danah"}
        )
        self.assertEqual(status, 200, done)
        self.assertEqual(done["status"], "approved")
        self.assertEqual(done["review"]["approved"], run["held_ids"])
        self.assertEqual(done["rows"], run["rows"] - held["rows_removed"])
        self.assertEqual(done["rows_before_review"], run["rows"])

        _, csv_body, content_type = self.client.call("GET", f"/runs/{run['id']}/target.csv")
        self.assertTrue(content_type.startswith("text/csv"))
        self.assertEqual(len(csv_body.decode("utf-8").splitlines()) - 1, done["rows"])

        _, audit, _ = self.client.call("GET", f"/runs/{run['id']}/audit")
        last = json.loads(audit.decode("utf-8").splitlines()[-1])
        self.assertEqual(last["event"], "review")
        self.assertEqual(last["actor"], "reviewer:Danah")
        self.assertEqual(last["output_rows"], done["rows"])

    def test_approving_nothing_keeps_the_rows(self):
        _, run = self.client.upload(D05, "contract=customer")
        status, done = self.client.json(
            "POST", f"/runs/{run['id']}/review", {"decision": "approve", "approve": [], "reviewer": "Sara"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(done["rows"], run["rows"])

    def test_reject_writes_no_output_and_cannot_be_reviewed_twice(self):
        _, run = self.client.upload(D05, "contract=customer")
        status, done = self.client.json(
            "POST", f"/runs/{run['id']}/review", {"decision": "reject", "reviewer": "Sara", "note": "wrong file"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(done["status"], "rejected")
        self.assertFalse((self.settings.runs_dir / run["id"] / "approved.csv").exists())
        status, error = self.client.json("POST", f"/runs/{run['id']}/review", {"decision": "approve", "reviewer": "x"})
        self.assertEqual(status, 409)
        self.assertIn("already reviewed", error["error"])

    def test_review_checks_ids_and_reviewer(self):
        _, run = self.client.upload(D05, "contract=customer")
        path = f"/runs/{run['id']}/review"
        status, error = self.client.json("POST", path, {"decision": "approve", "approve": "a999", "reviewer": "S"})
        self.assertEqual(status, 400)
        self.assertIn("a999", error["error"])
        status, error = self.client.json("POST", path, {"decision": "approve", "approve": "all"})
        self.assertEqual(status, 400)
        self.assertIn("reviewer", error["error"])
        status, _ = self.client.json("POST", path, {"decision": "maybe", "reviewer": "S"})
        self.assertEqual(status, 400)
        # comma-separated ids, as a form text field sends them
        status, done = self.client.json(
            "POST", path, {"decision": "approve", "approve": ", ".join(run["held_ids"]), "reviewer": "S"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(done["review"]["approved"], run["held_ids"])

    def test_local_file_run_and_listing(self):
        status, run = self.client.json(
            "POST", "/runs", {"source_path": "examples/messy_customers.csv", "contract": "customer"}
        )
        self.assertEqual(status, 201, run)
        self.assertEqual(run["filename"], "messy_customers.csv")
        status, listing = self.client.json("GET", "/runs")
        self.assertEqual([r["id"] for r in listing["runs"]], [run["id"]])
        status, report, content_type = self.client.call("GET", f"/runs/{run['id']}/report")
        self.assertEqual(status, 200)
        self.assertTrue(report.startswith(b"# Run report"))

    def test_bad_requests(self):
        cases = [
            (self.client.upload(EXAMPLE, "contract=nope"), 400),
            (self.client.upload(EXAMPLE, "contract=../contracts/customer"), 400),
            (self.client.upload(EXAMPLE, "contract=customer&system=magic"), 400),
            (self.client.json("POST", "/runs", {"source_path": "/etc/passwd", "contract": "customer"}), 400),
            (self.client.json("POST", "/runs", {"source_path": "../outside.csv", "contract": "customer"}), 400),
            (self.client.json("GET", "/runs/20260101-000000-abcdef"), 404),
            (self.client.json("GET", "/runs/..%2F..%2Fetc"), 404),
            (self.client.json("GET", "/nothing"), 404),
        ]
        for (status, data), expected in cases:
            self.assertEqual(status, expected, data)
            self.assertIn("error", data)
        status, raw, _ = self.client.call("POST", "/runs?contract=customer", b"", "text/csv")
        self.assertEqual(status, 400)


class ModelRunTest(ServerTest):
    def test_hybrid_uses_the_session_factory(self):
        def answers(request):
            body = json.loads(request.prompt)
            if request.prompt_version.startswith("map"):
                return {"mappings": [], "drop": []}
            if request.prompt_version.startswith("values"):
                return {"fields": {e["field"]["name"]: {"actions": []} for e in body["fields"]}}
            return {"actions": []}

        self.settings.session_factory = lambda system: LLMSession(FakeProvider(answers))
        status, run = self.client.upload(EXAMPLE, "contract=customer&system=hybrid")
        self.assertEqual(status, 201, run)
        self.assertEqual(run["system"], "hybrid")
        self.assertGreater(run["usage"]["llm_calls"], 0)

    def test_missing_key_is_a_clear_error_and_leaves_no_run(self):
        def no_key(system):
            raise LLMError("GEMINI_API_KEY is not set")

        self.settings.session_factory = no_key
        status, error = self.client.upload(EXAMPLE, "contract=customer&system=hybrid")
        self.assertEqual(status, 400)
        self.assertIn("GEMINI_API_KEY", error["error"])
        self.assertEqual(self.client.json("GET", "/runs")[1]["runs"], [])


class TokenTest(ServerTest):
    token = "s3cret"

    def test_token_is_required(self):
        self.assertEqual(self.client.json("GET", "/health")[0], 200)
        anonymous = Client(self.client.base)
        status, data = anonymous.json("GET", "/health")
        self.assertEqual(status, 401)
        wrong = Client(self.client.base, "nope")
        self.assertEqual(wrong.json("GET", "/health")[0], 401)


if __name__ == "__main__":
    unittest.main()
