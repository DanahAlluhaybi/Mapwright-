"""Markdown reports from scores."""

from __future__ import annotations

from .aggregate import HEADLINE

SPLIT_LABELS = {"dev": "Development", "heldout": "Held-out"}
FAILURES_PER_CASE = 50


def fmt(value) -> str:
    if value is None:
        return "–"
    if isinstance(value, int):
        return str(value)
    if abs(value) >= 100:
        return f"{value:,.0f}"
    return f"{value:.3f}"


def summary_markdown(run_info: dict, summary: dict) -> str:
    splits = [s for s in ("dev", "heldout") if s in summary]
    lines = [f"# Results: {run_info.get('system', 'unknown system')}", ""]
    details = [f"{k}: {v}" for k, v in run_info.items() if k != "system"]
    if details:
        lines += ["; ".join(details), ""]

    lines += ["| Metric | " + " | ".join(SPLIT_LABELS[s] for s in splits) + " |"]
    lines += ["| --- |" + " --- |" * len(splits)]
    for label, _, _ in HEADLINE:
        lines.append(f"| {label} | " + " | ".join(fmt(summary[s]["headline"][label]) for s in splits) + " |")

    for split in splits:
        lines += ["", f"## Detection by issue type, {SPLIT_LABELS[split].lower()}", ""]
        lines += ["| Issue | Precision | Recall | F1 | TP | FP | FN |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for issue, m in summary[split]["detection_by_issue"].items():
            lines.append(
                f"| {issue} | {fmt(m['precision'])} | {fmt(m['recall'])} | {fmt(m['f1'])} "
                f"| {m['tp']} | {m['fp']} | {m['fn']} |"
            )
        lines += ["", f"Cases: {', '.join(summary[split]['cases'])}"]
    return "\n".join(lines) + "\n"


def failures_markdown(scores: list[dict]) -> str:
    lines = ["# Failures", ""]
    for score in scores:
        failures = score["failures"]
        if not any(failures.values()):
            continue
        lines += [f"## {score['case']}", ""]
        if failures["missed_reviews"]:
            lines += ["Missed reviews:", ""]
            for m in failures["missed_reviews"][:FAILURES_PER_CASE]:
                lines.append(f"- {m['issue']} on {m['field'] or 'rows'}: {m['rows'] if m['rows'] is not None else 'whole column'}")
            lines.append("")
        for title, key in (("Fabricated values", "fabrications"), ("Silent errors", "silent_errors")):
            items = failures[key]
            if not items:
                continue
            lines += [f"{title} ({len(items)}):", "", "| Row | Field | Source | Output | Truth |", "| --- | --- | --- | --- | --- |"]
            for f in items[:FAILURES_PER_CASE]:
                lines.append(f"| {f['source_row']} | {f['field']} | {f['source']} | {f['output']} | {f['truth']} |")
            if len(items) > FAILURES_PER_CASE:
                lines.append(f"| … | {len(items) - FAILURES_PER_CASE} more | | | |")
            lines.append("")
    return "\n".join(lines) + "\n"


def comparison_markdown(runs: list[tuple[str, dict]]) -> str:
    """Systems side by side. ``runs`` is a list of (system name, summary)."""
    lines = ["# Comparison", ""]
    for split in ("dev", "heldout"):
        present = [(name, s) for name, s in runs if split in s]
        if not present:
            continue
        lines += [f"## {SPLIT_LABELS[split]}", ""]
        lines += ["| Metric | " + " | ".join(name for name, _ in present) + " |"]
        lines += ["| --- |" + " --- |" * len(present)]
        for label, _, _ in HEADLINE:
            lines.append(f"| {label} | " + " | ".join(fmt(s[split]["headline"][label]) for _, s in present) + " |")
        lines.append("")
    return "\n".join(lines) + "\n"
