"""Scorer tests.

The fixture is small enough to score by hand. Every expected number below
is worked out in the comments next to it, so a failing assertion points at
either a scorer bug or a wrong hand calculation, never at a magic number.
"""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from evaluation.aggregate import summarize
from evaluation.baselines import passthrough, perfect
from evaluation.loading import (
    CONTRACTS,
    DATASETS,
    load_case,
    load_cases,
    load_reference_customer_ids,
    load_run_case,
    write_run_case,
)
from evaluation.scorer import cells_equal, prf, score_case

FIELDS = CONTRACTS["customer"].field_names

TRUTH = [
    {"customer_id": "C-001", "full_name": "Sara Alharbi", "email": "sara@x.com", "phone": "",
     "country_code": "SA", "city": "Jeddah", "customer_type": "INDIVIDUAL",
     "signup_date": "2024-03-05", "credit_limit": "", "is_active": "true"},
    {"customer_id": "C-002", "full_name": "Omar Khan", "email": "", "phone": "+966501234567",
     "country_code": "SA", "city": "Riyadh", "customer_type": "",
     "signup_date": "2023-01-10", "credit_limit": "5000.00", "is_active": "false"},
]

# Row 1 has two dirty cells (email case, country synonym). Row 2 has a
# placeholder email and an unrecoverable blank customer type. Row 3 is an
# exact duplicate of row 2 and should be dropped.
SOURCE = [
    dict(TRUTH[0], email="SARA@X.COM", country_code="KSA"),
    dict(TRUTH[1], email="N/A"),
    dict(TRUTH[1], email="N/A"),
]

MANIFEST = {
    "dataset_id": "T01",
    "entity": "customer",
    "split": "dev",
    "encoding": "utf-8",
    "header_line": 1,
    "source_rows": 3,
    "target_rows": 2,
    "column_mapping": [{"source": [f], "target": f} for f in FIELDS],
    "dropped_columns": [],
    "missing_target_columns": [],
    "issues": [
        {"issue": "S4", "target": "email", "columns": ["email"], "unit": "cell", "risk": "auto", "rows": [1]},
        {"issue": "V1", "target": "country_code", "columns": ["country_code"], "unit": "cell", "risk": "auto", "rows": [1]},
        {"issue": "V3", "target": "email", "columns": ["email"], "unit": "cell", "risk": "auto", "rows": [2]},
        {"issue": "S3", "target": "customer_type", "columns": ["customer_type"], "unit": "cell",
         "risk": "review", "unrecoverable": True, "rows": [2]},
        {"issue": "S2", "target": None, "columns": [], "unit": "row", "risk": "auto", "groups": [[2, 3]]},
    ],
    "row_lineage": {"1": "C-001", "2": "C-002", "3": "drop"},
}

# The system fixed row 1's email but not its country, left the placeholder
# email, invented a customer type for row 2, wrote 5000 instead of 5000.00,
# and kept the duplicate row.
OUTPUT = [
    dict(TRUTH[0], country_code="KSA"),
    dict(TRUTH[1], email="N/A", customer_type="BUSINESS", credit_limit="5000"),
    dict(TRUTH[1], email="N/A", customer_type="BUSINESS", credit_limit="5000"),
]

RESULT = {
    "mapping": [{"source": [f], "target": f} for f in FIELDS],
    "dropped_columns": [],
    "detections": [
        {"issue": "S4", "target": "email", "rows": [1]},           # correct
        {"issue": "V3", "target": "email", "rows": [2]},           # correct
        {"issue": "S4", "target": "full_name", "rows": [1]},       # wrong
        {"issue": "V1", "target": "country_code", "rows": [3]},    # on a drop row: ignored
    ],
    "actions": [
        {"id": "a1", "valid": True, "executed": True, "succeeded": True, "revisions": 0, "status": "applied"},
        {"id": "a2", "valid": True, "executed": True, "succeeded": True, "revisions": 1, "status": "applied"},
        {"id": "a3", "valid": False, "executed": False, "succeeded": False, "revisions": 0, "status": "invalid"},
        {"id": "a4", "valid": True, "executed": True, "succeeded": False, "revisions": 2, "status": "escalated"},
    ],
    "escalations": [
        {"target": "customer_type", "rows": [2]},   # covers the S3 review issue
        {"target": "email", "rows": [1]},           # covers nothing that needs review
    ],
    "usage": {"llm_calls": 3, "input_tokens": 1200, "output_tokens": 300, "seconds": 2.5},
}


