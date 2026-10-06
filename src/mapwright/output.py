"""Writing a run to disk.

<out>/target.csv     _source_row plus every contract field, canonical text
<out>/result.json    mapping, detections, actions, escalations, usage
<out>/plan.json      every step with its parameters, decision and effect
<out>/report.md      what was fixed, flagged and held, for a person to read
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from .pipeline import RunOutcome

SOURCE_ROW = "_source_row"


def write_target(path: Path, fields: list[str], rows: list[dict[str, str]], source_rows: list[int]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow([SOURCE_ROW, *fields])
        for number, row in zip(source_rows, rows):
            writer.writerow([number, *(row.get(name, "") for name in fields)])


def _json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def report(outcome: RunOutcome) -> str:
    statuses = Counter(s.status for s in outcome.steps)
    decisions = Counter(s.decision.kind for s in outcome.steps if s.decision)
    lines = [
        f"# Run report: {outcome.contract.entity}",
        "",
        f"Source rows: {len(outcome.source.rows)}. Output rows: {len(outcome.rows)}.",
        f"Encoding {outcome.source.encoding}, header on line {outcome.source.header_line}.",
        "",
        "## Mapping",
        "",
        "| Source | Target | Method |",
        "| --- | --- | --- |",
    ]
    for m in outcome.mapping.mappings:
        lines.append(f"| {' + '.join(m.sources)} | {m.target} | {m.method} |")
    for column in outcome.mapping.dropped:
        lines.append(f"| {column} | dropped | no match |")
    for target in outcome.mapping.missing:
        lines.append(f"| (none) | {target} | missing |")

    lines += [
        "",
        "## Actions",
        "",
        f"{len(outcome.steps)} actions: {decisions['apply']} applied, {decisions['flag']} applied and flagged "
        f"for sign-off, {decisions['hold']} held for approval, {statuses['failed']} failed.",
        "",
        "| Id | Action | Target | Decision | Cells | Rows removed | Why |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in outcome.steps:
        if s.action.type in ("map_column", "combine_columns", "drop_column"):
            continue
        summary = s.effect.summary()
        decision = s.decision.kind if s.decision else "failed"
        lines.append(
            f"| {s.action.id} | {s.action.type} | {', '.join(s.action.targets)} | {decision} | "
            f"{summary['cells_changed']} | {summary['rows_removed']} | {s.action.rationale} |"
        )

    escalations = outcome.escalations
    lines += ["", "## Needs a person", ""]
    if not escalations:
        lines.append("Nothing.")
    for item in escalations:
        where = item.get("groups") or item.get("rows") or "whole column"
        count = len(where) if isinstance(where, list) else where
        lines.append(f"- {item.get('issue')} on {item.get('target')}: {count}")
    return "\n".join(lines) + "\n"


def write_run(outcome: RunOutcome, directory: str | Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    write_target(directory / "target.csv", outcome.contract.field_names, outcome.rows, outcome.source_rows)
    _json(directory / "result.json", outcome.result())
    _json(directory / "plan.json", outcome.plan())
    (directory / "report.md").write_text(report(outcome), encoding="utf-8")
    return directory
