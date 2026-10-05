"""Checks that the generated benchmark is internally consistent.

The most important property: every source cell that differs from its
ground-truth value is explained by an issue in the manifest, and nothing
in the manifest claims a change that is not there. A scorer built on an
inaccurate manifest would be measuring the generator, not the pipeline.
"""

import csv
import io
import json
import re
import unittest
from decimal import Decimal
from pathlib import Path

import yaml

from benchmark.generator import pools
from benchmark.generator.build import CONTRACTS, build_case, reference_customers
from benchmark.generator.cases import CASES
from mapwright.canonical import to_text
from mapwright.contracts import check_table

ROOT = Path(__file__).resolve().parents[1]
DATASETS = ROOT / "benchmark" / "datasets"
TAXONOMY = {
    issue["id"]: issue
    for issue in yaml.safe_load((ROOT / "benchmark" / "issue_types.yaml").read_text("utf-8"))["issues"]
}
COLUMN_LEVEL_CHANGES = {"V2", "M4"}


def load_case(case_id):
    directory = DATASETS / case_id
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    text = (directory / "source.csv").read_bytes().decode(manifest["encoding"])
    lines = list(csv.reader(io.StringIO(text)))
    header = lines[manifest["header_line"] - 1]
    source = [dict(zip(header, row)) for row in lines[manifest["header_line"]:]]
    with (directory / "ground_truth.csv").open(encoding="utf-8", newline="") as handle:
        truth = list(csv.DictReader(handle))
    return manifest, source, truth


class EveryCaseTest(unittest.TestCase):
    def test_cases_on_disk_match_the_generator(self):
        for case in CASES:
            with self.subTest(case=case.id):
                built = build_case(case)
                manifest, source, truth = load_case(case.id)
                self.assertEqual(manifest, json.loads(json.dumps(built.manifest)))
                self.assertEqual(len(source), manifest["source_rows"])
                self.assertEqual(len(truth), manifest["target_rows"])

    def test_generation_is_deterministic(self):
        for case in CASES:
            with self.subTest(case=case.id):
                self.assertEqual(build_case(case).manifest, build_case(case).manifest)

    def test_lineage_covers_every_source_row(self):
        for case in CASES:
            with self.subTest(case=case.id):
                manifest, source, truth = load_case(case.id)
                key = CONTRACTS[case.entity].primary_key[0]
                lineage = manifest["row_lineage"]
                self.assertEqual(set(lineage), {str(n) for n in range(1, len(source) + 1)})
                kept = [v for v in lineage.values() if v != "drop"]
                self.assertEqual(sorted(kept), sorted(row[key] for row in truth))

    def test_ground_truth_satisfies_the_contract(self):
        customer_ids = frozenset(row["customer_id"] for row in reference_customers())
        for case in CASES:
            with self.subTest(case=case.id):
                manifest, _, truth = load_case(case.id)
                contract = CONTRACTS[case.entity]
                allowed_nulls = set()
                for entry in manifest["issues"]:
                    if entry.get("unrecoverable"):
                        allowed_nulls.add(entry["target"])
                keys = {"customer_id": customer_ids} if case.entity == "sales_order" else None
                violations = [
                    v for v in check_table(contract, truth, foreign_keys=keys)
                    if not (v.rule == "required" and v.field in allowed_nulls)
                ]
                self.assertEqual(violations, [])

    def test_issue_ids_and_risk_follow_the_taxonomy(self):
        for case in CASES:
            with self.subTest(case=case.id):
                manifest, _, _ = load_case(case.id)
                for entry in manifest["issues"]:
                    self.assertIn(entry["issue"], TAXONOMY)
                    self.assertEqual(entry["risk"], TAXONOMY[entry["issue"]]["risk"])

    def test_every_case_needs_at_least_one_review(self):
        for case in CASES:
            with self.subTest(case=case.id):
                manifest, _, _ = load_case(case.id)
                self.assertTrue(any(e["risk"] == "review" for e in manifest["issues"]))


