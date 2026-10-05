"""How clean values are turned into messy source text.

Two kinds of function live here:

* **Formats** change a whole column the way a particular legacy system
  writes it (day-first dates, Y/N flags, upper-case names). They take a
  non-null value and return text.
* **Injectors** corrupt individual cells at a given rate. They may also
  update the ground truth, for example when an invalid email means the
  correct output is null. They return False when a row is not eligible.

Every change either kind makes is recorded in the case manifest by the
builder, so nothing here needs to know about bookkeeping.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from mapwright.canonical import format_decimal, to_text

from . import pools


@dataclass
class Context:
    rng: random.Random
    pool: str
    gt: dict
    src: dict[str, str]
    headers: dict[str, str]
    field: str

    @property
    def header(self) -> str:
        return self.headers[self.field]

    @property
    def value(self):
        return self.gt[self.field]


Format = Callable[[object, Context], str]
Injector = Callable[[Context], bool]


# Formats ---------------------------------------------------------------

def date_format(order: str, separator: str) -> Format:
    """Numeric dates in a fixed order, e.g. ("dmy", "/") -> 05/03/2024."""

    def render(value: dt.date, ctx: Context) -> str:
        parts = {"d": f"{value.day:02d}", "m": f"{value.month:02d}", "y": f"{value.year:04d}"}
        return separator.join(parts[p] for p in order)

    return render


def date_with_month_name(value: dt.date, ctx: Context) -> str:
    return f"{value.day:02d}-{pools.MONTH_ABBREVIATIONS[value.month - 1]}-{value.year}"


def datetime_with_time(value: dt.date, ctx: Context) -> str:
    return (
        f"{value.isoformat()} {ctx.rng.randint(8, 22):02d}:"
        f"{ctx.rng.randint(0, 59):02d}:{ctx.rng.randint(0, 59):02d}"
    )


def fixed_map(mapping: dict[str, str]) -> Format:
    """Every canonical value is written the same way, e.g. True -> "Y"."""

    def render(value, ctx: Context) -> str:
        return mapping[to_text(value)]

    return render


def any_variant(kind: str) -> Format:
    """Each cell independently uses one of the pool's variants."""

    def render(value, ctx: Context) -> str:
        options = pools.variants(kind, to_text(value), ctx.pool)
        return ctx.rng.choice(options) if options else to_text(value)

    return render


def upper_case(value, ctx: Context) -> str:
    return to_text(value).upper()


def thousands_separated(value: Decimal, ctx: Context) -> str:
    return f"{value:,.2f}"


# Injectors -------------------------------------------------------------

def variant(kind: str) -> Injector:
    """Replace the value with a synonym from the case's pool (V1, V5, V6)."""

    def inject(ctx: Context) -> bool:
        if ctx.value is None:
            return False
        options = pools.variants(kind, to_text(ctx.value), ctx.pool)
        if not options:
            return False
        ctx.src[ctx.header] = ctx.rng.choice(options)
        return True

    return inject


def whitespace_or_case(ctx: Context) -> bool:
    """Stray spaces, or the wrong case where case is meaningful (S4)."""
    text = ctx.src[ctx.header]
    if not text:
        return False
    has_latin = any("a" <= ch.lower() <= "z" for ch in text)
    choices = ["pad", "double_space"] + (["lower", "upper"] if has_latin else [])
    if " " not in text.strip():
        choices.remove("double_space")
    noise = ctx.rng.choice(choices)
    if noise == "pad":
        noisy = " " * ctx.rng.randint(1, 2) + text + " " * ctx.rng.randint(1, 3)
    elif noise == "double_space":
        noisy = text.replace(" ", "  ", 1)
    elif noise == "lower":
        noisy = text.lower()
    else:
        noisy = text.upper()
    if noisy == text:
        return False
    ctx.src[ctx.header] = noisy
    return True


def invalid_email(ctx: Context) -> bool:
    """An email the contract rejects; the correct output is null (S6)."""
    email = ctx.value
    if email is None:
        return False
    local, domain = email.split("@")
    broken = ctx.rng.choice([f"{local}{domain}", f"{local}@", f"{local}@{domain.split('.')[0]}"])
    ctx.src[ctx.header] = broken
    ctx.gt[ctx.field] = None
    return True


