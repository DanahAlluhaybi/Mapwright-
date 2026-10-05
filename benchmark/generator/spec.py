"""The shape of a benchmark case definition."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

from .render import Format, Injector


@dataclass(frozen=True)
class Column:
    """One column of the messy source file.

    ``field`` is the target field the column carries, or None for an
    irrelevant column that should be dropped. ``mapping_issue`` records how
    the header relates to the target (M1 renamed, M2 non-English, M3 split,
    M4 merged); None means the header already equals the field name.
    ``fmt`` rewrites every non-null value in the column, and ``fmt_issue``
    is the issue that rewrite represents.
    """

    header: str
    field: str | None = None
    mapping_issue: str | None = None
    fmt: Format | None = None
    fmt_issue: str | None = None
    split_part: int | None = None
    extra: Callable[[random.Random], str] | None = None


@dataclass(frozen=True)
class Injection:
    issue: str
    field: str
    rate: float
    inject: Injector


@dataclass(frozen=True)
class Case:
    id: str
    split: str
    entity: str
    style: str
    seed: int
    rows: int
    columns: tuple[Column, ...]
    injections: tuple[Injection, ...] = ()
    pool: str = "dev"
    encoding: str = "utf-8"
    title_rows: tuple[str, ...] = ()
    exact_duplicate_rate: float = 0.0
    conflicting_duplicate_rate: float = 0.0
    orphan_rate: float = 0.0
    arabic_share: float = 0.3
    max_day: int | None = None
    description: str = ""
