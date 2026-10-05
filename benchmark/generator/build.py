"""Turns a case definition into source.csv, ground_truth.csv and manifest.json."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
from dataclasses import dataclass
from pathlib import Path

import yaml

from mapwright.canonical import to_text
from mapwright.contracts import Contract, load_contracts

from . import records
from .render import Context
from .spec import Case, Column

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = load_contracts(ROOT / "contracts")
TAXONOMY = {
    issue["id"]: issue
    for issue in yaml.safe_load((ROOT / "benchmark" / "issue_types.yaml").read_text("utf-8"))["issues"]
}

# Cases whose correct value cannot be recovered from the source. The ground
# truth holds null there and the right behavior is to escalate.
UNRECOVERABLE = {"S3", "M6"}

REFERENCE_CUSTOMER_SEED = 1000
REFERENCE_CUSTOMER_COUNT = 400


def reference_customers() -> list[dict]:
    """The clean customer table that order cases reference."""
    return records.customers(REFERENCE_CUSTOMER_COUNT, random.Random(REFERENCE_CUSTOMER_SEED))


@dataclass
class SourceRow:
    cells: dict[str, str]
    gt_index: int | None
    kind: str  # original, exact_duplicate, conflicting_duplicate, orphan
    pair: "SourceRow | None" = None


@dataclass
class BuiltCase:
    case: Case
    header: list[str]
    rows: list[SourceRow]
    ground_truth: list[dict[str, str]]
    manifest: dict


def _field_headers(case: Case) -> dict[str, str]:
    return {c.field: c.header for c in case.columns if c.field and c.split_part is None}


def _render_row(
    case: Case,
    gt_row: dict,
    rng: random.Random,
    headers: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Render one clean record. Returns the cells and {field: issue} for
    every cell a column format changed."""
    cells: dict[str, str] = {}
    changed: dict[str, str] = {}
    for column in case.columns:
        if column.extra:
            cells[column.header] = column.extra(rng)
            continue

        value = gt_row[column.field]
        canonical = to_text(value)

        if column.split_part is not None:
            first, _, rest = canonical.partition(" ")
            cells[column.header] = first if column.split_part == 0 else rest
            continue

        if value is None or column.fmt is None:
            cells[column.header] = canonical
            continue

        ctx = Context(rng, case.pool, gt_row, cells, headers, column.field)
        text = column.fmt(value, ctx)
        cells[column.header] = text
        if text != canonical:
            if column.fmt_issue:
                changed[column.field] = column.fmt_issue
            elif column.mapping_issue != "M4":
                raise ValueError(f"{case.id}: format on {column.header} changed a value without an issue")
    return cells, changed


def _conflicting_copy(cells: dict[str, str], gt_row: dict, headers: dict[str, str], rng: random.Random) -> dict[str, str]:
    copy = dict(cells)
    if gt_row.get("email") and "email" in headers:
        local, domain = gt_row["email"].split("@")
        copy[headers["email"]] = f"{local}{rng.randint(100, 999)}@{domain}"
    else:
        copy[headers["phone"]] = "+9665" + "".join(str(rng.randint(0, 9)) for _ in range(8))
    return copy


def _insert_after(rows: list[SourceRow], anchor: SourceRow, new: SourceRow, rng: random.Random) -> None:
    position = next(i for i, row in enumerate(rows) if row is anchor)
    rows.insert(rng.randint(position + 1, len(rows)), new)


def build_case(case: Case) -> BuiltCase:
    rng = random.Random(case.seed)
    contract: Contract = CONTRACTS[case.entity]
    headers = _field_headers(case)

    if case.entity == "customer":
        gt = records.customers(case.rows, rng, arabic_share=case.arabic_share, max_day=case.max_day)
    else:
        customer_ids = [row["customer_id"] for row in reference_customers()]
        gt = records.orders(case.rows, rng, customer_ids, max_day=case.max_day)

    covered = {c.field for c in case.columns if c.field}
    missing = [name for name in contract.field_names if name not in covered]
    for row in gt:
        for name in missing:
            row[name] = None

    # (gt index, field) -> issue. A later injection on the same cell wins,
    # but injections never stack: a cell already corrupted is left alone.
    cell_issues: dict[tuple[int, str], str] = {}
    injected: set[tuple[int, str]] = set()
    column_level: dict[tuple[str, str], str] = {}

    source: list[SourceRow] = []
    for index, gt_row in enumerate(gt):
        cells, changed = _render_row(case, gt_row, rng, headers)
        for name, issue in changed.items():
            if TAXONOMY[issue]["unit"] == "column":
                column_level[(issue, name)] = headers[name]
            else:
                cell_issues[(index, name)] = issue
        source.append(SourceRow(cells, index, "original"))

    header_fields = {header: name for name, header in headers.items()}
    for injection in case.injections:
        for index, row in enumerate(source):
            hit = rng.random() < injection.rate
            if not hit or (index, injection.field) in injected:
                continue
            before = dict(row.cells)
            ctx = Context(rng, case.pool, gt[index], row.cells, headers, injection.field)
            if not injection.inject(ctx):
                continue
            for header, text in row.cells.items():
                if text != before[header]:
                    name = header_fields[header]
                    cell_issues[(index, name)] = injection.issue
                    injected.add((index, name))

    originals = list(source)
    for original in originals:
        if rng.random() < case.exact_duplicate_rate:
            copy = SourceRow(dict(original.cells), None, "exact_duplicate", pair=original)
            _insert_after(source, original, copy, rng)
    for original in originals:
        if rng.random() < case.conflicting_duplicate_rate:
            cells = _conflicting_copy(original.cells, gt[original.gt_index], headers, rng)
            copy = SourceRow(cells, None, "conflicting_duplicate", pair=original)
            _insert_after(source, original, copy, rng)

    if case.orphan_rate:
        count = round(case.rows * case.orphan_rate)
        orphan_ids = [f"C-{900001 + n:06d}" for n in range(count)]
        orphans = records.orders(count, rng, orphan_ids, start_id=800001, max_day=case.max_day)
        for orphan in orphans:
            for name in missing:
                orphan[name] = None
            cells, _ = _render_row(case, orphan, rng, headers)
            source.insert(rng.randint(0, len(source)), SourceRow(cells, None, "orphan"))

    number = {id(row): n for n, row in enumerate(source, start=1)}
    original_number = {row.gt_index: number[id(row)] for row in source if row.kind == "original"}

    ground_truth = [{name: to_text(row[name]) for name in contract.field_names} for row in gt]
    key = contract.primary_key[0]

    lineage = {}
    for row in source:
        lineage[str(number[id(row)])] = ground_truth[row.gt_index][key] if row.kind == "original" else "drop"

    manifest = {
        "dataset_id": case.id,
        "entity": case.entity,
        "split": case.split,
        "seed": case.seed,
        "style": case.style,
        "pool": case.pool,
        "description": case.description,
        "encoding": case.encoding,
        "header_line": len(case.title_rows) + 1,
        "source_rows": len(source),
        "target_rows": len(ground_truth),
        "column_mapping": _column_mapping(case),
        "dropped_columns": [c.header for c in case.columns if c.extra],
        "missing_target_columns": missing,
        "issues": _issues(case, headers, missing, column_level, cell_issues, original_number, source, number),
        "row_lineage": lineage,
    }
    return BuiltCase(case, [c.header for c in case.columns], source, ground_truth, manifest)


