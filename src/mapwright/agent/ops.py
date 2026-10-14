"""The operations the agent can run on a table.

Each operation is plain data, ``{"op": "to_boolean", "column": "Active", ...}``,
so it can come from a button, from the rule-based request parser or from a
model, and is always checked here before anything runs. Nothing outside
this vocabulary can be executed, and no operation invents data: a value it
cannot convert is left as it was and reported.

``preview`` computes the exact effect without changing the table; ``apply``
returns a new table. The original upload is never modified.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field

from ..normalize import clean_spaces, infer_date_formats, match_key, normalize_phone, parse_date, parse_number, title_if_uniform
from .infer import FALSE_TOKENS, TRUE_TOKENS, UNITS, date_label, is_null
from .table import Table

EXAMPLES = 6


class OpError(ValueError):
    """The operation is not valid for this table."""


@dataclass
class Effect:
    changes: list[tuple[int, str, str, str]] = field(default_factory=list)  # row id, column, before, after
    removed: list[int] = field(default_factory=list)
    unmatched: Counter = field(default_factory=Counter)  # values left as they were
    columns_added: list[str] = field(default_factory=list)
    columns_removed: list[str] = field(default_factory=list)
    renamed: tuple[str, str] | None = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        examples, seen = [], set()
        for rid, column, before, after in self.changes:
            if (before, after) in seen:
                continue
            seen.add((before, after))
            examples.append({"row": rid, "column": column, "before": before, "after": after})
            if len(examples) == EXAMPLES:
                break
        return {
            "cells_changed": len(self.changes),
            "rows_changed": len({c[0] for c in self.changes}),
            "rows_removed": len(self.removed),
            "removed_rows": self.removed[:50],
            "examples": examples,
            "unmatched": [{"value": v, "count": c} for v, c in self.unmatched.most_common(10)],
            "unmatched_total": sum(self.unmatched.values()),
            "columns_added": self.columns_added,
            "columns_removed": self.columns_removed,
            "renamed": list(self.renamed) if self.renamed else None,
            "notes": self.notes,
        }


# Helpers -------------------------------------------------------------------


def _column(spec: dict, table: Table, key: str = "column") -> str:
    name = spec.get(key)
    if not isinstance(name, str) or name not in table.columns:
        raise OpError(f"no column {name!r}; columns are: {', '.join(table.columns)}")
    return name


def _map_cells(table: Table, column: str, convert) -> tuple[Table, Effect]:
    """Apply ``convert(value) -> new value | None`` to a column; None leaves the value and counts it."""
    out, effect, i = table.copy(), Effect(), table.index(column)
    for row, rid in zip(out.rows, out.row_ids):
        before = row[i]
        if not before.strip():
            continue
        after = convert(before)
        if after is None:
            effect.unmatched[before] += 1
        elif after != before:
            row[i] = after
            effect.changes.append((rid, column, before, after))
    return out, effect


# Operations ----------------------------------------------------------------


def _to_boolean(table: Table, spec: dict) -> tuple[Table, Effect]:
    column = _column(spec, table)
    true_values = {match_key(v) for v in spec.get("true_values") or TRUE_TOKENS}
    false_values = {match_key(v) for v in spec.get("false_values") or FALSE_TOKENS}
    if true_values & false_values:
        raise OpError(f"values cannot be both true and false: {', '.join(sorted(true_values & false_values))}")
    labels = (str(spec.get("true_label", "true")), str(spec.get("false_label", "false")))

    def convert(value: str):
        key = match_key(value)
        if key in true_values:
            return labels[0]
        if key in false_values:
            return labels[1]
        return None

    out, effect = _map_cells(table, column, convert)
    effect.notes.append(f"true: {', '.join(sorted(true_values))}; false: {', '.join(sorted(false_values))}")
    return out, effect


def _parse_date(table: Table, spec: dict) -> tuple[Table, Effect]:
    column = _column(spec, table)
    values = [v for v in table.column(column) if v.strip() and not is_null(v)]
    formats = spec.get("formats")
    if not formats:
        inference = infer_date_formats(values)
        formats = inference.formats
        if inference.ambiguous and spec.get("order") not in ("day_first", "month_first"):
            raise OpError(
                "the day and month order cannot be told from the values (for example "
                f"{values[0]!r}); say whether the day or the month comes first"
            )
        if spec.get("order") == "month_first":
            formats = [f.replace("%d", "\0").replace("%m", "%d").replace("\0", "%m") if "%d" in f and "%m" in f and f.index("%d") < f.index("%m") else f for f in formats]
        elif spec.get("order") == "day_first":
            formats = [f.replace("%m", "\0").replace("%d", "%m").replace("\0", "%d") if "%d" in f and "%m" in f and f.index("%m") < f.index("%d") else f for f in formats]
    if not formats:
        raise OpError(f"no date format reads the values of {column!r}")
    output = spec.get("output", "%Y-%m-%d")

    def convert(value: str):
        for f in formats:
            parsed = parse_date(value, f)
            if parsed:
                return parsed.strftime(output)
        return None

    out, effect = _map_cells(table, column, convert)
    effect.notes.append(f"read as {', '.join(date_label(f) for f in formats)}; written as {date_label(output)}")
    return out, effect


def _parse_number(table: Table, spec: dict) -> tuple[Table, Effect]:
    column = _column(spec, table)
    decimals = spec.get("decimals")
    if decimals is not None and (not isinstance(decimals, int) or not 0 <= decimals <= 6):
        raise OpError("decimals must be a whole number from 0 to 6")

    units: Counter = Counter()

    def convert(value: str):
        if is_null(value):
            return value
        parsed = parse_number(value, UNITS)
        if parsed is None:
            return None
        if parsed.unit:
            units[parsed.unit] += 1
        number = parsed.value
        if decimals is not None:
            return f"{number:.{decimals}f}"
        text = format(number.normalize(), "f")
        return text

    out, effect = _map_cells(table, column, convert)
    effect.notes.append("separators, Arabic digits and K/M suffixes removed")
    if units:
        effect.notes.append("currency removed from " + ", ".join(f"{c} values in {u}" for u, c in units.items()))
        if len(units) > 1:
            effect.notes.append("the column mixes currencies; the numbers are not comparable until converted")
    return out, effect


def _normalize_text(table: Table, spec: dict) -> tuple[Table, Effect]:
    column = _column(spec, table)
    case = spec.get("case", "none")
    if case not in ("none", "upper", "lower", "title"):
        raise OpError("case must be none, upper, lower or title")

    def convert(value: str):
        text = clean_spaces(value)
        if case == "upper":
            text = text.upper()
        elif case == "lower":
            text = text.lower()
        elif case == "title":
            text = title_if_uniform(text) if text == text.upper() or text == text.lower() else text
        return text

    return _map_cells(table, column, convert)


def _normalize_email(table: Table, spec: dict) -> tuple[Table, Effect]:
    column = _column(spec, table)
    return _map_cells(table, column, lambda v: v if is_null(v) else clean_spaces(v).lower())


def _normalize_phone(table: Table, spec: dict) -> tuple[Table, Effect]:
    column = _column(spec, table)
    out, effect = _map_cells(table, column, lambda v: normalize_phone(v) if not is_null(v) else None)
    effect.notes.append("international format (+country code); local numbers other than Saudi mobiles are left for review")
    return out, effect


def _map_values(table: Table, spec: dict) -> tuple[Table, Effect]:
    column = _column(spec, table)
    mapping = spec.get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        raise OpError("mapping must list the values to change, for example {'Ksa': 'SA'}")
    lookup = {match_key(str(k)): str(v) for k, v in mapping.items()}
    out, effect = _map_cells(table, column, lambda v: lookup.get(match_key(v), v))
    effect.unmatched.clear()
    return out, effect


def _set_missing(table: Table, spec: dict) -> tuple[Table, Effect]:
    column = _column(spec, table)
    placeholders = {match_key(v) for v in spec.get("values") or []}

    def convert(value: str):
        if is_null(value) or match_key(value) in placeholders:
            return ""
        return value

    return _map_cells(table, column, convert)


def _fill_missing(table: Table, spec: dict) -> tuple[Table, Effect]:
    """Only fills with a value the user chose, or a statistic of the column that is shown first."""
    column = _column(spec, table)
    strategy = spec.get("strategy")
    values = [v for v in table.column(column) if v.strip() and not is_null(v)]
    if strategy == "constant":
        fill = spec.get("value")
        if fill is None or str(fill) == "":
            raise OpError("give the value to fill with")
        fill = str(fill)
    elif strategy == "mode":
        if not values:
            raise OpError("the column has no values to take the most common from")
        fill = Counter(values).most_common(1)[0][0]
    elif strategy in ("median", "mean"):
        numbers = [float(p.value) for v in values if (p := parse_number(v)) is not None]
        if not numbers:
            raise OpError("the column has no numbers")
        result = statistics.median(numbers) if strategy == "median" else statistics.fmean(numbers)
        fill = f"{result:.2f}".rstrip("0").rstrip(".")
    else:
        raise OpError("strategy must be constant, mode, median or mean")
    out, effect, i = table.copy(), Effect(), table.index(column)
    for row, rid in zip(out.rows, out.row_ids):
        if not row[i].strip() or is_null(row[i]):
            effect.changes.append((rid, column, row[i], fill))
            row[i] = fill
    effect.notes.append(f"missing values filled with {fill!r} ({strategy}); these are estimates, not real data")
    return out, effect


def _rename_column(table: Table, spec: dict) -> tuple[Table, Effect]:
    column = _column(spec, table)
    new = str(spec.get("new_name") or "").strip()
    if not new:
        raise OpError("give the new column name")
    if new in table.columns and new != column:
        raise OpError(f"there is already a column {new!r}")
    out = table.copy()
    out.columns[out.index(column)] = new
    effect = Effect(renamed=(column, new))
    return out, effect


def _drop_column(table: Table, spec: dict) -> tuple[Table, Effect]:
    column = _column(spec, table)
    out, i = table.copy(), table.index(column)
    out.columns.pop(i)
    for row in out.rows:
        row.pop(i)
    return out, Effect(columns_removed=[column])


def _split_column(table: Table, spec: dict) -> tuple[Table, Effect]:
    column = _column(spec, table)
    separator = spec.get("separator")
    into = spec.get("into")
    if not isinstance(separator, str) or not separator:
        raise OpError("give the separator to split on")
    if not isinstance(into, list) or len(into) < 2 or not all(isinstance(n, str) and n.strip() for n in into):
        raise OpError("give at least two new column names")
    clash = [n for n in into if n in table.columns]
    if clash:
        raise OpError(f"columns already exist: {', '.join(clash)}")
    out, i, effect = table.copy(), table.index(column), Effect(columns_added=list(into))
    out.columns[i + 1 : i + 1] = into
    for row, rid in zip(out.rows, out.row_ids):
        parts = [p.strip() for p in row[i].split(separator, len(into) - 1)] if row[i].strip() else []
        if row[i].strip() and len(parts) < len(into):
            effect.unmatched[row[i]] += 1
        parts = (parts + [""] * len(into))[: len(into)]
        row[i + 1 : i + 1] = parts
        for name, part in zip(into, parts):
            if part:
                effect.changes.append((rid, name, "", part))
    return out, effect


def _row_filter(table: Table, spec: dict):
    column = _column(spec, table)
    i = table.index(column)
    condition = spec.get("where", "missing")
    if condition == "missing":
        return lambda row: not row[i].strip() or is_null(row[i]), f"{column} is missing"
    if condition == "equals":
        targets = {match_key(str(v)) for v in spec.get("values") or []}
        if not targets:
            raise OpError("give the values that mark rows to remove")
        return lambda row: match_key(row[i]) in targets, f"{column} is one of {', '.join(sorted(targets))}"
    raise OpError("where must be 'missing' or 'equals'")


def _drop_rows(table: Table, spec: dict) -> tuple[Table, Effect]:
    test, description = _row_filter(table, spec)
    out, effect = Table(list(table.columns), [], []), Effect()
    for row, rid in zip(table.rows, table.row_ids):
        if test(row):
            effect.removed.append(rid)
        else:
            out.rows.append(list(row))
            out.row_ids.append(rid)
    effect.notes.append(f"removes rows where {description}")
    return out, effect


def _deduplicate(table: Table, spec: dict) -> tuple[Table, Effect]:
    columns = spec.get("columns") or list(table.columns)
    for c in columns:
        _column({"column": c}, table)
    idx = [table.index(c) for c in columns]
    seen: dict[tuple, list[str]] = {}
    out, effect, conflicts = Table(list(table.columns), [], []), Effect(), 0
    for row, rid in zip(table.rows, table.row_ids):
        key = tuple(match_key(row[i]) for i in idx)
        if all(not k for k in key):
            out.rows.append(list(row))
            out.row_ids.append(rid)
            continue
        if key in seen:
            if [match_key(v) for v in seen[key]] != [match_key(v) for v in row]:
                conflicts += 1
            effect.removed.append(rid)
            continue
        seen[key] = row
        out.rows.append(list(row))
        out.row_ids.append(rid)
    effect.notes.append(f"keeps the first row for each {'whole row' if len(columns) == len(table.columns) else ', '.join(columns)}")
    if conflicts:
        effect.notes.append(f"{conflicts} removed rows differ from the kept row in other columns; that data would be lost")
    return out, effect


OPS = {
    "to_boolean": (_to_boolean, "review", "Convert {column} to yes/no values"),
    "parse_date": (_parse_date, "review", "Write {column} as dates in one format"),
    "parse_number": (_parse_number, "review", "Write {column} as plain numbers"),
    "normalize_text": (_normalize_text, "low", "Tidy the text in {column}"),
    "normalize_email": (_normalize_email, "low", "Lower-case and trim the emails in {column}"),
    "normalize_phone": (_normalize_phone, "review", "Write the phone numbers in {column} in international format"),
    "map_values": (_map_values, "review", "Replace values in {column}"),
    "set_missing": (_set_missing, "low", "Turn placeholders like 'N/A' in {column} into empty values"),
    "fill_missing": (_fill_missing, "destructive", "Fill the missing values in {column}"),
    "rename_column": (_rename_column, "low", "Rename {column} to {new_name}"),
    "drop_column": (_drop_column, "destructive", "Remove the column {column}"),
    "split_column": (_split_column, "review", "Split {column} into {into}"),
    "drop_rows": (_drop_rows, "destructive", "Remove rows where {column} is {where}"),
    "deduplicate": (_deduplicate, "destructive", "Remove duplicate rows"),
}

RISK_TEXT = {
    "low": "Formatting only; no information is lost.",
    "review": "Changes values; check the examples before approving.",
    "destructive": "Removes or fills in data; it will only run after you approve it.",
}


def check(spec: object) -> dict:
    if not isinstance(spec, dict) or spec.get("op") not in OPS:
        raise OpError(f"unknown operation; known: {', '.join(OPS)}")
    allowed = {"op", "column", "columns", "true_values", "false_values", "true_label", "false_label", "formats",
               "order", "output", "decimals", "case", "mapping", "values", "strategy", "value", "new_name",
               "separator", "into", "where"}
    extra = set(spec) - allowed
    if extra:
        raise OpError(f"unknown settings: {', '.join(sorted(extra))}")
    return spec


def describe(spec: dict) -> str:
    template = OPS[spec["op"]][2]
    values = {k: (", ".join(v) if isinstance(v, list) else v) for k, v in spec.items()}
    values.setdefault("new_name", "")
    values.setdefault("into", "")
    values.setdefault("where", "missing")
    values.setdefault("column", ", ".join(spec.get("columns") or []) or "all columns")
    return template.format(**values)


def run(table: Table, spec: dict) -> tuple[Table, Effect]:
    check(spec)
    return OPS[spec["op"]][0](table, spec)


def preview(table: Table, spec: dict) -> dict:
    _, effect = run(table, spec)
    risk = OPS[spec["op"]][1]
    summary = effect.summary()
    if summary["unmatched_total"]:
        summary["notes"].append(f"{summary['unmatched_total']} values could not be converted and stay as they are")
    return {"op": spec, "title": describe(spec), "risk": risk, "risk_text": RISK_TEXT[risk], "effect": summary}


def replay(table: Table, plan: list[dict]) -> Table:
    for spec in plan:
        table, _ = run(table, spec)
    return table
