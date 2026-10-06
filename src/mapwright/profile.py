"""Column profiles: what each source column holds, without reading meaning into it."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass

from .ingest import SourceTable
from .normalize import (
    DATE_FORMATS,
    is_email,
    looks_like_phone,
    parse_date,
    parse_number,
)

TOP_VALUES = 10


def signature(text: str) -> str:
    """Character-class shape of a value: "C-100001" -> "A-9"."""
    shape = re.sub(r"[A-Za-z]+", "A", text)
    shape = re.sub(r"[0-9]+", "9", shape)
    shape = re.sub(r"[؀-ۿ]+", "ع", shape)
    return re.sub(r"\s+", " ", shape)


def _rate(values: list[str], test) -> float:
    return sum(1 for v in values if test(v)) / len(values) if values else 0.0


@dataclass
class ColumnProfile:
    name: str
    rows: int
    blank: int
    distinct: int
    top_values: list[tuple[str, int]]
    signatures: list[tuple[str, int]]
    min_length: int
    max_length: int
    date_rate: float
    number_rate: float
    email_rate: float
    phone_rate: float

    def to_dict(self) -> dict:
        return asdict(self)


def profile_column(name: str, values: list[str]) -> ColumnProfile:
    filled = [v.strip() for v in values if v.strip()]
    counts = Counter(filled)
    lengths = [len(v) for v in filled] or [0]
    return ColumnProfile(
        name=name,
        rows=len(values),
        blank=len(values) - len(filled),
        distinct=len(counts),
        top_values=counts.most_common(TOP_VALUES),
        signatures=Counter(signature(v) for v in filled).most_common(5),
        min_length=min(lengths),
        max_length=max(lengths),
        date_rate=_rate(filled, lambda v: any(parse_date(v, f.pattern) for f in DATE_FORMATS)),
        number_rate=_rate(filled, lambda v: parse_number(v) is not None),
        email_rate=_rate(filled, lambda v: is_email(v.lower())),
        phone_rate=_rate(filled, looks_like_phone),
    )


def profile_table(table: SourceTable) -> dict[str, ColumnProfile]:
    return {name: profile_column(name, table.column(name)) for name in table.header}
