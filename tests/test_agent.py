import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from mapwright.agent import analysis, ops
from mapwright.agent.infer import infer_column, infer_table
from mapwright.agent.nl import interpret
from mapwright.agent.quality import all_findings
from mapwright.agent.session import SessionError, SessionStore
from mapwright.agent.table import Table, read_table
from mapwright.server import Settings, make_server

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo_customers.xlsx"
ORDERS = ROOT / "benchmark" / "datasets" / "D06" / "source.csv"
os.environ.setdefault("MAPWRIGHT_API_QUIET", "1")

SALES = """order,date,region,amount,channel
1,2024-01-05,North,100,Online
2,2024-01-20,South,200,online
3,2024-02-03,North,300,Store
4,2024-02-14,South,400,Store
5,2024-03-01,North,500,Online
6,2024-03-09,North,600,STORE
"""


def table_from(text: str) -> Table:
    return read_table(text.encode("utf-8"), "data.csv")[0]


class InferTest(unittest.TestCase):
    def test_types_on_the_demo_file(self):
        table, info = read_table(DEMO.read_bytes(), "demo.xlsx", "Customers")
        types = {c.name: c.type for c in infer_table(table)}
        self.assertEqual(info["sheet"], "Customers")
        self.assertEqual(types["Customer No"], "identifier")
        self.assertEqual(types["E-mail"], "email")
        self.assertEqual(types["Mobile"], "phone")
        self.assertEqual(types["Joined"], "date")
        self.assertEqual(types["Active"], "boolean")
        self.assertEqual(types["Type"], "category")
        self.assertEqual(types["Credit"], "integer")

    def test_evidence_counts_and_misfits(self):
        c = infer_column("Credit", ["100", "2,000", "300", "400", "abc", "", "N/A"])
        self.assertEqual(c.type, "integer")
        self.assertEqual((c.blank, c.null_tokens), (1, 1))
        self.assertIn("4 of 5 filled values", c.evidence[0])
        self.assertEqual(c.misfits, ["abc"])

    def test_zero_one_counts_are_numbers_unless_named_as_flags(self):
        self.assertEqual(infer_column("NumPartners", ["0", "1", "0", "1"]).type, "integer")
        self.assertEqual(infer_column("is_active", ["0", "1", "0", "1"]).type, "boolean")

    def test_ambiguous_dates_are_reported(self):
        c = infer_column("Joined", ["01/02/2024", "03/04/2024", "05/06/2024"])
        self.assertEqual(c.type, "date")
        self.assertTrue(c.date_ambiguous)

    def test_header_words_do_not_match_inside_other_words(self):
        self.assertEqual(infer_column("Country", ["SA", "AE", "SA", "AE"]).meaning, "a place")


