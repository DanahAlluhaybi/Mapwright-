import json
import shutil
import tempfile
import unittest
from pathlib import Path

from benchmark.generator import pools
from evaluation.loading import load_case, load_reference_customer_ids, load_run_case
from evaluation.scorer import score_case
from mapwright.contracts import load_contracts
from mapwright.dictionaries import _load
from mapwright.normalize import match_key
from mapwright.output import write_run
from mapwright.pipeline import replay, run

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = load_contracts(ROOT / "contracts")
DATASETS = ROOT / "benchmark" / "datasets"
DEV_CASES = ["D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08"]

CUSTOMERS = """\
Cust No,Name,Email,Mobile,Country,City,Type,Open Date,Credit Limit,Active,Row Hash
c-1 ,SARA  ALI,0501234567,,KSA,JED,I,25/06/2010,"12,500.00",Y,ab12
C-2,Gulf Star Foods,accounts@gulfstar.com,+966 55 000 1111,SA,Riyadh,B,05/03/2023,N/A,N,cd34
C-3,Noura Hamdan,noura@,,Mars,Dubai,,01/02/2020,-50,Y,ef56
"""


def _references(entity):
    return {"customer_id": load_reference_customer_ids()} if entity == "sales_order" else {}


class SmallFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.tmp.name) / "customers.csv"
        cls.path.write_text(CUSTOMERS, encoding="utf-8")
        cls.outcome = run(cls.path, CONTRACTS["customer"])
        cls.rows = {n: row for n, row in zip(cls.outcome.source_rows, cls.outcome.rows)}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_values_reach_canonical_form(self):
        self.assertEqual(
            self.rows[1],
            {
                "customer_id": "C-1",
                "full_name": "Sara Ali",
                "email": "",
                "phone": "+966501234567",
                "country_code": "SA",
                "city": "Jeddah",
                "customer_type": "INDIVIDUAL",
                "signup_date": "2010-06-25",
                "credit_limit": "12500.00",
                "is_active": "true",
            },
        )
        self.assertEqual(self.rows[2]["phone"], "+966550001111")
        self.assertEqual(self.rows[2]["credit_limit"], "")

    def test_what_rules_cannot_fix_is_left_and_escalated(self):
        self.assertEqual(self.rows[3]["country_code"], "Mars")
        self.assertEqual(self.rows[3]["email"], "")
        escalated = {(e["issue"], e["target"], tuple(e.get("rows") or ())) for e in self.outcome.escalations}
        self.assertIn(("V1", "country_code", (3,)), escalated)
        self.assertIn(("S3", "customer_type", (3,)), escalated)
        self.assertIn(("S5", "credit_limit", (3,)), escalated)
        self.assertIn(("V7", "email", (1,)), escalated)
        self.assertIn(("V2", "signup_date", ()), escalated)

    def test_detections(self):
        found = {(d["issue"], d.get("target") or d["columns"][0]) for d in self.outcome.detections()}
        for expected in [
            ("M1", "customer_id"),
            ("M5", "Row Hash"),
            ("S4", "customer_id"),
            ("S4", "full_name"),
            ("V7", "email"),
            ("V7", "phone"),
            ("V1", "city"),
            ("V5", "is_active"),
            ("S1", "credit_limit"),
            ("V3", "credit_limit"),
            ("S6", "email"),
            ("V2", "signup_date"),
        ]:
            with self.subTest(expected=expected):
                self.assertIn(expected, found)
        self.assertNotIn(
            ("S9", "phone"), {(d["issue"], d.get("target")) for d in self.outcome.detections() if d.get("rows") == [1]}
        )


class RulesOnBenchmarkTest(unittest.TestCase):
    """Development cases only. Held-out cases are scored once per version, never in tests."""

    def test_plan_replays_to_the_same_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            for case_id in ["D02", "D05", "D07", "D08"]:
                with self.subTest(case=case_id):
                    case = load_case(DATASETS / case_id)
                    outcome = run(DATASETS / case_id / "source.csv", case.contract, _references(case.contract.entity))
                    out = write_run(outcome, Path(tmp) / case_id)
                    plan = json.loads((out / "plan.json").read_text("utf-8"))
                    rows, numbers = replay(plan, DATASETS / case_id / "source.csv", case.contract)
                    self.assertEqual((rows, numbers), (outcome.rows, outcome.source_rows))

    def test_approving_held_actions_applies_them(self):
        case = load_case(DATASETS / "D07")
        outcome = run(DATASETS / "D07" / "source.csv", case.contract, _references("sales_order"))
        held = [s.action.id for s in outcome.steps if s.status == "escalated"]
        rows, _ = replay(outcome.plan(), DATASETS / "D07" / "source.csv", case.contract, set(held))
        self.assertLess(len(rows), len(outcome.rows))

    def test_pipeline_sees_only_the_source_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            alone = Path(tmp) / "export.csv"
            shutil.copy(DATASETS / "D04" / "source.csv", alone)
            isolated = run(alone, CONTRACTS["customer"])
            in_place = run(DATASETS / "D04" / "source.csv", CONTRACTS["customer"])
        self.assertEqual(isolated.rows, in_place.rows)
        self.assertEqual(isolated.escalations, in_place.escalations)

    def test_development_scores_do_not_regress(self):
        reference_ids = load_reference_customer_ids()
        with tempfile.TemporaryDirectory() as tmp:
            for case_id in DEV_CASES:
                with self.subTest(case=case_id):
                    case = load_case(DATASETS / case_id)
                    outcome = run(DATASETS / case_id / "source.csv", case.contract, _references(case.contract.entity))
                    out = write_run(outcome, Path(tmp) / case_id)
                    scores = score_case(case, load_run_case(out, case.contract), reference_ids)
                    self.assertGreaterEqual(scores["transformation"]["dirty_cell_accuracy"], 0.95)
                    self.assertEqual(scores["validation"]["silent_errors"], 0)
                    self.assertEqual(scores["escalation"]["missed_reviews"], 0)
                    self.assertEqual(scores["mapping"]["f1"], 1.0)


class DictionaryIsolationTest(unittest.TestCase):
    def test_no_held_out_variant_in_the_rule_dictionaries(self):
        rules = _load("values.yaml")
        listed = {match_key(str(t)) for t in rules["missing_tokens"]}
        for spec in [*rules["fields"].values(), rules["boolean"]]:
            for spellings in spec["values"].values():
                listed |= {match_key(str(s)) for s in spellings}

        def spellings(pool):
            found = {match_key(t) for t in pools.DISGUISED_MISSING[pool]}
            for by_value in pools.VARIANTS.values():
                for variants in by_value.values():
                    found |= {match_key(v) for v in variants[pool]}
            return found

        # Lookups ignore case, so a held-out spelling that differs from a
        # development one only by case is matched without being listed.
        only_held_out = spellings("heldout") - spellings("dev")
        self.assertEqual(listed & only_held_out, set())


if __name__ == "__main__":
    unittest.main()
