import json
import tempfile
import unittest
from pathlib import Path

from mapwright.contracts import load_contracts
from mapwright.ingest import parse_text
from mapwright.llm import CachedProvider, CacheMiss, LLMError
from mapwright.llm.fake import FakeProvider
from mapwright.llmplan import Invalid, LLMSession, build_action, llm_mapping
from mapwright.mapping import map_columns
from mapwright.pipeline import run
from mapwright.profile import profile_table

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = load_contracts(ROOT / "contracts")
CUSTOMER = CONTRACTS["customer"]

SOURCE = """\
Company Ref,Name,Kind,Since,Credit,Live,Junk
c-1,SARA ALI,Sole trader,2010-06-25,100.00,Y,x
C-2,Gulf Star Foods,Business,2023-03-05,,N,y
C-3,Noura Hamdan,Individual,2020-02-01,,Y,z
C-4,Ali Saleh,Individual,2021-02-01,,Y,z
C-5,Red Sea Trading,Business,2019-02-01,,Y,z
C-6,Huda Omar,Individual,2018-02-01,,Y,z
"""


def per_field(body, answer_for):
    """Answer a batched values request with ``answer_for(field name) -> actions``."""
    return {"fields": {e["field"]["name"]: {"actions": answer_for(e["field"]["name"])} for e in body["fields"]}}


def answers(request):
    """A scripted model: maps the unknown header and the unknown category values."""
    body = json.loads(request.prompt)
    if request.prompt_version.startswith("map"):
        mappings = []
        columns = [c["column"] for c in body["source_columns"]]
        if "Company Ref" in columns:
            mappings.append({"source": ["Company Ref"], "target": "customer_id", "confidence": 0.95})
        if "Kind" in columns:
            mappings.append({"source": ["Kind"], "target": "customer_type", "confidence": 0.9})
        mappings.append({"source": ["Nope"], "target": "email", "confidence": 0.9})
        return {"mappings": mappings, "drop": ["Junk"]}
    if request.prompt_version.startswith("values"):
        return per_field(
            body,
            lambda name: (
                [
                    {"type": "map_values", "mapping": {"Sole trader": "INDIVIDUAL"}, "confidence": 0.9},
                    {"type": "map_values", "mapping": {"Sole trader": "PERSON"}, "confidence": 0.9},
                ]
                if name == "customer_type"
                else []
            ),
        )
    return {"actions": []}


class BuildActionTest(unittest.TestCase):
    def setUp(self):
        self.field = CUSTOMER.field("customer_type")
        self.values = ["Sole trader", "Ltd company"]
        self.kinds = ["set_null", "map_values"]

    def test_valid_map(self):
        action = build_action(
            CUSTOMER,
            self.field,
            {"type": "map_values", "mapping": {"Sole trader": "INDIVIDUAL"}, "confidence": 0.9},
            self.values,
            self.kinds,
        )
        self.assertEqual(action.mapping, {"sole trader": "INDIVIDUAL"})
        self.assertEqual((action.origin, action.confidence, action.issue), ("llm", 0.9, "V1"))

    def test_rejections(self):
        cases = [
            {"type": "map_values", "mapping": {"Sole trader": "PERSON"}},
            {"type": "map_values", "mapping": {"never shown": "BUSINESS"}},
            {"type": "parse_date", "formats": ["%Y"]},
            {"type": "set_null", "tokens": ["N/A"]},
            "not an object",
        ]
        for item in cases:
            with self.subTest(item=item):
                self.assertIsInstance(build_action(CUSTOMER, self.field, item, self.values, self.kinds), Invalid)

    def test_date_order_is_checked_by_code(self):
        f = CUSTOMER.field("signup_date")
        kinds = ["set_null", "parse_date"]
        settled = build_action(CUSTOMER, f, {"type": "parse_date", "formats": ["%d/%m/%Y"]}, ["25/06/2010"], kinds)
        self.assertFalse(settled.ambiguous)
        unclear = build_action(CUSTOMER, f, {"type": "parse_date", "formats": ["%d/%m/%Y"]}, ["05/03/2023"], kinds)
        self.assertTrue(unclear.ambiguous)
        self.assertEqual((unclear.issue, unclear.scope), ("V2", "column"))


class MappingTest(unittest.TestCase):
    def test_model_fills_only_what_rules_left_and_bad_proposals_are_invalid(self):
        table = parse_text(SOURCE)
        session = LLMSession(FakeProvider(answers))
        result = llm_mapping(session, table, profile_table(table), CUSTOMER, map_columns(table, CUSTOMER))
        by_target = {m.target: (m.sources, m.method) for m in result.mappings}
        self.assertEqual(by_target["customer_id"], (["Company Ref"], "llm"))
        self.assertEqual(by_target["full_name"][1], "synonym")
        self.assertIn("Junk", result.dropped)
        self.assertEqual([i.target for i in session.invalid], ["email"])


class SystemsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.tmp.name) / "export.csv"
        cls.path.write_text(SOURCE, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_hybrid_uses_the_model_for_leftovers(self):
        session = LLMSession(FakeProvider(answers))
        outcome = run(self.path, CUSTOMER, system="hybrid", session=session)
        rows = dict(zip(outcome.source_rows, outcome.rows))
        self.assertEqual(rows[1]["customer_id"], "C-1")
        self.assertEqual(rows[1]["customer_type"], "INDIVIDUAL")
        self.assertFalse(any(f.issue == "V1" and f.target == "customer_type" for f in outcome.findings))
        result = outcome.result()
        self.assertGreater(result["usage"]["llm_calls"], 0)
        statuses = [a["status"] for a in result["actions"] if a["origin"] == "llm"]
        self.assertIn("invalid", statuses)
        self.assertIn("applied", statuses)

    def test_rules_system_needs_no_session(self):
        outcome = run(self.path, CUSTOMER)
        self.assertEqual(outcome.result()["usage"]["llm_calls"], 0)
        self.assertNotIn("customer_id", {m.target for m in outcome.mapping.mappings})

    def test_llm_only_system(self):
        def llm_answers(request):
            body = json.loads(request.prompt)
            if request.prompt_version.startswith("map"):
                return {
                    "mappings": [
                        {"source": ["Company Ref"], "target": "customer_id", "confidence": 0.9},
                        {"source": ["Name"], "target": "full_name", "confidence": 0.9},
                        {"source": ["Live"], "target": "is_active", "confidence": 0.9},
                    ]
                }

            def for_field(name):
                if name == "customer_id":
                    return [{"type": "normalize_text", "case": "upper", "confidence": 0.9}]
                if name == "is_active":
                    return [{"type": "map_values", "mapping": {"Y": "true", "N": "false"}, "confidence": 0.9}]
                return []

            return per_field(body, for_field)

        session = LLMSession(FakeProvider(llm_answers))
        outcome = run(self.path, CUSTOMER, system="llm", session=session)
        rows = dict(zip(outcome.source_rows, outcome.rows))
        self.assertEqual(rows[1]["customer_id"], "C-1")
        # is_active changes every value: an LLM action that broad is held for review
        self.assertEqual(rows[1]["is_active"], "Y")
        self.assertTrue(any(e["target"] == "is_active" for e in outcome.escalations))
        self.assertTrue(
            any(f.issue == "S3" and f.target == "customer_type" for f in outcome.findings)
            or "customer_type" in outcome.mapping.missing
        )

    def test_repair_loop_revises_a_failed_action(self):
        source = "id,signup\nC-1,25/06/2010\n" + "".join(f"C-{n},2011-01-0{n}\n" for n in range(2, 8))
        path = Path(self.tmp.name) / "dates.csv"
        path.write_text(source, encoding="utf-8")

        def date_answers(request):
            body = json.loads(request.prompt)
            if request.prompt_version.startswith("map"):
                return {"mappings": [{"source": ["signup"], "target": "signup_date", "confidence": 0.9}]}
            if request.prompt_version.startswith("revise"):
                return {"actions": [{"type": "parse_date", "formats": ["%d/%m/%Y", "%Y-%m-%d"], "confidence": 0.9}]}
            return per_field(
                body,
                lambda name: (
                    [{"type": "parse_date", "formats": ["%Y-%m-%d"], "confidence": 0.9}]
                    if name == "signup_date"
                    else []
                ),
            )

        with_repair = run(path, CUSTOMER, system="llm", session=LLMSession(FakeProvider(date_answers)))
        without = run(path, CUSTOMER, system="llm", session=LLMSession(FakeProvider(date_answers), repair=False))
        repaired = [a for a in with_repair.result()["actions"] if a["type"] == "parse_date"]
        self.assertEqual(repaired[0]["revisions"], 1)
        self.assertTrue(repaired[0]["succeeded"])
        self.assertEqual(dict(zip(with_repair.source_rows, with_repair.rows))[1]["signup_date"], "2010-06-25")
        self.assertEqual(dict(zip(without.source_rows, without.rows))[1]["signup_date"], "25/06/2010")

    def test_a_failed_request_stops_the_run(self):
        def broken(request):
            raise LLMError("Gemini returned HTTP 404: model retired")

        class Failing(FakeProvider):
            def complete(self, request):
                broken(request)

        with self.assertRaises(LLMError):
            run(self.path, CUSTOMER, system="hybrid", session=LLMSession(Failing(lambda r: {})))

    def test_values_are_asked_once_per_case(self):
        provider = FakeProvider(answers)
        run(self.path, CUSTOMER, system="llm", session=LLMSession(provider))
        versions = [r.prompt_version for r in provider.requests]
        self.assertEqual(versions.count("values-v2"), 1)

    def test_replay_without_a_cached_answer_fails_loudly(self):
        with tempfile.TemporaryDirectory() as cache:
            session = LLMSession(CachedProvider(cache, model="gemini-test"))
            with self.assertRaises(CacheMiss):
                run(self.path, CUSTOMER, system="hybrid", session=session)


if __name__ == "__main__":
    unittest.main()
