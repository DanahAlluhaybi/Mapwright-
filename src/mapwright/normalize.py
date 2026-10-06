"""Value-level parsing and normalization.

Pure functions on single values. Actions apply them to columns; the
planner uses them to find out which values a rule can handle.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .canonical import format_decimal

SPACES = re.compile(r"\s+")
LATIN = re.compile(r"[A-Za-z]")
EMAIL = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")
E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")


def clean_spaces(text: str) -> str:
    return SPACES.sub(" ", text).strip()


def match_key(text: str) -> str:
    """The form used to look values up in a dictionary."""
    return clean_spaces(text).casefold()


# Text --------------------------------------------------------------------


def _capitalize(word: str) -> str:
    return "-".join(part[:1].upper() + part[1:].lower() for part in word.split("-"))


def title_if_uniform(text: str) -> str:
    """Title-case Latin text written entirely in upper or lower case.

    Mixed-case text is left alone: "McKenzie" is already deliberate.
    Text without Latin letters (Arabic names) is never touched.
    """
    letters = "".join(LATIN.findall(text))
    if not letters or not (letters.isupper() or letters.islower()):
        return text
    return " ".join(_capitalize(word) for word in text.split(" "))


def is_email(text: str) -> bool:
    return bool(EMAIL.match(text))


# Phones ------------------------------------------------------------------

PHONE_NOISE = re.compile(r"[\s\-().]")


def normalize_phone(text: str) -> str | None:
    """Return the number in E.164, or None if it cannot be read as one.

    Numbers with a "+" or "00" prefix are international. Without a prefix,
    only the Saudi mobile shapes named in the contract are accepted:
    05XXXXXXXX, 5XXXXXXXX and 9665XXXXXXXX.
    """
    compact = PHONE_NOISE.sub("", text)
    if compact.startswith("+"):
        body = compact[1:]
    elif compact.startswith("00"):
        body = compact[2:]
    elif re.fullmatch(r"05\d{8}", compact):
        body = "966" + compact[1:]
    elif re.fullmatch(r"5\d{8}", compact):
        body = "966" + compact
    elif re.fullmatch(r"9665\d{8}", compact):
        body = compact
    else:
        return None
    number = "+" + body
    return number if E164.match(number) else None


def looks_like_phone(text: str) -> bool:
    return normalize_phone(text) is not None


# Dates -------------------------------------------------------------------


@dataclass(frozen=True)
class DateFormat:
    pattern: str
    order: str  # ymd, dmy, mdy: which part comes first, for ambiguity checks
    has_time: bool = False

    @property
    def numeric_day_month(self) -> bool:
        return self.order in ("dmy", "mdy") and "%b" not in self.pattern


DATE_FORMATS = tuple(
    DateFormat(p, o, t)
    for p, o, t in [
        ("%Y-%m-%d", "ymd", False),
        ("%Y/%m/%d", "ymd", False),
        ("%Y.%m.%d", "ymd", False),
        ("%d/%m/%Y", "dmy", False),
        ("%m/%d/%Y", "mdy", False),
        ("%d-%m-%Y", "dmy", False),
        ("%m-%d-%Y", "mdy", False),
        ("%d.%m.%Y", "dmy", False),
        ("%d-%b-%Y", "dmy", False),
        ("%d %b %Y", "dmy", False),
        ("%b %d, %Y", "mdy", False),
        ("%Y-%m-%d %H:%M:%S", "ymd", True),
        ("%Y-%m-%d %H:%M", "ymd", True),
        ("%Y-%m-%dT%H:%M:%S", "ymd", True),
        ("%d/%m/%Y %H:%M", "dmy", True),
        ("%m/%d/%Y %H:%M", "mdy", True),
    ]
)
FORMATS_BY_PATTERN = {f.pattern: f for f in DATE_FORMATS}


def parse_date(text: str, pattern: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(text.strip(), pattern).date()
    except ValueError:
        return None


def _swap_day_month(pattern: str) -> str:
    return pattern.replace("%d", "\0").replace("%m", "%d").replace("\0", "%m")


@dataclass
class DateInference:
    """What a column of date text looks like.

    ``ambiguous`` lists formats whose day/month order the values cannot
    settle: every value parses both ways. ``evidence`` counts values that
    only parse in the chosen order (a day above 12).
    """

    formats: list[str]
    unparsed: list[int]
    ambiguous: list[str]
    evidence: dict[str, int]

    @property
    def has_time(self) -> bool:
        return any(FORMATS_BY_PATTERN[p].has_time for p in self.formats)


def infer_date_formats(values: list[str], max_formats: int = 3) -> DateInference:
    """Choose the fewest formats that read the non-blank values.

    Formats are picked greedily by coverage. When a day-first format and
    its month-first twin cover the same values, the order is ambiguous.
    """
    pending = {i for i, v in enumerate(values) if v.strip()}
    chosen: list[str] = []
    ambiguous: list[str] = []
    evidence: dict[str, int] = {}

    while pending and len(chosen) < max_formats:
        coverage = {f.pattern: {i for i in pending if parse_date(values[i], f.pattern)} for f in DATE_FORMATS}
        best = max(coverage, key=lambda p: len(coverage[p]))
        if not coverage[best]:
            break
        fmt = FORMATS_BY_PATTERN[best]
        if fmt.numeric_day_month:
            twin = _swap_day_month(best)
            if twin in coverage and coverage[twin] == coverage[best]:
                ambiguous.append(best)
            else:
                evidence[best] = len(coverage[best] - coverage.get(twin, set()))
        chosen.append(best)
        pending -= coverage[best]

    return DateInference(chosen, sorted(pending), ambiguous, evidence)


# Numbers -----------------------------------------------------------------

NATIVE_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")
NUMBER = re.compile(r"^([+-]?)(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?([KkMm])?$")
SCALES = {"k": Decimal(1000), "m": Decimal(1_000_000)}


@dataclass(frozen=True)
class ParsedNumber:
    value: Decimal
    separators: bool = False
    unit: str | None = None
    scaled: bool = False
    native_digits: bool = False

    @property
    def text(self) -> str:
        return format_decimal(self.value)

    @property
    def embedded_unit(self) -> bool:
        return self.unit is not None or self.scaled or self.native_digits


def _unit_pattern(tokens: list[str]) -> re.Pattern | None:
    if not tokens:
        return None
    parts = []
    for token in sorted(tokens, key=len, reverse=True):
        escaped = re.escape(token)
        parts.append(rf"(?<!\w){escaped}(?!\w)" if token[-1].isalnum() else escaped)
    return re.compile("|".join(parts), re.IGNORECASE)


def parse_number(text: str, units: dict[str, str] | None = None) -> ParsedNumber | None:
    """Read a number written with separators, a unit or a scale suffix.

    ``units`` maps unit tokens ("SAR", "SR", "$", "ريال") to the code they
    stand for. A recognised token is removed and reported in ``unit``.
    """
    units = units or {}
    native = text.translate(NATIVE_DIGITS)
    native_digits = native != text
    unit = None

    pattern = _unit_pattern(list(units))
    if pattern:
        found = pattern.findall(native)
        if len({units[_lookup(units, t)] for t in found}) > 1:
            return None
        if found:
            unit = units[_lookup(units, found[0])]
            native = pattern.sub(" ", native)

    compact = native.replace(" ", "").replace(" ", "")
    match = NUMBER.match(compact)
    if not match:
        return None
    sign, whole, fraction, scale = match.groups()
    try:
        value = Decimal(f"{sign}{whole.replace(',', '')}.{fraction or '0'}")
    except InvalidOperation:
        return None
    if scale:
        value *= SCALES[scale.lower()]
    return ParsedNumber(value, "," in whole, unit, bool(scale), native_digits)


def _lookup(units: dict[str, str], found: str) -> str:
    for token in units:
        if token.casefold() == found.casefold():
            return token
    raise KeyError(found)
