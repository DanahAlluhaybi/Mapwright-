"""Canonical text forms for target values.

Ground-truth files, pipeline output and the scorer all go through these
functions, so two values are equal exactly when their canonical text is.
An empty string always means null.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def format_decimal(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def format_date(value: dt.date) -> str:
    if isinstance(value, dt.datetime):
        value = value.date()
    return value.isoformat()


def format_bool(value: bool) -> str:
    return "true" if value else "false"


def to_text(value: object) -> str:
    """Render a Python value in its canonical text form."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return format_bool(value)
    if isinstance(value, Decimal):
        return format_decimal(value)
    if isinstance(value, dt.date):
        return format_date(value)
    return str(value)
