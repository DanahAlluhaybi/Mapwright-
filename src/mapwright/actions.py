"""The fixed vocabulary of typed actions.

Every change to the data is one of these actions. An action is computed
against the current workspace into an ``Effect`` (the exact cells it would
change and rows it would remove) before anything is committed, so the
approval policy can look at the effect first and a held action leaves no
trace in the data.

Actions are plain data and serialize to JSON, which is what makes a plan
replayable: the same actions applied to the same source give the same
target.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import ClassVar

from .canonical import format_date
from .ingest import SourceTable
from .normalize import (
    clean_spaces,
    match_key,
    normalize_phone,
    parse_date,
    parse_number,
    title_if_uniform,
)


@dataclass(frozen=True)
class Change:
    row: int
    field: str
    before: str
    after: str
    issue: str | None


@dataclass
class Effect:
    changes: list[Change] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)

    def rows(self, target: str | None = None) -> list[int]:
        return sorted({c.row for c in self.changes if target is None or c.field == target})

    def fields(self) -> list[str]:
        return sorted({c.field for c in self.changes})

    def summary(self) -> dict:
        return {"cells_changed": len(self.changes), "rows_removed": len(self.removed)}


class Workspace:
    """The staging table: one dict per surviving source row, keyed by its number."""

    def __init__(self, source: SourceTable, fields: list[str]):
        self.source = source
        self.fields = list(fields)
        self.rows: dict[int, dict[str, str]] = {n: {f: "" for f in fields} for n in source.row_numbers}

    def values(self, target: str) -> dict[int, str]:
        return {n: row[target] for n, row in self.rows.items()}

    def commit(self, effect: Effect) -> None:
        for change in effect.changes:
            self.rows[change.row][change.field] = change.after
        for number in effect.removed:
            self.rows.pop(number, None)

    def table(self) -> tuple[list[dict[str, str]], list[int]]:
        numbers = list(self.rows)
        return [dict(self.rows[n]) for n in numbers], numbers


REGISTRY: dict[str, type["Action"]] = {}


@dataclass(kw_only=True)
class Action:
    """Base class. ``issue`` is the label changes are reported under;
    ``scope`` says whether the issue is per cell or about the whole column."""

    type: ClassVar[str] = ""
    id: str = ""
    origin: str = "rules"
    rationale: str = ""
    issue: str | None = None
    scope: str = "cell"
    confidence: float | None = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.type:
            REGISTRY[cls.type] = cls

    @property
    def targets(self) -> list[str]:
        return [self.target] if hasattr(self, "target") else []

    def compute(self, ws: Workspace) -> Effect:
        raise NotImplementedError

    def to_dict(self) -> dict:
        return {"type": self.type, **asdict(self)}


def action_from_dict(data: dict) -> Action:
    data = dict(data)
    cls = REGISTRY[data.pop("type")]
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def _cell_changes(action: Action, ws: Workspace, target: str, convert) -> Effect:
    effect = Effect()
    for number, before in ws.values(target).items():
        after = convert(before)
        if after is not None and after != before:
            effect.changes.append(Change(number, target, before, after, action.classify(before, after)))
    return effect


class _Classify:
    def classify(self, before: str, after: str) -> str | None:
        return self.issue


# Structure ---------------------------------------------------------------


@dataclass(kw_only=True)
class MapColumn(_Classify, Action):
    type: ClassVar[str] = "map_column"
    source: str
    target: str

    def compute(self, ws: Workspace) -> Effect:
        effect = Effect()
        for number, row in zip(ws.source.row_numbers, ws.source.rows):
            if number in ws.rows and row[self.source] != ws.rows[number][self.target]:
                effect.changes.append(Change(number, self.target, ws.rows[number][self.target], row[self.source], None))
        return effect


@dataclass(kw_only=True)
class CombineColumns(_Classify, Action):
    type: ClassVar[str] = "combine_columns"
    sources: list[str]
    target: str
    separator: str = " "

    def compute(self, ws: Workspace) -> Effect:
        effect = Effect()
        for number, row in zip(ws.source.row_numbers, ws.source.rows):
            if number not in ws.rows:
                continue
            value = self.separator.join(p for p in (row[s].strip() for s in self.sources) if p)
            if value != ws.rows[number][self.target]:
                effect.changes.append(Change(number, self.target, ws.rows[number][self.target], value, None))
        return effect


@dataclass(kw_only=True)
class DropColumn(_Classify, Action):
    type: ClassVar[str] = "drop_column"
    column: str

    def compute(self, ws: Workspace) -> Effect:
        return Effect()


# Values ------------------------------------------------------------------


@dataclass(kw_only=True)
class NormalizeText(_Classify, Action):
    """Trim, collapse repeated spaces and optionally fix case."""

    type: ClassVar[str] = "normalize_text"
    target: str
    case: str = "keep"  # keep | upper | lower | title_if_uniform
    issue: str | None = "S4"

    def convert(self, text: str) -> str:
        text = clean_spaces(text)
        if self.case == "upper":
            return text.upper()
        if self.case == "lower":
            return text.lower()
        if self.case == "title_if_uniform":
            return title_if_uniform(text)
        return text

    def compute(self, ws: Workspace) -> Effect:
        return _cell_changes(self, ws, self.target, self.convert)


@dataclass(kw_only=True)
class SetNull(_Classify, Action):
    """Blank out listed placeholder tokens, or listed rows."""

    type: ClassVar[str] = "set_null"
    target: str
    tokens: list[str] = field(default_factory=list)
    rows: list[int] = field(default_factory=list)
    issue: str | None = "V3"

    def compute(self, ws: Workspace) -> Effect:
        tokens = {match_key(t) for t in self.tokens}
        rows = set(self.rows)
        effect = Effect()
        for number, before in ws.values(self.target).items():
            if before and (number in rows or match_key(before) in tokens):
                effect.changes.append(Change(number, self.target, before, "", self.issue))
        return effect


@dataclass(kw_only=True)
class MapValues(_Classify, Action):
    """Replace values through an explicit dictionary (keys in lookup form)."""

    type: ClassVar[str] = "map_values"
    target: str
    mapping: dict[str, str]

    def compute(self, ws: Workspace) -> Effect:
        return _cell_changes(self, ws, self.target, lambda v: self.mapping.get(match_key(v)) if v else None)


@dataclass(kw_only=True)
class ParseDate(_Classify, Action):
    """Read dates in the listed formats, tried in order, and write ISO dates."""

    type: ClassVar[str] = "parse_date"
    target: str
    formats: list[str]
    ambiguous: bool = False

    def convert(self, text: str) -> str | None:
        if not text.strip():
            return None
        for pattern in self.formats:
            value = parse_date(text, pattern)
            if value is not None:
                return format_date(value)
        return None

    def compute(self, ws: Workspace) -> Effect:
        return _cell_changes(self, ws, self.target, self.convert)


@dataclass(kw_only=True)
class ParseNumber(Action):
    """Read numbers with separators, unit tokens and scale suffixes.

    With ``currency_field`` set, a currency token inside the amount must
    agree with that row's currency, or the value is left for review.
    """

    type: ClassVar[str] = "parse_number"
    target: str
    units: dict[str, str] = field(default_factory=dict)
    currency_field: str | None = None

    def classify(self, before: str, after: str) -> str | None:
        parsed = parse_number(before.strip(), self.units)
        if parsed is not None and parsed.embedded_unit:
            return "V4"
        return "S1" if parsed is not None and parsed.separators else self.issue

    def compute(self, ws: Workspace) -> Effect:
        effect = Effect()
        for number, row in ws.rows.items():
            before = row[self.target]
            parsed = parse_number(before.strip(), self.units) if before.strip() else None
            if parsed is None:
                continue
            if parsed.unit and self.currency_field and row.get(self.currency_field) not in ("", parsed.unit):
                continue
            if parsed.text != before:
                effect.changes.append(
                    Change(number, self.target, before, parsed.text, self.classify(before, parsed.text))
                )
        return effect


@dataclass(kw_only=True)
class NormalizePhone(_Classify, Action):
    type: ClassVar[str] = "normalize_phone"
    target: str
    issue: str | None = "S9"

    def compute(self, ws: Workspace) -> Effect:
        return _cell_changes(self, ws, self.target, lambda v: normalize_phone(v) if v else None)


@dataclass(kw_only=True)
class MoveValue(_Classify, Action):
    """Move values typed into the wrong column, where the right one is empty."""

    type: ClassVar[str] = "move_value"
    source_field: str
    target: str
    rows: list[int]
    issue: str | None = "V7"

    @property
    def targets(self) -> list[str]:
        return [self.source_field, self.target]

    def compute(self, ws: Workspace) -> Effect:
        effect = Effect()
        for number in self.rows:
            row = ws.rows.get(number)
            if row is None or not row[self.source_field] or row[self.target]:
                continue
            effect.changes.append(Change(number, self.target, row[self.target], row[self.source_field], self.issue))
            effect.changes.append(Change(number, self.source_field, row[self.source_field], "", self.issue))
        return effect


# Rows --------------------------------------------------------------------


@dataclass(kw_only=True)
class Deduplicate(_Classify, Action):
    """Keep the first row of each group and remove the rest.

    ``conflicting`` groups share a key but differ elsewhere, so removing
    the later rows loses information and needs review.
    """

    type: ClassVar[str] = "deduplicate"
    keys: list[str]
    groups: list[list[int]]
    conflicting: bool = False
    issue: str | None = "S2"

    @property
    def targets(self) -> list[str]:
        return list(self.keys)

    def compute(self, ws: Workspace) -> Effect:
        return Effect(removed=[n for group in self.groups for n in group[1:] if n in ws.rows])


@dataclass(kw_only=True)
class DropRows(_Classify, Action):
    type: ClassVar[str] = "drop_rows"
    target: str
    rows: list[int]
    reason: str = ""

    def compute(self, ws: Workspace) -> Effect:
        return Effect(removed=[n for n in self.rows if n in ws.rows])