class ManifestAccuracyTest(unittest.TestCase):
    def test_every_changed_cell_is_recorded_and_every_record_is_a_change(self):
        for case in CASES:
            with self.subTest(case=case.id):
                self._check_case(case)

    def _check_case(self, case):
        manifest, source, truth = load_case(case.id)
        key = CONTRACTS[case.entity].primary_key[0]
        truth_by_key = {row[key]: row for row in truth}

        recorded = {}
        skipped_fields = set()
        unrecoverable = set()
        for entry in manifest["issues"]:
            if entry["issue"] in COLUMN_LEVEL_CHANGES:
                skipped_fields.add(entry["target"])
            for row in entry.get("rows", []):
                if entry["target"] and entry["issue"] != "S7":
                    recorded[(row, entry["target"])] = entry["issue"]
                    if entry.get("unrecoverable"):
                        unrecoverable.add((row, entry["target"]))

        found = {}
        for number, row in enumerate(source, start=1):
            target_key = manifest["row_lineage"][str(number)]
            if target_key == "drop":
                continue
            expected = truth_by_key[target_key]
            for mapping in manifest["column_mapping"]:
                target = mapping["target"]
                if target in skipped_fields:
                    continue
                actual = " ".join(row[h] for h in mapping["source"])
                if actual != expected[target]:
                    found[(number, target)] = (actual, expected[target])

        unexplained = {cell: values for cell, values in found.items() if cell not in recorded}
        self.assertEqual(unexplained, {}, "changed cells missing from the manifest")

        phantom = [cell for cell in recorded if cell not in found and cell not in unrecoverable]
        self.assertEqual(phantom, [], "manifest records changes that are not in the source")

    def test_scale_suffixes_match_the_ground_truth(self):
        suffix = re.compile(r"^([\d.]+)[Kk]\b")
        for case in CASES:
            manifest, source, truth = load_case(case.id)
            key = CONTRACTS[case.entity].primary_key[0]
            truth_by_key = {row[key]: row for row in truth}
            for entry in manifest["issues"]:
                if entry["issue"] != "V4":
                    continue
                header = entry["columns"][0]
                for number in entry["rows"]:
                    text = source[number - 1][header]
                    match = suffix.match(text)
                    if match:
                        target = truth_by_key[manifest["row_lineage"][str(number)]]
                        with self.subTest(case=case.id, row=number):
                            self.assertEqual(
                                Decimal(match.group(1)) * 1000, Decimal(target[entry["target"]])
                            )


class BenchmarkDesignTest(unittest.TestCase):
    def test_development_cases_cover_every_issue_type(self):
        covered = set()
        for case in CASES:
            if case.split == "dev":
                manifest, _, _ = load_case(case.id)
                covered |= {e["issue"] for e in manifest["issues"]}
        expected = {i for i, info in TAXONOMY.items() if not info.get("stretch")}
        self.assertEqual(expected - covered, set())

    def test_heldout_variants_never_appear_in_development_pool(self):
        for kind, by_canonical in pools.VARIANTS.items():
            dev = {v for pool in by_canonical.values() for v in pool["dev"]}
            heldout = {v for pool in by_canonical.values() for v in pool["heldout"]}
            self.assertEqual(dev & heldout, set(), kind)
        self.assertEqual(set(pools.DISGUISED_MISSING["dev"]) & set(pools.DISGUISED_MISSING["heldout"]), set())

    def test_variants_are_unambiguous(self):
        for kind, by_canonical in pools.VARIANTS.items():
            for pool in ("dev", "heldout"):
                seen = {}
                for canonical, pools_for_value in by_canonical.items():
                    for v in pools_for_value[pool]:
                        normalized = v.strip().lower()
                        with self.subTest(kind=kind, pool=pool, variant=v):
                            self.assertNotEqual(v, canonical)
                            self.assertNotIn(normalized, seen)
                        seen[normalized] = canonical

    def test_heldout_cases_use_only_the_heldout_pool(self):
        for case in CASES:
            self.assertEqual(case.pool == "heldout", case.split == "heldout", case.id)

    def test_reference_customers_are_valid(self):
        rows = [{k: to_text(v) for k, v in row.items()} for row in reference_customers()]
        self.assertEqual(check_table(CONTRACTS["customer"], rows), [])


if __name__ == "__main__":
    unittest.main()
