import csv
import importlib.util
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
            custom_dir=Path(self.tmp.name) / "custom",
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
    def test_the_page(self):
        status, body, content_type = self.client.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/html"))
        self.assertIn(b"<title>Mapwright</title>", body)

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
        self.assertIn("Reviewed by Danah: 1 of 1 held action applied.", done["summary_text"])
        self.assertIn("1 action held for approval", run["summary_text"])

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


def workbook_bytes(path: Path, sheets: dict[str, Path]) -> bytes:
    import io

    import openpyxl

    book = openpyxl.Workbook()
    book.remove(book.active)
    for name, csv_path in sheets.items():
        sheet = book.create_sheet(name)
        for row in csv.reader(csv_path.open(encoding="utf-8", newline="")):
            sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


@unittest.skipUnless(importlib.util.find_spec("openpyxl"), "openpyxl not installed")
class ExcelUploadTest(ServerTest):
    def test_excel_upload_matches_the_csv_run(self):
        data = workbook_bytes(Path(self.tmp.name), {"Notes": EXAMPLE, "Customers": D05})
        status, from_csv = self.client.upload(D05, "contract=customer")
        status, run = self.client.call("POST", "/runs?contract=customer&filename=book.xlsx&sheet=Customers", data, "application/octet-stream")[:2]
        run = json.loads(run)
        self.assertEqual(status, 201, run)
        self.assertEqual(run["sheet"], "Customers")
        self.assertEqual(run["rows"], from_csv["rows"])
        self.assertEqual(run["held_ids"], from_csv["held_ids"])

    def test_unknown_sheet_lists_the_sheets(self):
        data = workbook_bytes(Path(self.tmp.name), {"Customers": D05})
        status, raw, _ = self.client.call("POST", "/runs?contract=customer&filename=book.xlsx&sheet=Nope", data)
        self.assertEqual(status, 400)
        self.assertIn("Customers", json.loads(raw)["error"])

    def test_a_file_that_is_not_a_workbook(self):
        status, raw, _ = self.client.call("POST", "/runs?contract=customer&filename=book.xlsx", b"not excel")
        self.assertEqual(status, 400)


class RulesTest(ServerTest):
    def test_read_rules(self):
        status, detail = self.client.json("GET", "/contracts/customer")
        self.assertEqual(status, 200)
        self.assertTrue(detail["builtin"])
        email = next(f for f in detail["fields"] if f["name"] == "email")
        self.assertFalse(email["unique"])

    def test_save_and_use_custom_rules(self):
        status, saved = self.client.json(
            "POST",
            "/contracts",
            {
                "name": "customer-strict",
                "base": "customer",
                "fields": {
                    "email": {"unique": True, "required": True},
                    "customer_type": {"allowed_values": "INDIVIDUAL, BUSINESS, GOVERNMENT"},
                },
            },
        )
        self.assertEqual(status, 201, saved)
        fields = {f["name"]: f for f in saved["fields"]}
        self.assertTrue(fields["email"]["unique"])
        self.assertEqual(fields["customer_type"]["allowed_values"], ["INDIVIDUAL", "BUSINESS", "GOVERNMENT"])
        self.assertFalse(saved["builtin"])
        self.assertIn("customer-strict", self.client.json("GET", "/contracts")[1]["contracts"])

        # the reference list for city still loads from the saved copy
        status, run = self.client.upload(D05, "contract=customer-strict")
        self.assertEqual(status, 201, run)
        # review works against a custom schema too
        status, done = self.client.json(
            "POST", f"/runs/{run['id']}/review", {"decision": "approve", "approve": "all", "reviewer": "S"}
        )
        self.assertEqual(status, 200, done)

    def test_rules_that_are_refused(self):
        cases = [
            {"name": "customer", "base": "customer", "fields": {}},
            {"name": "bad name!", "base": "customer", "fields": {}},
            {"name": "x", "base": "nope", "fields": {}},
            {"name": "x", "base": "customer", "fields": {"no_such_column": {"unique": True}}},
            {"name": "x", "base": "customer", "fields": {"customer_id": {"unique": False}}},
        ]
        for body in cases:
            status, error = self.client.json("POST", "/contracts", body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", error)


class TokenTest(ServerTest):
    token = "s3cret"

    def test_token_is_required(self):
        self.assertEqual(self.client.json("GET", "/health")[0], 200)
        anonymous = Client(self.client.base)
        status, data = anonymous.json("GET", "/health")
        self.assertEqual(status, 401)
        wrong = Client(self.client.base, "nope")
        self.assertEqual(wrong.json("GET", "/health")[0], 401)
        self.assertEqual(anonymous.call("GET", "/")[0], 200)  # the page itself holds no data


if __name__ == "__main__":
    unittest.main()
