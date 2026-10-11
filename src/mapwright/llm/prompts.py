"""Prompt text, versioned.

The version string is part of every cache key, so changing a prompt
without bumping its version would silently replay stale answers. Prompts
were written against development cases only.

What the model sees is bounded: column names, profiles, up to 50 frequent
values plus up to 20 others per column. Never the full table.
"""

from __future__ import annotations

import json
import random

from ..contracts import Contract, Field
from ..profile import ColumnProfile

MAPPING_VERSION = "map-v1"
VALUES_VERSION = "values-v2"
REVISE_VERSION = "revise-v1"

MAX_FREQUENT = 50
MAX_OTHERS = 20
MAX_REFERENCE = 60

SYSTEM = """You help migrate messy legacy data exports into a fixed target schema.
You never write code, SQL or formulas. You answer with one JSON object that
follows the schema given in the request, and nothing else.
When you are not sure, say so with a low confidence or leave the item out:
a person reviews uncertain items, and an invented answer is worse than none."""


def field_spec(contract: Contract, f: Field) -> dict:
    spec: dict = {"name": f.name, "type": f.type, "required": f.required}
    if f.canonical:
        spec["canonical_form"] = f.canonical
    if f.pattern:
        spec["pattern"] = f.pattern
    if f.allowed_values:
        spec["allowed_values"] = list(f.allowed_values)
    reference = sorted(contract.reference_values.get(f.name, ()))
    if reference:
        spec["allowed_values"] = reference[:MAX_REFERENCE]
    if f.min is not None or f.max is not None:
        spec["range"] = [f.min, f.max]
    return spec


def value_sample(values: list[str], seed: str) -> list[str]:
    """Distinct non-blank values: the most frequent first, then a few others."""
    counts: dict[str, int] = {}
    for value in values:
        if value.strip():
            counts[value] = counts.get(value, 0) + 1
    frequent = sorted(counts, key=lambda v: (-counts[v], v))
    shown = frequent[:MAX_FREQUENT]
    rest = frequent[MAX_FREQUENT:]
    rng = random.Random(seed)
    shown += sorted(rng.sample(rest, min(MAX_OTHERS, len(rest))))
    return shown


def column_view(name: str, profile: ColumnProfile, values: list[str]) -> dict:
    return {
        "column": name,
        "rows": profile.rows,
        "blank": profile.blank,
        "distinct": profile.distinct,
        "shapes": [s for s, _ in profile.signatures],
        "values": value_sample(values, name)[:25],
    }


def mapping_prompt(contract: Contract, columns: list[dict], open_fields: list[str], taken: dict[str, list[str]]) -> str:
    request = {
        "task": (
            "Decide which target field each source column carries. A target field may be built from several "
            "source columns (for example first and last name), listed in reading order. Each source column is "
            "used at most once and each target field at most once. Columns that carry none of the open fields "
            "are dropped. Only use the open fields."
        ),
        "target_entity": contract.entity,
        "open_fields": [field_spec(contract, contract.field(n)) for n in open_fields],
        "already_mapped": taken,
        "source_columns": columns,
        "answer_schema": {
            "mappings": [{"source": ["<column>"], "target": "<open field>", "confidence": "0..1", "reason": "<short>"}],
            "drop": ["<column>"],
        },
    }
    return json.dumps(request, ensure_ascii=False, indent=1)


VALUE_ACTIONS = {
    "map_values": {"mapping": {"<value exactly as shown>": "<allowed value>"}},
    "set_null": {"tokens": ["<value exactly as shown that stands for a missing value>"]},
    "parse_date": {"formats": ["<Python strptime format, e.g. %d/%m/%Y>"]},
    "parse_number": {"units": {"<currency token as written, e.g. SR>": "<ISO code>"}},
    "normalize_text": {"case": "keep | upper | lower | title_if_uniform"},
    "normalize_phone": {},
}


def allowed_action_types(f: Field, closed: bool) -> list[str]:
    if f.type == "date":
        return ["set_null", "parse_date"]
    if f.type == "decimal":
        return ["set_null", "parse_number"]
    if closed:
        return ["set_null", "map_values"]
    if f.name == "phone":
        return ["set_null", "normalize_phone"]
    return ["set_null", "normalize_text"]


VALUES_TASK = (
    "Each entry below is one target field with values from the source column that feeds it. For each "
    "field, propose actions, chosen only from that field's allowed action types, that turn its values "
    "into the field's canonical form. Keys of map_values and set_null tokens must be copied exactly from "
    "that field's values. Map a value only when you are confident what it means; list the rest under "
    "unresolved. Leave a field's actions empty when its values are already canonical."
)


def field_request(contract: Contract, f: Field, values: list[str], kinds: list[str], context: str) -> dict:
    return {
        "field": field_spec(contract, f),
        "context": context,
        "values": values,
        "allowed_actions": {k: VALUE_ACTIONS[k] for k in kinds},
    }


def values_prompt(contract: Contract, fields: list[dict]) -> str:
    """One request for every field that needs actions, to keep the number of calls small."""
    request = {
        "task": VALUES_TASK,
        "fields": fields,
        "answer_schema": {
            "fields": {
                "<field name>": {
                    "actions": [
                        {"type": "<allowed action>", "...": "parameters", "confidence": "0..1", "reason": "<short>"}
                    ],
                    "unresolved": ["<value>"],
                }
            }
        },
    }
    return json.dumps(request, ensure_ascii=False, indent=1)


def revise_prompt(original: dict, action: dict, problems: list[str], still_wrong: list[str]) -> str:
    request = {
        "task": (
            "Your previous action did not pass validation. Return a corrected action of the same type, "
            "or an empty actions list if the values cannot be fixed safely."
        ),
        "original_request": original,
        "previous_action": action,
        "validation_problems": problems[:20],
        "values_still_wrong": still_wrong[:30],
        "answer_schema": {"actions": [{"type": action.get("type"), "...": "parameters", "confidence": "0..1"}]},
    }
    return json.dumps(request, ensure_ascii=False, indent=1)
