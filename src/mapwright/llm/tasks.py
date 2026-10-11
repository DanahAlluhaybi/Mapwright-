"""The two questions the model is asked, and strict checks on its answers.

1. Column mapping: which target field, if any, does each unmapped source
   column hold?
2. Value repair: what does each value the rules could not handle mean,
   in the target's canonical form?

The model sees field definitions from the contract, column profiles and
distinct values. It never sees whole rows. Answers that name a column,
field or value that was not asked about are discarded and counted as
invalid.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..contracts import Contract, Field
from ..profile import ColumnProfile
from .provider import Request, parse_json

PROMPT_VERSION = "1"
VALUE_KINDS = ("synonym", "format", "missing", "unknown")
MAX_EXAMPLES = 15

MAPPING_SYSTEM = """\
You are one step in a data onboarding pipeline. You map columns of a legacy
export to fields of a target schema.

Rules:
- Use only the target fields listed. If no listed field fits a column, use null.
- Map a column only when its values clearly hold that field. Headers may be
  abbreviated, in another language or transliterated; judge by the values too.
- Each target field receives at most one column.
- confidence is your probability, from 0 to 1, that the mapping is right.

Answer with one JSON object and nothing else:
{"mappings": [{"column": "<header exactly as given>", "target": "<field name or null>",
  "confidence": <0..1>, "reason": "<a few words>"}]}
Include every column you were given."""

VALUES_SYSTEM = """\
You are one step in a data onboarding pipeline. Deterministic rules have
already cleaned what they could. The values below are the ones they could not
handle. For each value, decide what it is:

- "synonym": it means one of the field's allowed values. replacement is that
  allowed value, spelled exactly as listed.
- "format": it is the right data written in another format. replacement is
  the same data in the field's canonical form, as described.
- "missing": it is a placeholder for no data, such as "N/A" or "TBD".
  replacement is "".
- "unknown": you cannot tell, or the value does not contain the information.
  replacement is null.

Never invent data. If the value does not say what the field needs, answer
"unknown". Do not decide whether 03/04 means March or April: dates whose day
and month order is unclear are "unknown".

confidence is your probability, from 0 to 1, that your answer is right.

Answer with one JSON object and nothing else:
{"fields": [{"field": "<field name>", "values": [{"value": "<value exactly as given>",
  "kind": "synonym|format|missing|unknown", "replacement": "<text, empty string or null>",
  "confidence": <0..1>}]}]}
Include every value you were given."""

REVISION_SYSTEM = (
    VALUES_SYSTEM
    + """

