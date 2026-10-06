"""Checks on H04, the held-out case built from a real Companies House export."""

import csv
import json
import unittest
from pathlib import Path

import yaml

from benchmark.real.companies_house import eligible, title_case, truth_row
from mapwright.contracts import check_table, load_contracts

ROOT = Path(__file__).resolve().parents[1]
H04 = ROOT / "benchmark" / "datasets" / "H04"
CONTRACT = load_contracts(ROOT / "contracts")["customer"]
TAXONOMY = {
    issue["id"]: issue
    for issue in yaml.safe_load((ROOT / "benchmark" / "issue_types.yaml").read_text("utf-8"))["issues"]
}


class TitleCaseTest(unittest.TestCase):
    def test_letter_runs(self):
        self.assertEqual(
            title_case("117 PUTNEY BRIDGE ROAD (FREEHOLD) LIMITED"), "117 Putney Bridge Road (Freehold) Limited"
        )
        self.assertEqual(title_case("AL-KAMEL TRADING"), "Al-Kamel Trading")
        self.assertEqual(title_case("12A SOUTHEY  STREET"), "12A Southey Street")

    def test_apostrophes(self):
        self.assertEqual(title_case("ALLEN'S & SONS LIMITED"), "Allen's & Sons Limited")


class TruthRowTest(unittest.TestCase):
    def row(self, **overrides):
        row = {
            "CompanyNumber": "08260755",
            "CompanyName": "ACME LTD",
            "CountryOfOrigin": "United Kingdom",
            "RegAddress.PostTown": "LONDON",
            "CompanyCategory": "Private Limited Company",
            "IncorporationDate": "19/10/2012",
            "CompanyStatus": "Active",
        }
        row.update(overrides)
        return row

    def test_mapping(self):
        self.assertEqual(
            truth_row(self.row()),
            {
                "customer_id": "08260755",
                "full_name": "Acme Ltd",
                "email": "",
                "phone": "",
                "country_code": "GB",
                "city": "London",
                "customer_type": "BUSINESS",
                "signup_date": "2012-10-19",
                "credit_limit": "",
                "is_active": "true",
            },
        )

    def test_status(self):
        self.assertEqual(truth_row(self.row(CompanyStatus="Liquidation"))["is_active"], "false")
        self.assertEqual(truth_row(self.row(CompanyStatus="Active - Proposal to Strike off"))["is_active"], "true")

    def test_sample_rule(self):
        self.assertTrue(eligible(["RUISLIP, LONDON", "01/02/2003"], 0, 1))
        self.assertFalse(eligible(["LEEDS", "01/02/2003"], 0, 1))
        self.assertFalse(eligible(["LONDON", "01/02/1999"], 0, 1))


@unittest.skipUnless((H04 / "manifest.json").exists(), "H04 has not been built")
class BuiltCaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((H04 / "manifest.json").read_text("utf-8"))
        with (H04 / "source.csv").open(encoding="utf-8", newline="") as handle:
            cls.source = list(csv.DictReader(handle))
        with (H04 / "ground_truth.csv").open(encoding="utf-8", newline="") as handle:
            cls.truth = list(csv.DictReader(handle))

    def test_lineage_covers_every_row_once(self):
        lineage = self.manifest["row_lineage"]
        self.assertEqual(set(lineage), {str(n) for n in range(1, len(self.source) + 1)})
        self.assertEqual(sorted(lineage.values()), sorted(row["customer_id"] for row in self.truth))

    def test_ground_truth_satisfies_the_contract_except_missing_columns(self):
        missing = set(self.manifest["missing_target_columns"])
        violations = [v for v in check_table(CONTRACT, self.truth) if not (v.rule == "required" and v.field in missing)]
        self.assertEqual(violations, [])

    def test_issues_follow_the_taxonomy(self):
        for entry in self.manifest["issues"]:
            self.assertEqual(entry["risk"], TAXONOMY[entry["issue"]]["risk"])
        self.assertTrue(any(e["risk"] == "review" for e in self.manifest["issues"]))

    def test_every_changed_cell_is_recorded(self):
        recorded = {(row, e["target"]) for e in self.manifest["issues"] for row in e.get("rows", [])}
        column_level = {e["target"] for e in self.manifest["issues"] if e["issue"] == "V2"}
        found = set()
        for number, (row, truth) in enumerate(zip(self.source, self.truth), start=1):
            for mapping in self.manifest["column_mapping"]:
                target = mapping["target"]
                if target in column_level:
                    continue
                if " ".join(row[h] for h in mapping["source"]) != truth[target]:
                    found.add((number, target))
        self.assertEqual(found, recorded)


if __name__ == "__main__":
    unittest.main()
