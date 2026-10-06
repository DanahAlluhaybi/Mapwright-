"""The rules-only planner.

For each target field, looks at the staged values and proposes typed
actions from the rule dictionaries and parsers. Planners are generators:
the pipeline executes each action before asking for the next, so a later
proposal always sees the data as the earlier ones left it.

What the rules cannot settle is never guessed. It is reported as a
``Finding`` and sent to a person.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Iterator

from .actions import (
    Action,
    Deduplicate,
    DropRows,
    MapValues,
    MoveValue,
    NormalizePhone,
    NormalizeText,
    ParseDate,
    ParseNumber,
    SetNull,
    Workspace,
)
from .contracts import Contract, Field, check_value
from .dictionaries import ValueDictionary, missing_tokens, unit_tokens, value_dictionary
from .normalize import (
    FORMATS_BY_PATTERN,
    infer_date_formats,
    is_email,
    match_key,
    normalize_phone,
)

MIN_PHONE_DIGITS = 8


@dataclass
class Finding:
    """Something the rules detected but did not fix."""

    issue: str
    target: str | None
    rows: list[int] | None = None
    groups: list[list[int]] | None = None
    note: str = ""
    escalate: bool = True

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Context:
    contract: Contract
    ws: Workspace
    references: dict[str, frozenset[str]]
    missing: frozenset[str] = frozenset()


Proposal = Action | Finding


def _filled(ws: Workspace, target: str) -> dict[int, str]:
    return {n: v for n, v in ws.values(target).items() if v.strip()}


def _null_tokens(ctx: Context, f: Field) -> Iterator[Proposal]:
    tokens = missing_tokens()
    present = sorted({v.strip() for v in _filled(ctx.ws, f.name).values() if match_key(v) in tokens})
    if present:
        yield SetNull(target=f.name, tokens=present, rationale="placeholder tokens that stand for a missing value")


# Field kinds -------------------------------------------------------------


def _identifier(ctx: Context, f: Field) -> Iterator[Proposal]:
    yield NormalizeText(target=f.name, case="upper", rationale="identifiers are trimmed and upper-cased")


def _free_text(ctx: Context, f: Field) -> Iterator[Proposal]:
    yield NormalizeText(
        target=f.name, case="title_if_uniform", rationale="collapse spaces; title-case Latin text typed in one case"
    )


def _email(ctx: Context, f: Field) -> Iterator[Proposal]:
    yield NormalizeText(target=f.name, case="lower", rationale="emails are trimmed and lower-cased")
    yield from _null_tokens(ctx, f)

    if "phone" in ctx.contract.field_names:
        phones = ctx.ws.values("phone")
        misplaced = [
            n
            for n, v in _filled(ctx.ws, f.name).items()
            if not is_email(v) and normalize_phone(v) and not phones[n].strip()
        ]
        if misplaced:
            yield MoveValue(
                source_field=f.name,
                target="phone",
                rows=misplaced,
                rationale="phone numbers typed into the email column while the phone column is empty",
            )

    invalid = [n for n, v in _filled(ctx.ws, f.name).items() if not is_email(v)]
    if invalid:
        yield SetNull(target=f.name, rows=invalid, issue="S6", rationale="the contract stores invalid emails as null")


def _phone(ctx: Context, f: Field) -> Iterator[Proposal]:
    yield from _null_tokens(ctx, f)
    yield NormalizePhone(target=f.name, rationale="local and spaced formats to E.164")

    short, unreadable = [], []
    for n, v in _filled(ctx.ws, f.name).items():
        if check_value(ctx.contract, f, v):
            digits = sum(ch.isdigit() for ch in v)
            (short if digits < MIN_PHONE_DIGITS else unreadable).append(n)
    if short:
        yield SetNull(target=f.name, rows=short, issue="S6", rationale="too few digits to be a phone number")
    if unreadable:
        yield Finding("S6", f.name, unreadable, note="phone number in a format the rules cannot read")


def _initial_codes(dictionary: ValueDictionary, values: set[str]) -> dict[str, str]:
    """Single-letter codes that stand for exactly one canonical value ("I", "B")."""
    if not values or not all(len(v.strip()) == 1 and v.strip().isalpha() for v in values):
        return {}
    by_initial = defaultdict(list)
    for canonical in dictionary.canonical:
        by_initial[canonical[0].casefold()].append(canonical)
    codes = {}
    for v in values:
        options = by_initial.get(v.strip().casefold(), [])
        if len(options) != 1:
            return {}
        codes[match_key(v)] = options[0]
    return codes


def _closed_set(ctx: Context, f: Field) -> Iterator[Proposal]:
    dictionary = value_dictionary(ctx.contract, f)
    yield from _null_tokens(ctx, f)

    values = set(_filled(ctx.ws, f.name).values())
    mapping = {match_key(v): dictionary.get(v) for v in values if dictionary.get(v) is not None}
    unknown = {v for v in values if dictionary.get(v) is None}
    codes = _initial_codes(dictionary, unknown)
    mapping.update(codes)
    if mapping:
        yield MapValues(
            target=f.name,
            mapping=dict(sorted(mapping.items())),
            issue=dictionary.issue,
            rationale="known spellings mapped to canonical values"
            + (", single-letter codes by initial" if codes else ""),
        )

    left = [n for n, v in _filled(ctx.ws, f.name).items() if check_value(ctx.contract, f, v)]
    if left:
        yield Finding(dictionary.issue, f.name, left, note="value not in the dictionary")


def _date(ctx: Context, f: Field) -> Iterator[Proposal]:
    yield from _null_tokens(ctx, f)
    filled = _filled(ctx.ws, f.name)
    numbers = list(filled)
    inference = infer_date_formats([filled[n] for n in numbers])

    if inference.formats and inference.formats != ["%Y-%m-%d"]:
        numeric = [p for p in inference.formats if FORMATS_BY_PATTERN[p].numeric_day_month]
        if numeric:
            issue, scope = "V2", "column"
            rationale = (
                "day/month order cannot be told from the values"
                if inference.ambiguous
                else "day/month order settled by values where only one reading is a valid date"
            )
        elif inference.has_time:
            issue, scope, rationale = "M4", "column", "timestamps: the time part is dropped"
        else:
            issue, scope, rationale = "S9", "cell", "dates in a non-ISO format"
        yield ParseDate(
            target=f.name,
            formats=inference.formats,
            issue=issue,
            scope=scope,
            ambiguous=bool(inference.ambiguous),
            rationale=rationale,
        )

    unread = [n for n, v in _filled(ctx.ws, f.name).items() if "type" in check_value(ctx.contract, f, v)]
    if unread and not inference.ambiguous:
        yield Finding("S1", f.name, unread, note="not a date in any known format")
    yield from _range(ctx, f)


def _decimal(ctx: Context, f: Field) -> Iterator[Proposal]:
    yield from _null_tokens(ctx, f)
    currency = "currency" if "currency" in ctx.contract.field_names and f.name != "currency" else None
    yield ParseNumber(
        target=f.name,
        units=unit_tokens(ctx.contract),
        currency_field=currency,
        rationale="separators, currency tokens and scale suffixes removed",
    )

    unread = [n for n, v in _filled(ctx.ws, f.name).items() if "type" in check_value(ctx.contract, f, v)]
    if unread:
        yield Finding("S1", f.name, unread, note="not a number the rules can read")
    yield from _range(ctx, f)


def _range(ctx: Context, f: Field) -> Iterator[Proposal]:
    outside = [n for n, v in _filled(ctx.ws, f.name).items() if "range" in check_value(ctx.contract, f, v)]
    if outside:
        yield Finding("S5", f.name, outside, note="outside the contract's range; not changed")


def plan_field(ctx: Context, f: Field) -> Iterator[Proposal]:
    if f.type == "date":
        yield from _date(ctx, f)
    elif f.type == "decimal":
        yield from _decimal(ctx, f)
    elif f.type == "boolean" or f.allowed_values or f.reference:
        yield from _closed_set(ctx, f)
    elif f.name == "email":
        yield from _email(ctx, f)
    elif f.name == "phone":
        yield from _phone(ctx, f)
    elif f.pattern or f.foreign_key or f.name in ctx.contract.primary_key:
        yield from _identifier(ctx, f)
    else:
        yield from _free_text(ctx, f)


# Whole rows --------------------------------------------------------------


def plan_rows(ctx: Context) -> Iterator[Proposal]:
    contract, ws = ctx.contract, ctx.ws

    for f in contract.fields:
        if f.required and f.name not in ctx.missing:
            blank = [n for n, v in ws.values(f.name).items() if not v.strip()]
            if blank:
                yield Finding("S3", f.name, blank, note="required value is missing")

    keys = list(contract.primary_key)
    groups: dict[tuple, list[int]] = defaultdict(list)
    for n, row in ws.rows.items():
        key = tuple(row[k] for k in keys)
        if all(key):
            groups[key].append(n)
    exact, conflicting = [], []
    for rows in groups.values():
        if len(rows) < 2:
            continue
        first = ws.rows[rows[0]]
        same = all(ws.rows[n] == first for n in rows[1:])
        (exact if same else conflicting).append(rows)
    if exact:
        yield Deduplicate(keys=keys, groups=exact, rationale="identical rows; the first copy is kept")
    if conflicting:
        yield Deduplicate(
            keys=keys,
            groups=conflicting,
            conflicting=True,
            issue="S8",
            rationale="same key with different values; keeping the first would discard data",
        )

    for f in contract.fields:
        allowed = ctx.references.get(f.name)
        if f.foreign_key and allowed is not None:
            orphans = [n for n, v in ws.values(f.name).items() if v and v not in allowed]
            if orphans:
                yield DropRows(
                    target=f.name,
                    rows=orphans,
                    issue="S7",
                    reason="orphan_foreign_key",
                    rationale=f"{f.name} not found in {f.foreign_key}",
                )


def plan_missing_columns(missing: list[str]) -> Iterator[Finding]:
    for target in missing:
        yield Finding("M6", target, note="no source column carries this field")