This is a second look. Your earlier replacements below were rejected by the
target schema's checks, for the reasons given. Give a corrected answer for
each, or "unknown" if you cannot produce a valid value."""
)


def field_spec(contract: Contract, f: Field) -> dict:
    spec: dict = {"field": f.name, "type": f.type}
    if f.canonical:
        spec["canonical_form"] = f.canonical
    allowed = f.allowed_values or tuple(sorted(contract.reference_values.get(f.name, ())))
    if allowed:
        spec["allowed_values"] = list(allowed)
    if f.pattern:
        spec["pattern"] = f.pattern
    if f.type == "decimal":
        spec["canonical_form"] = (spec.get("canonical_form", "") + " Two decimal places, no separators.").strip()
    if f.type == "date":
        spec["canonical_form"] = (spec.get("canonical_form", "") + " YYYY-MM-DD.").strip()
    return spec


def column_brief(profile: ColumnProfile) -> dict:
    return {
        "column": profile.name,
        "examples": [value for value, _ in profile.top_values[:MAX_EXAMPLES]],
        "blank_share": round(profile.blank / profile.rows, 2) if profile.rows else 0.0,
        "distinct_values": profile.distinct,
        "shapes": [shape for shape, _ in profile.signatures[:3]],
    }


def _confidence(value) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


# Column mapping ----------------------------------------------------------


@dataclass(frozen=True)
class ColumnProposal:
    column: str
    target: str | None
    confidence: float
    reason: str = ""


def mapping_request(contract: Contract, columns: list[ColumnProfile], fields: list[Field]) -> Request:
    payload = {
        "entity": contract.entity,
        "target_fields": [field_spec(contract, f) for f in fields],
        "columns": [column_brief(p) for p in columns],
    }
    return Request("map_columns", MAPPING_SYSTEM, json.dumps(payload, ensure_ascii=False, indent=1), PROMPT_VERSION)


def parse_mapping(text: str, columns: list[str], fields: list[str]) -> tuple[list[ColumnProposal], int]:
    """Valid proposals, best first per field, and the number of invalid entries."""
    data = parse_json(text)
    entries = data.get("mappings")
    if not isinstance(entries, list):
        raise ValueError("no mappings list")

    invalid = 0
    best: dict[str, ColumnProposal] = {}
    seen_columns: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            invalid += 1
            continue
        column, target = entry.get("column"), entry.get("target")
        if column not in columns or column in seen_columns or (target is not None and target not in fields):
            invalid += 1
            continue
        seen_columns.add(column)
        if target is None:
            continue
        proposal = ColumnProposal(column, target, _confidence(entry.get("confidence")), str(entry.get("reason", "")))
        if target not in best or proposal.confidence > best[target].confidence:
            best[target] = proposal
    return sorted(best.values(), key=lambda p: -p.confidence), invalid


# Value repair ------------------------------------------------------------


@dataclass(frozen=True)
class ValueProposal:
    field: str
    value: str
    kind: str
    replacement: str | None
    confidence: float


def values_request(contract: Contract, values: dict[str, list[tuple[str, int]]]) -> Request:
    payload = {
        "entity": contract.entity,
        "fields": [
            {**field_spec(contract, contract.field(name)), "values": [{"value": v, "count": c} for v, c in items]}
            for name, items in values.items()
        ],
    }
    return Request("repair_values", VALUES_SYSTEM, json.dumps(payload, ensure_ascii=False, indent=1), PROMPT_VERSION)


def revision_request(contract: Contract, name: str, rejected: list[tuple[str, str, list[str]]]) -> Request:
    """``rejected`` holds (value, rejected replacement, reasons)."""
    payload = {
        "entity": contract.entity,
        "fields": [
            {
                **field_spec(contract, contract.field(name)),
                "values": [
                    {"value": value, "rejected_replacement": replacement, "rejected_because": reasons}
                    for value, replacement, reasons in rejected
                ],
            }
        ],
    }
    return Request("revise_values", REVISION_SYSTEM, json.dumps(payload, ensure_ascii=False, indent=1), PROMPT_VERSION)


def parse_values(text: str, asked: dict[str, set[str]]) -> tuple[list[ValueProposal], int]:
    """Valid proposals and the number of invalid entries. ``asked`` maps each
    field to the values that were sent."""
    data = parse_json(text)
    groups = data.get("fields")
    if not isinstance(groups, list):
        raise ValueError("no fields list")

    proposals: list[ValueProposal] = []
    invalid = 0
    seen: set[tuple[str, str]] = set()
    for group in groups:
        name = group.get("field") if isinstance(group, dict) else None
        if name not in asked or not isinstance(group.get("values"), list):
            invalid += 1
            continue
        for entry in group["values"]:
            if not isinstance(entry, dict):
                invalid += 1
                continue
            value, kind, replacement = entry.get("value"), entry.get("kind"), entry.get("replacement")
            if value not in asked[name] or (name, value) in seen or kind not in VALUE_KINDS:
                invalid += 1
                continue
            if kind in ("synonym", "format") and not (isinstance(replacement, str) and replacement.strip()):
                invalid += 1
                continue
            seen.add((name, value))
            if kind == "missing":
                replacement = ""
            elif kind == "unknown":
                replacement = None
            proposals.append(ValueProposal(name, value, kind, replacement, _confidence(entry.get("confidence"))))
    return proposals, invalid
