"""Combines per-case scores into per-split summaries.

Ratios are macro-averaged across cases (each case counts once, whatever
its size). Detection is also micro-averaged per issue type by summing
true and false positives across cases, because many issue types appear in
only one or two cases.
"""

from __future__ import annotations

from collections import defaultdict

from .scorer import prf

HEADLINE = [
    ("Dirty-cell accuracy", ("transformation", "dirty_cell_accuracy"), "mean"),
    ("Dirty-cell accuracy, oracle reviewer", ("transformation_oracle", "dirty_cell_accuracy"), "mean"),
    ("Clean-cell preservation", ("transformation", "clean_cell_preservation"), "mean"),
    ("Overall cell accuracy", ("transformation", "overall_cell_accuracy"), "mean"),
    ("Row recall", ("transformation", "row_recall"), "mean"),
    ("Spurious row rate", ("transformation", "spurious_row_rate"), "mean"),
    ("Silent errors per 1,000 cells", ("validation", "silent_errors_per_1000"), "mean"),
    ("Fabrication rate", ("validation", "fabrication_rate"), "mean"),
    ("Contract pass rate", ("validation", "contract_pass_rate"), "mean"),
    ("Mapping F1", ("mapping", "f1"), "mean"),
    ("Detection F1", ("detection", "overall", "f1"), "mean"),
    ("Detection F1, location only", ("detection", "location_only", "f1"), "mean"),
    ("Escalation precision", ("escalation", "precision"), "mean"),
    ("Escalation recall", ("escalation", "recall"), "mean"),
    ("Missed reviews", ("escalation", "missed_reviews"), "sum"),
    ("Action validity rate", ("execution", "action_validity_rate"), "mean"),
    ("Execution success rate", ("execution", "execution_success_rate"), "mean"),
    ("Repair recovery rate", ("execution", "repair_recovery_rate"), "mean"),
    ("LLM calls per case", ("usage", "llm_calls"), "mean"),
    ("Input tokens per case", ("usage", "input_tokens"), "mean"),
    ("Output tokens per case", ("usage", "output_tokens"), "mean"),
    ("Seconds per case", ("usage", "seconds"), "mean"),
]


def _get(score: dict, path: tuple[str, ...]):
    value = score
    for part in path:
        value = value.get(part) if isinstance(value, dict) else None
    return value


def summarize(scores: list[dict]) -> dict:
    by_split: dict[str, list[dict]] = defaultdict(list)
    for score in scores:
        by_split[score["split"]].append(score)

    summary = {}
    for split, items in sorted(by_split.items()):
        headline = {}
        for label, path, how in HEADLINE:
            values = [v for v in (_get(s, path) for s in items) if v is not None]
            if not values:
                headline[label] = None
            elif how == "sum":
                headline[label] = sum(values)
            else:
                headline[label] = sum(values) / len(values)

        totals: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        for s in items:
            for issue, counts in s["detection"]["by_issue"].items():
                totals[issue][0] += counts["tp"]
                totals[issue][1] += counts["fp"]
                totals[issue][2] += counts["fn"]

        summary[split] = {
            "cases": [s["case"] for s in items],
            "headline": headline,
            "detection_by_issue": {issue: prf(*t) for issue, t in sorted(totals.items())},
        }
    return summary