class OpsTest(unittest.TestCase):
    def setUp(self):
        self.table = table_from("name,active,joined,amount,city\n Sara ,Yes,03/14/2024,\"1,200\",Jeddah\nomar,no,04/30/2024,٧٠٠ ريال,\nOmar,maybe,05/01/2024,N/A,Riyadh\n")

    def test_to_boolean_reports_what_it_cannot_convert(self):
        out, effect = ops.run(self.table, {"op": "to_boolean", "column": "active"})
        self.assertEqual(out.column("active"), ["true", "false", "maybe"])
        self.assertEqual(effect.unmatched["maybe"], 1)
        self.assertEqual(self.table.column("active"), ["Yes", "no", "maybe"])  # the input is untouched

    def test_dates_numbers_and_units(self):
        out, _ = ops.run(self.table, {"op": "parse_date", "column": "joined"})
        self.assertEqual(out.column("joined"), ["2024-03-14", "2024-04-30", "2024-05-01"])
        out, effect = ops.run(self.table, {"op": "parse_number", "column": "amount"})
        self.assertEqual(out.column("amount"), ["1200", "700", "N/A"])
        self.assertTrue(any("SAR" in n for n in effect.notes))

    def test_ambiguous_dates_need_an_order(self):
        table = table_from("d\n01/02/2024\n03/04/2024\n")
        with self.assertRaisesRegex(ops.OpError, "day and month"):
            ops.run(table, {"op": "parse_date", "column": "d"})
        out, _ = ops.run(table, {"op": "parse_date", "column": "d", "order": "month_first"})
        self.assertEqual(out.column("d"), ["2024-01-02", "2024-03-04"])
        out, _ = ops.run(table, {"op": "parse_date", "column": "d", "order": "day_first"})
        self.assertEqual(out.column("d"), ["2024-02-01", "2024-04-03"])

    def test_fill_missing_only_with_a_chosen_value_or_a_shown_statistic(self):
        with self.assertRaisesRegex(ops.OpError, "value"):
            ops.run(self.table, {"op": "fill_missing", "column": "city", "strategy": "constant"})
        out, effect = ops.run(self.table, {"op": "fill_missing", "column": "city", "strategy": "constant", "value": "Unknown"})
        self.assertEqual(out.column("city")[1], "Unknown")
        self.assertTrue(any("estimates" in n for n in effect.notes))
        with self.assertRaises(ops.OpError):
            ops.run(self.table, {"op": "fill_missing", "column": "city", "strategy": "guess"})

    def test_rows_columns_and_duplicates(self):
        out, effect = ops.run(self.table, {"op": "drop_rows", "column": "city", "where": "missing"})
        self.assertEqual((out.size, effect.removed), (2, [2]))
        out, effect = ops.run(self.table, {"op": "deduplicate", "columns": ["name"]})
        self.assertEqual(out.size, 2)  # "omar" and "Omar" are the same name
        out, effect = ops.run(table_from("k,v\na,1\nA,2\n"), {"op": "deduplicate", "columns": ["k"]})
        self.assertEqual(effect.removed, [2])
        self.assertTrue(any("differ" in n for n in effect.notes))
        out, _ = ops.run(self.table, {"op": "split_column", "column": "joined", "separator": "/", "into": ["m", "rest"]})
        self.assertEqual(out.columns[3:5], ["m", "rest"])
        with self.assertRaises(ops.OpError):
            ops.run(self.table, {"op": "rename_column", "column": "name", "new_name": "city"})

    def test_only_the_vocabulary_runs(self):
        for spec in [{"op": "exec", "code": "x"}, {"op": "to_boolean", "column": "nope"}, {"op": "to_boolean", "column": "active", "shell": "rm"}, "drop"]:
            with self.assertRaises(ops.OpError):
                ops.run(self.table, spec)

    def test_replay_matches_step_by_step(self):
        plan = [{"op": "normalize_text", "column": "name", "case": "title"}, {"op": "to_boolean", "column": "active"}]
        step = self.table
        for spec in plan:
            step, _ = ops.run(step, spec)
        self.assertEqual(ops.replay(self.table, plan).rows, step.rows)


class QualityTest(unittest.TestCase):
    def test_findings_on_the_demo_file(self):
        table, _ = read_table(DEMO.read_bytes(), "demo.xlsx")
        kinds = {(f["kind"], f["column"]) for f in all_findings(table, infer_table(table))}
        for expected in [("duplicate_rows", None), ("placeholders", "E-mail"), ("date_format", "Joined"),
                         ("boolean_words", "Active"), ("type", "Credit"), ("email_invalid", "E-mail")]:
            self.assertIn(expected, kinds)

    def test_mixed_currencies_and_end_before_start(self):
        table = table_from("start date,end date,amount\n2024-01-01,2024-02-01,100 SAR\n2024-03-01,2024-02-01,$50\n2024-01-01,2024-01-05,70 SAR\n2024-01-02,2024-01-09,80 SAR\n")
        findings = {f["kind"]: f for f in all_findings(table, infer_table(table))}
        self.assertEqual(findings["date_order_pair"]["rows"], [2])
        self.assertIn("currencies", findings)


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        self.table = table_from(SALES)
        self.columns = infer_table(self.table)

    def run_q(self, spec):
        return analysis.run_query(self.table, self.columns, spec)

    def test_group_numbers_are_exact(self):
        r = self.run_q({"q": "group", "by": "region", "metric": "amount", "agg": "sum"})
        self.assertEqual(r["table"]["rows"], [["North", "1,500"], ["South", "600"]])
        self.assertIn("71% of the total", r["facts"][0])
        r = self.run_q({"q": "group", "by": "region", "agg": "count", "where": [{"column": "channel", "op": "in", "value": ["store"]}]})
        self.assertEqual(r["rows_used"], 3)

    def test_trend_and_relation(self):
        r = self.run_q({"q": "trend", "date": "date", "metric": "amount", "period": "month"})
        self.assertEqual(r["table"]["rows"], [["2024-01", "300"], ["2024-02", "700"], ["2024-03", "1,100"]])
        self.assertFalse(any("incomplete" in c for c in r["caveats"]))  # March is as full as the other months
        thin = table_from(SALES + "7,2024-04-02,North,10,Store\n")
        r = analysis.run_query(thin, infer_table(thin), {"q": "trend", "date": "date", "metric": "amount", "period": "month"})
        self.assertTrue(any("2024-04 is incomplete" in c for c in r["caveats"]), r["caveats"])
        r = self.run_q({"q": "relation", "a": "order", "b": "amount"})
        self.assertIn("r = 1.00", r["facts"][0])
        self.assertIn("does not show that one causes the other", r["caveats"][0])

    def test_forecast_needs_history_and_is_checked(self):
        r = self.run_q({"q": "forecast", "date": "date", "metric": "amount", "period": "month"})
        self.assertIn("Not enough history", r["facts"][0])
        lines = ["day,value"] + [f"2024-{m:02d}-28,{100 + 10 * m}" for m in range(1, 13)]
        table = table_from("\n".join(lines) + "\n")
        r = analysis.run_query(table, infer_table(table), {"q": "forecast", "date": "day", "metric": "value", "period": "month", "horizon": 2})
        self.assertTrue(r["estimate"])
        self.assertIn("straight-line trend", r["facts"][0])
        self.assertEqual(r["table"]["rows"], [["2025-01", "230"], ["2025-02", "240"]])

    def test_bad_queries(self):
        for spec in [{"q": "sql", "text": "drop"}, {"q": "group", "by": "nope"}, {"q": "trend", "date": "region"}]:
            with self.assertRaises(analysis.QueryError):
                self.run_q(spec)