def _column_mapping(case: Case) -> list[dict]:
    mapping: dict[str, list[str]] = {}
    for column in case.columns:
        if column.field:
            mapping.setdefault(column.field, []).append(column.header)
    return [{"source": sources, "target": target} for target, sources in mapping.items()]


def _entry(issue: str, target: str | None, columns: list[str], **extra) -> dict:
    entry = {
        "issue": issue,
        "target": target,
        "columns": columns,
        "unit": TAXONOMY[issue]["unit"],
        "risk": TAXONOMY[issue]["risk"],
    }
    if issue in UNRECOVERABLE:
        entry["unrecoverable"] = True
    entry.update(extra)
    return entry


def _issues(case, headers, missing, column_level, cell_issues, original_number, source, number) -> list[dict]:
    entries: list[dict] = []

    split_sources: dict[str, list[str]] = {}
    for column in case.columns:
        if column.extra:
            entries.append(_entry("M5", None, [column.header]))
        elif column.split_part is not None:
            split_sources.setdefault(column.field, []).append(column.header)
        elif column.mapping_issue:
            entries.append(_entry(column.mapping_issue, column.field, [column.header]))
    for target, columns in split_sources.items():
        entries.append(_entry("M3", target, columns))
    for name in missing:
        entries.append(_entry("M6", name, []))

    for (issue, name), header in sorted(column_level.items()):
        entries.append(_entry(issue, name, [header]))

    grouped: dict[tuple[str, str], list[int]] = {}
    for (index, name), issue in cell_issues.items():
        grouped.setdefault((issue, name), []).append(original_number[index])
    for (issue, name), rows in sorted(grouped.items()):
        entries.append(_entry(issue, name, [headers[name]], rows=sorted(rows)))

    key_header = headers.get(CONTRACTS[case.entity].primary_key[0])
    exact = [[number[id(r.pair)], number[id(r)]] for r in source if r.kind == "exact_duplicate"]
    conflicting = [[number[id(r.pair)], number[id(r)]] for r in source if r.kind == "conflicting_duplicate"]
    orphans = [number[id(r)] for r in source if r.kind == "orphan"]
    if exact:
        entries.append(_entry("S2", None, [], groups=sorted(exact)))
    if conflicting:
        entries.append(_entry("S8", "customer_id", [key_header], groups=sorted(conflicting)))
    if orphans:
        entries.append(_entry("S7", "customer_id", [headers["customer_id"]], rows=sorted(orphans)))

    return entries


def _csv_text(header: list[str], rows: list[list[str]], title_rows: tuple[str, ...] = ()) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for title in title_rows:
        writer.writerow([title])
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def write_case(built: BuiltCase, directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    case = built.case

    source_text = _csv_text(
        built.header,
        [[row.cells[h] for h in built.header] for row in built.rows],
        case.title_rows,
    )
    contract = CONTRACTS[case.entity]
    truth_text = _csv_text(
        contract.field_names,
        [[row[name] for name in contract.field_names] for row in built.ground_truth],
    )

    paths = [directory / "source.csv", directory / "ground_truth.csv", directory / "manifest.json"]
    paths[0].write_bytes(source_text.encode(case.encoding))
    paths[1].write_bytes(truth_text.encode("utf-8"))
    paths[2].write_bytes(
        (json.dumps(built.manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    return paths


def write_reference_customers(path: Path) -> Path:
    contract = CONTRACTS["customer"]
    rows = [[to_text(row[name]) for name in contract.field_names] for row in reference_customers()]
    path.write_bytes(_csv_text(contract.field_names, rows).encode("utf-8"))
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