def write_csv(path, header, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


class HandScoredCaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        case_dir, run_dir = root / "case", root / "run"
        case_dir.mkdir()
        write_csv(case_dir / "source.csv", FIELDS, SOURCE)
        write_csv(case_dir / "ground_truth.csv", FIELDS, TRUTH)
        (case_dir / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
        case = load_case(case_dir)
        write_run_case(run_dir, case.contract, OUTPUT, [1, 2, 3], RESULT)
        cls.score = score_case(case, load_run_case(run_dir, case.contract))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_transformation(self):
        t = self.score["transformation"]
        # 20 truth cells, 1 unrecoverable -> 19 scored. Dirty: row 1 email,
        # row 1 country, row 2 email = 3; only row 1 email is fixed.
        self.assertEqual(t["dirty_cells"], 3)
        self.assertAlmostEqual(t["dirty_cell_accuracy"], 1 / 3)
        # 16 clean cells, all kept (5000 equals 5000.00 within tolerance).
        self.assertEqual(t["clean_cells"], 16)
        self.assertEqual(t["clean_cell_preservation"], 1.0)
        self.assertAlmostEqual(t["overall_cell_accuracy"], 17 / 19)
        self.assertEqual(t["row_recall"], 1.0)
        # The kept duplicate is 1 of 3 output rows.
        self.assertAlmostEqual(t["spurious_row_rate"], 1 / 3)

    def test_validation(self):
        v = self.score["validation"]
        # Violations: row 1 country; row 2 email, credit_limit format;
        # row 3 email, credit_limit format, duplicate id. 6 of 30 cells.
        self.assertAlmostEqual(v["contract_pass_rate"], 24 / 30)
        # Wrong cells that pass every check: only the invented customer
        # type. 1 per 20 aligned cells = 50 per 1,000.
        self.assertEqual(v["silent_errors"], 1)
        self.assertAlmostEqual(v["silent_errors_per_1000"], 50.0)
        # Truth nulls in aligned rows: row 1 phone and credit_limit, row 2
        # email and customer_type = 4. Invented: customer type. The kept
        # "N/A" is not invented, it was already in the source.
        self.assertAlmostEqual(v["fabrication_rate"], 1 / 4)
        # The oracle reviewer resolves the escalated customer type.
        self.assertEqual(v["fabrication_rate_oracle"], 0.0)

    def test_detection(self):
        overall = self.score["detection"]["overall"]
        # Gold: S4, V1, V3, S3, S2 group = 5. Predicted (after ignoring the
        # drop row): 3, of which 2 are right.
        self.assertEqual((overall["tp"], overall["fp"], overall["fn"]), (2, 1, 3))
        self.assertAlmostEqual(overall["precision"], 2 / 3)
        self.assertAlmostEqual(overall["recall"], 2 / 5)
        self.assertAlmostEqual(overall["f1"], 0.5)
        self.assertEqual(self.score["detection"]["by_issue"]["S4"]["fp"], 1)

    def test_mapping(self):
        self.assertEqual(self.score["mapping"]["f1"], 1.0)

    def test_escalation(self):
        e = self.score["escalation"]
        self.assertEqual(e["review_instances"], 1)
        self.assertEqual(e["recall"], 1.0)
        self.assertEqual(e["precision"], 0.5)
        self.assertEqual(e["missed_reviews"], 0)
        self.assertEqual(e["escalation_rate"], 0.25)

    def test_execution(self):
        x = self.score["execution"]
        self.assertEqual(x["action_validity_rate"], 0.75)
        # Of 3 valid actions, a1 and a2 succeeded.
        self.assertAlmostEqual(x["execution_success_rate"], 2 / 3)
        # a2 and a4 needed repair; only a2 recovered; revisions 1 + 2.
        self.assertEqual(x["repair_recovery_rate"], 0.5)
        self.assertEqual(x["mean_revisions_per_failed_action"], 1.5)

    def test_failures_are_listed(self):
        f = self.score["failures"]
        self.assertEqual([(x["field"], x["output"]) for x in f["fabrications"]], [("customer_type", "BUSINESS")])
        self.assertEqual([x["field"] for x in f["silent_errors"]], ["customer_type"])


class HelpersTest(unittest.TestCase):
    def test_decimal_tolerance(self):
        self.assertTrue(cells_equal("decimal", "12.004", "12.00"))
        self.assertFalse(cells_equal("decimal", "12.01", "12.00"))
        self.assertFalse(cells_equal("decimal", "12,000.00", "12000.00"))
        self.assertFalse(cells_equal("string", "abc", "ABC"))

    def test_f1_conventions(self):
        self.assertEqual(prf(0, 0, 4)["f1"], 0.0)
        self.assertIsNone(prf(0, 0, 4)["precision"])
        self.assertIsNone(prf(0, 0, 0)["f1"])


class ReferenceSystemsTest(unittest.TestCase):
    """The perfect system must score perfectly on every real case; if it
    does not, the scorer or the benchmark is wrong."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.reference_ids = load_reference_customer_ids()
        cls.cases = load_cases(DATASETS)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_system(self, system, case):
        directory = Path(self.tmp.name) / system.__name__ / case.id
        system(case, directory)
        return score_case(case, load_run_case(directory, case.contract), self.reference_ids)

    def test_perfect_system_scores_perfectly(self):
        for case in self.cases:
            with self.subTest(case=case.id):
                s = self.run_system(perfect, case)
                for name in ("dirty_cell_accuracy", "clean_cell_preservation", "overall_cell_accuracy", "row_recall"):
                    if s["transformation"][name] is not None:
                        self.assertEqual(s["transformation"][name], 1.0, name)
                self.assertEqual(s["transformation"]["spurious_row_rate"], 0.0)
                self.assertEqual(s["validation"]["silent_errors"], 0)
                # Order cases have no null truth cells, so the rate is undefined there.
                self.assertIn(s["validation"]["fabrication_rate"], (0.0, None))
                self.assertEqual(s["mapping"]["f1"], 1.0)
                self.assertEqual(s["detection"]["overall"]["f1"], 1.0)
                self.assertEqual(s["escalation"]["recall"], 1.0)
                self.assertEqual(s["escalation"]["precision"], 1.0)
                self.assertEqual(s["escalation"]["missed_reviews"], 0)

    def test_passthrough_is_a_floor(self):
        scores = [self.run_system(passthrough, case) for case in self.cases]
        for s in scores:
            with self.subTest(case=s["case"]):
                self.assertEqual(s["transformation"]["row_recall"], 1.0)
                self.assertEqual(s["validation"]["fabrication_rate"] or 0.0, 0.0)
                self.assertEqual(s["escalation"]["recall"], 0.0)
        summary = summarize(scores)
        for split in summary.values():
            self.assertLess(split["headline"]["Dirty-cell accuracy"], 0.25)


if __name__ == "__main__":
    unittest.main()