class InterpretTest(unittest.TestCase):
    def test_requests(self):
        table, _ = read_table(DEMO.read_bytes(), "demo.xlsx")
        columns = infer_table(table)
        cases = [
            ("Convert this column into Boolean values", ["Active"], "proposal"),
            ("حول عمود Active الى نعم/لا", [], "proposal"),
            ("Convert Joined into a consistent date format", [], "proposal"),
            ("fill missing Credit", [], "clarify"),
            ("Map these labels: Individual = INDIVIDUAL, Company = BUSINESS", ["Type"], "proposal"),
            ("Tell me which columns are useful for sales analysis", [], "answer"),
            ("top 3 City by Credit", [], "analysis"),
            ("find suspicious records", [], "analysis"),
            ("create a dashboard", [], "dashboard"),
            ("make me a sandwich", [], "unknown"),
        ]
        for text, selected, kind in cases:
            with self.subTest(text=text):
                self.assertEqual(interpret(text, selected, table, columns)["kind"], kind)

    def test_a_bad_model_answer_never_runs(self):
        table, _ = read_table(DEMO.read_bytes(), "demo.xlsx")
        columns = infer_table(table)

        class Liar:
            model = "fake"

            def complete(self, request):
                from mapwright.llm import LLMResponse

                return LLMResponse(text=json.dumps({"kind": "proposal", "ops": [{"op": "exec", "code": "rm -rf"}]}), model="fake")

        result = interpret("delete everything", [], table, columns, Liar())
        self.assertEqual(result["kind"], "unknown")
        self.assertIn("unknown operation", result["message"])


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self.tmp.name))
        self.session = self.store.create(DEMO.read_bytes(), "demo.xlsx", "Customers")

    def tearDown(self):
        self.tmp.cleanup()

    def test_approve_undo_and_the_original_stays(self):
        original = (self.session.dir / "original.csv").read_bytes()
        answer = self.session.ask("convert Active to yes/no", [])
        pid = answer["proposals"][0]["id"]
        with self.assertRaisesRegex(SessionError, "who"):
            self.session.decide(pid, "approve", "")
        result = self.session.decide(pid, "approve", "Danah")
        self.assertTrue(any("yes/no" in r for r in result["validation"]["resolved"]), result["validation"])
        self.assertEqual(set(self.session.state()[0].column("Active")), {"true", "false"})
        with self.assertRaises(SessionError) as ctx:
            self.session.decide(pid, "approve", "Danah")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual((self.session.dir / "original.csv").read_bytes(), original)
        self.session.undo("Danah")
        self.assertIn("Y", self.session.state()[0].column("Active"))
        events = [h["event"] for h in self.session.history()]
        self.assertEqual(events, ["uploaded", "proposed", "applied", "undone"])

    def test_rejecting_changes_nothing_and_type_can_be_confirmed(self):
        p = self.session.propose({"op": "drop_column", "column": "Mobile"})
        self.session.decide(p["id"], "reject", "Danah")
        self.assertIn("Mobile", self.session.state()[0].columns)
        detail = self.session.set_type("Customer No", "text", "Danah")
        self.assertEqual(detail["type"], "text")
        self.assertIn("set to text by you", detail["evidence"][0])

    def test_pins_dashboard_and_report(self):
        from mapwright.agent.report import build_dashboard, build_report

        self.session.pin({"q": "group", "by": "City", "agg": "count"})
        html = build_dashboard(self.session, [])
        self.assertIn("Rows by City", html)
        self.assertIn("<svg", html)
        report = build_report(self.session)
        for section in ("What the file contains", "Data quality", "Changes", "Analysis"):
            self.assertIn(section, report)
        with self.assertRaises(SessionError):
            self.session.pin({"q": "group", "by": "nope"})


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "data").mkdir()
        (root / "data" / "orders.csv").write_bytes(ORDERS.read_bytes())
        (root / "data" / "other.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        self.settings = Settings(runs_dir=root / "runs" / "api", custom_dir=root / "custom", data_root=root / "data",
                                 contracts_dir=ROOT / "contracts")
        self.server = make_server(self.settings, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=60) as r:
                content = r.read()
                return r.status, (json.loads(content) if r.headers.get("Content-Type", "").startswith("application/json") else content), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers

    def test_the_whole_journey(self):
        status, page, _ = self.call("GET", "/agent")
        self.assertEqual(status, 200)
        self.assertIn(b"Mapwright Agent", page)
        status, overview, _ = self.call("POST", "/sessions?filename=orders.csv", raw=ORDERS.read_bytes())
        self.assertEqual(status, 201, overview)
        sid = overview["id"]
        status, column, _ = self.call("GET", f"/sessions/{sid}/columns/DOC_DATE")
        self.assertEqual(column["type"], "date")
        status, answer, _ = self.call("POST", f"/sessions/{sid}/ask", {"text": "convert DOC_DATE into a consistent date format", "columns": []})
        pid = answer["proposals"][0]["id"]
        self.assertEqual(answer["proposals"][0]["effect"]["cells_changed"], 350)
        status, decided, _ = self.call("POST", f"/sessions/{sid}/proposals/{pid}", {"decision": "approve", "reviewer": "Danah"})
        self.assertEqual(status, 200, decided)
        status, answer, _ = self.call("POST", f"/sessions/{sid}/ask", {"text": "trend of NET_AMOUNT per year", "columns": []})
        query = answer["results"][0]["query"]
        self.call("POST", f"/sessions/{sid}/pins", {"query": query})
        status, csv_bytes, headers = self.call("GET", f"/sessions/{sid}/export/csv")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertTrue(csv_bytes.decode().splitlines()[1].split(",")[2].startswith("20"))
        status, html, _ = self.call("GET", f"/sessions/{sid}/dashboard?column=SALES_CHANNEL&value=ONLINE")
        self.assertIn(b"Filtered", html)
        status, plan, _ = self.call("GET", f"/sessions/{sid}/export/plan")
        self.assertEqual(plan["steps"][0]["op"], "parse_date")

        # n8n jobs: check, idempotency, repeating the plan on another file
        status, job, _ = self.call("POST", "/jobs", {"kind": "check", "source_path": "orders.csv", "idempotency_key": "day-1", "threshold": 0.99})
        self.assertEqual(status, 202, job)
        status, again, _ = self.call("POST", "/jobs", {"kind": "check", "source_path": "orders.csv", "idempotency_key": "day-1"})
        self.assertEqual((status, again["id"]), (200, job["id"]))
        done = self.wait(job["id"])
        self.assertEqual(done["status"], "done", done)
        self.assertTrue(done["result"]["below_threshold"])
        status, applied, _ = self.call("POST", "/jobs", {"kind": "apply_plan", "source_path": "orders.csv", "session": sid})
        self.assertEqual(self.wait(applied["id"])["result"]["steps_applied"], 1)
        status, failing, _ = self.call("POST", "/jobs", {"kind": "apply_plan", "source_path": "other.csv", "session": sid})
        failed = self.wait(failing["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("step 1", failed["error"])
        self.assertIn("No steps were kept", failed["error"])
        status, error, _ = self.call("POST", "/jobs", {"kind": "check", "source_path": "../../etc/passwd"})
        self.assertEqual(self.wait(error["id"])["status"], "failed")

    def wait(self, jid):
        for _ in range(100):
            status, job, _ = self.call("GET", f"/jobs/{jid}")
            if job["status"] in ("done", "failed"):
                return job
            time.sleep(0.1)
        self.fail("job did not finish")

    def test_errors_are_json(self):
        for method, path, body in [("GET", "/sessions/s-nope", None), ("POST", "/jobs", {"kind": "nope"}),
                                   ("GET", "/jobs/j-nope", None)]:
            status, data, _ = self.call(method, path, body)
            self.assertIn(status, (400, 404))
            self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()
