"""Scores one system run on one benchmark case.

Every metric here is defined in docs/EVALUATION.md. Ratios with an empty
denominator are reported as None rather than 0 or 1, so a case that has no
dirty cells of some kind neither helps nor hurts an average.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from mapwright.contracts import check_table

from .loading import ROOT, BenchmarkCase, RunCase

TAXONOMY = {
    issue["id"]: issue
    for issue in yaml.safe_load((ROOT / "benchmark" / "issue_types.yaml").read_text("utf-8"))["issues"]
}
DECIMAL_TOLERANCE = Decimal("0.005")


def ratio(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else numerator / denominator


def prf(tp: int, fp: int, fn: int) -> dict:
    precision = ratio(tp, tp + fp)
    recall = ratio(tp, tp + fn)
    if tp == 0:
        # Nothing right: F1 is 0 if anything was predicted or expected,
        # and undefined only when both sides are empty.
        f1 = 0.0 if fp + fn else None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {"precision": precision, "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn}


def _set_prf(gold: set, predicted: set) -> dict:
    return prf(len(gold & predicted), len(predicted - gold), len(gold - predicted))


# Issue instances ---------------------------------------------------------

def _where(entry: dict) -> str | None:
    return entry.get("target") or (entry.get("columns") or [None])[0]


def instance_keys(entry: dict, drop_rows: set[int]) -> set[tuple]:
    """Turn a manifest issue or a detection into comparable instances.

    Column-level: (issue, field, None). Cell or row level: (issue, field, row).
    Duplicate groups: (issue, None, frozenset(rows)), matched in any order.
    Cell-level instances on rows that should be dropped are ignored.
    """
    issue = entry["issue"]
    if entry.get("groups") is not None:
        return {(issue, None, frozenset(group)) for group in entry["groups"]}
    where = _where(entry)
    rows = entry.get("rows")
    if rows is None:
        return {(issue, where, None)}
    unit = TAXONOMY.get(issue, {}).get("unit")
    return {(issue, where, row) for row in rows if not (unit == "cell" and row in drop_rows)}


def _layer(issue: str) -> str:
    return TAXONOMY.get(issue, {}).get("layer", "unknown")


def score_detection(manifest_issues: list[dict], detections: list[dict], drop_rows: set[int]) -> dict:
    gold = set().union(*(instance_keys(e, drop_rows) for e in manifest_issues)) if manifest_issues else set()
    predicted = set().union(*(instance_keys(d, drop_rows) for d in detections)) if detections else set()

    by_issue = {}
    for issue in sorted({k[0] for k in gold | predicted}):
        by_issue[issue] = _set_prf({k for k in gold if k[0] == issue}, {k for k in predicted if k[0] == issue})

    by_layer = {}
    for layer in sorted({_layer(k[0]) for k in gold | predicted}):
        by_layer[layer] = _set_prf(
            {k for k in gold if _layer(k[0]) == layer}, {k for k in predicted if _layer(k[0]) == layer}
        )

    return {
        "overall": _set_prf(gold, predicted),
        "location_only": _set_prf({k[1:] for k in gold}, {k[1:] for k in predicted}),
        "by_issue": by_issue,
        "by_layer": by_layer,
    }


# Mapping -----------------------------------------------------------------

def score_mapping(manifest: dict, result: dict) -> dict:
    gold = {(frozenset(m["source"]), m["target"]) for m in manifest["column_mapping"]}
    gold |= {(frozenset([c]), "drop") for c in manifest["dropped_columns"]}
    predicted = {(frozenset(m["source"]), m["target"]) for m in result.get("mapping", [])}
    predicted |= {(frozenset([c]), "drop") for c in result.get("dropped_columns", [])}
    return _set_prf(gold, predicted)


# Cells -------------------------------------------------------------------

def cells_equal(field_type: str, output: str, truth: str) -> bool:
    if output == truth:
        return True
    if field_type == "decimal" and output and truth:
        try:
            return abs(Decimal(output) - Decimal(truth)) <= DECIMAL_TOLERANCE
        except InvalidOperation:
            return False
    return False


class _Alignment:
    """Which output row stands for which ground-truth row."""

    def __init__(self, case: BenchmarkCase, run: RunCase):
        lineage = case.manifest["row_lineage"]
        self.drop_rows = {int(n) for n, key in lineage.items() if key == "drop"}
        self.key_to_source = {key: int(n) for n, key in lineage.items() if key != "drop"}
        self.aligned: dict[str, int] = {}
        self.spurious: list[int] = []
        for index, number in enumerate(run.source_rows):
            key = lineage.get(str(number)) if number is not None else None
            if key is None or key == "drop" or key in self.aligned:
                self.spurious.append(index)
            else:
                self.aligned[key] = index


def _source_text(case: BenchmarkCase, number: int, field: str, sources: dict[str, list[str]]) -> str:
    headers = sources.get(field)
    if not headers:
        return ""
    row = case.source_rows[number - 1]
    return " ".join(row[h] for h in headers)


def _unrecoverable_cells(case: BenchmarkCase) -> set[tuple[str, str]]:
    lineage = case.manifest["row_lineage"]
    cells = set()
    for entry in case.manifest["issues"]:
        if not entry.get("unrecoverable"):
            continue
        if entry.get("rows") is not None:
            for row in entry["rows"]:
                cells.add((lineage[str(row)], entry["target"]))
        else:
            for truth_row in case.truth:
                cells.add((truth_row[case.key_field], entry["target"]))
    return cells


def _cell_metrics(
    case: BenchmarkCase,
    run: RunCase,
    alignment: _Alignment,
    unrecoverable: set[tuple[str, str]],
    overrides: set[tuple[str, str]],
    removed_spurious: int,
) -> tuple[dict, dict]:
    sources = {m["target"]: m["source"] for m in case.manifest["column_mapping"]}
    types = {f.name: f.type for f in case.contract.fields}
    counts = defaultdict(int)
    wrong_cells: list[dict] = []
    fabrications: list[dict] = []

    for truth_row in case.truth:
        key = truth_row[case.key_field]
        number = alignment.key_to_source[key]
        index = alignment.aligned.get(key)
        for field in case.contract.field_names:
            truth = truth_row[field]
            source = _source_text(case, number, field, sources)
            if index is None:
                output = None
            elif (key, field) in overrides:
                output = truth
            else:
                output = run.rows[index][field]
            correct = output is not None and cells_equal(types[field], output, truth)

            if (key, field) not in unrecoverable:
                counts["cells"] += 1
                counts["correct"] += correct
                if source == truth:
                    counts["clean"] += 1
                    counts["clean_kept"] += correct
                else:
                    counts["dirty"] += 1
                    counts["dirty_fixed"] += correct

            if index is None:
                continue
            counts["aligned_cells"] += 1
            if truth == "":
                counts["truth_nulls"] += 1
                if output and output != source:
                    counts["fabricated"] += 1
                    fabrications.append(_failure(case, number, key, field, source, output, truth))
            if not correct:
                wrong_cells.append({"index": index, **_failure(case, number, key, field, source, output, truth)})

    output_rows = len(run.rows) - removed_spurious
    metrics = {
        "dirty_cell_accuracy": ratio(counts["dirty_fixed"], counts["dirty"]),
        "clean_cell_preservation": ratio(counts["clean_kept"], counts["clean"]),
        "overall_cell_accuracy": ratio(counts["correct"], counts["cells"]),
        "row_recall": ratio(len(alignment.aligned), len(case.truth)),
        "spurious_row_rate": ratio(len(alignment.spurious) - removed_spurious, output_rows),
        "fabrication_rate": ratio(counts["fabricated"], counts["truth_nulls"]),
        "dirty_cells": counts["dirty"],
        "clean_cells": counts["clean"],
    }
    details = {"wrong_cells": wrong_cells, "fabrications": fabrications, "aligned_cells": counts["aligned_cells"]}
    return metrics, details


def _failure(case, number, key, field, source, output, truth) -> dict:
    return {"source_row": number, "key": key, "field": field, "source": source, "output": output, "truth": truth}


# Escalation --------------------------------------------------------------

def _covers(item: dict, instance: tuple) -> bool:
    _, where, row = instance
    item_rows = item.get("rows")
    item_groups = [frozenset(g) for g in item.get("groups") or []]
    if isinstance(row, frozenset):
        return row in item_groups or bool(item_rows and row & set(item_rows))
    if _where(item) != where:
        return False
    if row is None:
        return item_rows is None and not item_groups
    return item_rows is None or row in item_rows


def _escalated_cells(case: BenchmarkCase, items: list[dict]) -> tuple[set[tuple[str, str]], set[int]]:
    """Cells and source rows an oracle reviewer would resolve."""
    lineage = case.manifest["row_lineage"]
    cells: set[tuple[str, str]] = set()
    rows: set[int] = set()
    for item in items:
        for group in item.get("groups") or []:
            rows.update(group)
        field = item.get("target")
        if item.get("rows") is not None:
            rows.update(item["rows"])
        if field not in case.contract.field_names:
            continue
        if item.get("rows") is None and not item.get("groups"):
            cells.update((t[case.key_field], field) for t in case.truth)
        else:
            for row in item.get("rows") or []:
                key = lineage.get(str(row))
                if key and key != "drop":
                    cells.add((key, field))
    return cells, rows


def score_escalation(case: BenchmarkCase, result: dict, drop_rows: set[int]) -> tuple[dict, list[dict]]:
    items = result.get("escalations", [])
    review = set()
    for entry in case.manifest["issues"]:
        if entry["risk"] == "review":
            review |= instance_keys(entry, drop_rows)

    covered = {inst for inst in review if any(_covers(item, inst) for item in items)}
    useful = sum(1 for item in items if any(_covers(item, inst) for inst in review))
    actions = result.get("actions", [])
    escalated_actions = sum(1 for a in actions if a.get("status") == "escalated")

    missed = [
        {"issue": inst[0], "field": inst[1], "rows": sorted(inst[2]) if isinstance(inst[2], frozenset) else inst[2]}
        for inst in sorted(review - covered, key=repr)
    ]
    metrics = {
        "escalation_rate": ratio(escalated_actions, len(actions)),
        "precision": ratio(useful, len(items)),
        "recall": ratio(len(covered), len(review)),
        "missed_reviews": len(review - covered),
        "review_instances": len(review),
        "escalations": len(items),
    }
    return metrics, missed


# Execution ---------------------------------------------------------------

def score_execution(result: dict) -> dict:
    actions = result.get("actions", [])
    valid = [a for a in actions if a.get("valid")]
    succeeded = [a for a in valid if a.get("succeeded")]
    needed_repair = [a for a in valid if a.get("revisions", 0) > 0 or not a.get("succeeded")]
    recovered = [a for a in needed_repair if a.get("revisions", 0) > 0 and a.get("succeeded")]
    return {
        "actions": len(actions),
        "action_validity_rate": ratio(len(valid), len(actions)),
        "execution_success_rate": ratio(len(succeeded), len(valid)),
        "repair_recovery_rate": ratio(len(recovered), len(needed_repair)),
        "mean_revisions_per_failed_action": ratio(
            sum(a.get("revisions", 0) for a in needed_repair), len(needed_repair)
        ),
    }


# Whole case --------------------------------------------------------------

def score_case(case: BenchmarkCase, run: RunCase, reference_customer_ids: frozenset[str] | None = None) -> dict:
    result = run.result
    alignment = _Alignment(case, run)
    unrecoverable = _unrecoverable_cells(case)

    transformation, details = _cell_metrics(case, run, alignment, unrecoverable, set(), 0)

    escalated_cells, escalated_rows = _escalated_cells(case, result.get("escalations", []))
    removed = sum(
        1 for index in alignment.spurious
        if run.source_rows[index] is not None and run.source_rows[index] in escalated_rows
    )
    oracle, _ = _cell_metrics(case, run, alignment, unrecoverable, escalated_cells, removed)

    foreign_keys = None
    if case.manifest["entity"] == "sales_order" and reference_customer_ids is not None:
        foreign_keys = {"customer_id": reference_customer_ids}
    violations = check_table(case.contract, run.rows, foreign_keys=foreign_keys)
    violated = {(v.row - 1, v.field) for v in violations}
    output_cells = len(run.rows) * len(case.contract.fields)
    silent = [c for c in details["wrong_cells"] if (c["index"], c["field"]) not in violated]

    escalation, missed = score_escalation(case, result, alignment.drop_rows)

    return {
        "case": case.id,
        "split": case.split,
        "entity": case.manifest["entity"],
        "mapping": score_mapping(case.manifest, result),
        "detection": score_detection(case.manifest["issues"], result.get("detections", []), alignment.drop_rows),
        "transformation": transformation,
        "transformation_oracle": oracle,
        "validation": {
            "contract_pass_rate": None if output_cells == 0 else 1 - len(violated) / output_cells,
            "silent_errors": len(silent),
            "silent_errors_per_1000": ratio(1000 * len(silent), details["aligned_cells"]),
            "fabrication_rate": transformation["fabrication_rate"],
            "fabrication_rate_oracle": oracle["fabrication_rate"],
        },
        "execution": score_execution(result),
        "escalation": escalation,
        "usage": {
            "llm_calls": result.get("usage", {}).get("llm_calls", 0),
            "input_tokens": result.get("usage", {}).get("input_tokens", 0),
            "output_tokens": result.get("usage", {}).get("output_tokens", 0),
            "seconds": result.get("usage", {}).get("seconds", 0.0),
        },
        "failures": {
            "missed_reviews": missed,
            "fabrications": details["fabrications"],
            "silent_errors": [{k: v for k, v in c.items() if k != "index"} for c in silent],
        },
    }


def score_run_case(case_dir: Path, run_dir: Path, reference_customer_ids=None) -> dict:
    from .loading import load_case, load_run_case

    case = load_case(case_dir)
    run = load_run_case(run_dir, case.contract)
    return score_case(case, run, reference_customer_ids)