def invalid_phone(ctx: Context) -> bool:
    """A number too short to be real; the correct output is null (S6)."""
    if ctx.value is None:
        return False
    ctx.src[ctx.header] = "".join(str(ctx.rng.randint(0, 9)) for _ in range(6))
    ctx.gt[ctx.field] = None
    return True


def required_missing(ctx: Context) -> bool:
    """A required value is blank and cannot be recovered (S3)."""
    if ctx.value is None:
        return False
    ctx.src[ctx.header] = ""
    ctx.gt[ctx.field] = None
    return True


def disguised_missing(ctx: Context) -> bool:
    """A null written as a placeholder token (V3)."""
    if ctx.value is not None or ctx.src[ctx.header] != "":
        return False
    ctx.src[ctx.header] = ctx.rng.choice(pools.DISGUISED_MISSING[ctx.pool])
    return True


def thousands_separator(ctx: Context) -> bool:
    """A number with grouping commas (S1)."""
    if ctx.value is None:
        return False
    ctx.src[ctx.header] = f"{ctx.value:,.2f}"
    return True


def _currency_of(ctx: Context) -> str:
    return ctx.gt.get("currency") or "SAR"


ALL_UNIT_STYLES = ("code_prefix", "thousands_suffix", "arabic_digits")


def embedded_unit(styles: tuple[str, ...] = ALL_UNIT_STYLES) -> Injector:
    """A number with a currency, a scale suffix or Arabic-Indic digits (V4).

    Scale suffixes and Arabic-Indic digits are only used on round amounts,
    and the ground truth is rounded to match, so the value stays exact.
    Files saved as Windows-1256 cannot hold Arabic-Indic digits, so those
    cases pass a narrower set of styles.
    """

    def inject(ctx: Context) -> bool:
        amount = ctx.value
        if amount is None:
            return False
        currency = _currency_of(ctx)
        style = ctx.rng.choice(styles)

        if style == "code_prefix":
            if ctx.pool == "dev":
                text = f"{currency} {amount:,.2f}"
            else:
                text = f"{amount:,.2f}/- {currency}"
        else:
            rounded = Decimal(max(100, int(round(amount, -2))))
            ctx.gt[ctx.field] = rounded
            if style == "thousands_suffix":
                thousands = format((rounded / 1000).normalize(), "f")
                text = f"{thousands}K" if ctx.pool == "dev" else f"{thousands}k {currency}"
            else:
                digits = str(int(rounded)).translate(pools.ARABIC_INDIC_DIGITS)
                word = "ريال" if currency == "SAR" else currency
                text = f"{digits} {word}" if ctx.pool == "dev" else f"{word} {digits}"

        ctx.src[ctx.header] = text
        return True

    return inject


def negative_sign(ctx: Context) -> bool:
    """A sign error on a non-negative amount; the true value is positive (S5)."""
    if ctx.value is None:
        return False
    ctx.src[ctx.header] = "-" + format_decimal(ctx.value)
    return True


def local_phone_format(ctx: Context) -> bool:
    """A valid number in a local or spaced format instead of E.164 (S9)."""
    phone = ctx.value
    if phone is None:
        return False
    rng = ctx.rng
    if phone.startswith("+9665"):
        national = phone[4:]
        choices = [
            "0" + national,
            national,
            phone[1:],
            "00966 " + national[:2] + " " + national[2:5] + " " + national[5:],
            "+966 " + national[:2] + " " + national[2:5] + " " + national[5:],
        ]
        text = rng.choice(choices)
    else:
        text = "00" + phone[1:]
    ctx.src[ctx.header] = text
    return True


def phone_in_email_column(ctx: Context) -> bool:
    """The phone number was typed into the email column (V7).

    Only used where the customer has no email, so nothing is lost: the
    correct output moves the number to phone and leaves email null.
    """
    gt = ctx.gt
    if gt["email"] is not None or gt["phone"] is None:
        return False
    phone_header = ctx.headers["phone"]
    ctx.src[ctx.header] = "0" + gt["phone"][4:] if gt["phone"].startswith("+9665") else gt["phone"]
    ctx.src[phone_header] = ""
    return True


def bool_variant(ctx: Context) -> bool:
    """Mixed boolean spellings within one column (V5)."""
    return variant("boolean")(ctx)


def id_noise(ctx: Context) -> bool:
    """Identifier in lower case with a trailing space (S4)."""
    text = ctx.src[ctx.header]
    if not text:
        return False
    ctx.src[ctx.header] = text.lower() + " "
    return True
