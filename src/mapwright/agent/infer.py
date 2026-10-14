"""What each column is: type, role, statistics and the evidence for them.

Deterministic. Every conclusion carries the counts it rests on, so the page
can say "412 of 420 values (98%) read as dd/mm/yyyy dates" rather than "this
is a date column". The ``meaning`` field is a guess from the column name and
is labelled as one.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from ..normalize import (
    clean_spaces,
    infer_date_formats,
    is_email,
    looks_like_phone,
    match_key,
    parse_date,
    parse_number,
)

NULL_TOKENS = {
    "", "n/a", "na", "null", "none", "nil", "-", "--", "—", "?", "nan", "#n/a", "unknown",
    "غير متوفر", "لا يوجد", "غير معروف", "فارغ",
}
TRUE_TOKENS = {"yes", "y", "true", "t", "1", "active", "on", "نعم", "صح", "فعال", "نشط"}
FALSE_TOKENS = {"no", "n", "false", "f", "0", "inactive", "off", "لا", "خطأ", "غير فعال", "غير نشط"}
SAMPLE = 8
# Currency words and signs read off numbers ("700 ريال", "$1,200"); the unit is reported, never guessed.
UNITS = {"SAR": "SAR", "SR": "SAR", "ر.س": "SAR", "ريال": "SAR", "رس": "SAR", "USD": "USD", "$": "USD",
         "AED": "AED", "درهم": "AED", "EUR": "EUR", "€": "EUR", "GBP": "GBP", "£": "GBP", "KWD": "KWD", "QAR": "QAR"}

TYPE_LABELS = {
    "empty": "empty",
    "boolean": "yes/no",
    "integer": "whole number",
    "decimal": "number",
    "date": "date",
    "email": "email",
    "phone": "phone",
    "identifier": "identifier",
    "category": "category",
    "text": "text",
}

# Header words that hint at meaning. A hint only names a guess; it never
# changes the type, which comes from the values.
MEANINGS = [
    (r"\b(id|code|no|nr|number|ref|key|sku)\b|رقم|كود", "an identifier or reference number"),
    (r"\b(e ?mail|mail)\b|بريد", "an email address"),
    (r"\b(phone|mobile|tel|telephone|cell)\b|جوال|هاتف", "a phone number"),
    (r"\b(date|time|created|joined|signup|day|month|year|period)\b|تاريخ", "a date"),
    (r"\b(status|state|stage)\b|حالة", "a status"),
    (r"\b(type|category|class|segment|group|channel|kind)\b|نوع|فئة|قناة", "a category"),
    (r"\b(city|country|region|area|address|town|county|branch|location)\b|مدينة|دولة|منطقة|عنوان|فرع", "a place"),
    (r"\b(active|enabled|flag|is|has)\b|نشط|فعال", "a yes/no flag"),
    (r"\b(price|amount|revenue|sales|total|cost|value|limit|salary|credit|net|gross|discount)\b|مبلغ|سعر|إجمالي|مبيعات|راتب", "a money amount"),
    (r"\b(qty|quantity|count|units|num)\b|كمية|عدد", "a quantity"),
    (r"\b(name|customer|client|supplier|vendor|company)\b|اسم|عميل|مورد|شركة", "a name"),
]

DATE_LABELS = {"%Y": "yyyy", "%m": "mm", "%d": "dd", "%b": "Mon", "%H": "HH", "%M": "MM", "%S": "SS"}


def date_label(pattern: str) -> str:
    for code, label in DATE_LABELS.items():
        pattern = pattern.replace(code, label)
    return pattern


def is_null(value: str) -> bool:
    return match_key(value) in NULL_TOKENS


def bool_value(value: str) -> bool | None:
    key = match_key(value)
    if key in TRUE_TOKENS:
        return True
    if key in FALSE_TOKENS:
        return False
    return None


def pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "0%"


@dataclass
class ColumnInfo:
    name: str
    type: str
    role: str
    confidence: float
    rows: int
    blank: int
    null_tokens: int
    distinct: int
    top_values: list[tuple[str, int]]
    examples: list[str]
    evidence: list[str] = field(default_factory=list)
    misfits: list[str] = field(default_factory=list)  # values that do not fit the type
    stats: dict = field(default_factory=dict)
    date_formats: list[str] = field(default_factory=list)
    date_ambiguous: bool = False
    meaning: str | None = None

    @property
    def filled(self) -> int:
        return self.rows - self.blank - self.null_tokens

    @property
    def label(self) -> str:
        return TYPE_LABELS[self.type]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["filled"] = self.filled
        data["label"] = self.label
        return data


def _numbers(values: list[str]) -> list[Decimal | None]:
    out = []
    for v in values:
        parsed = parse_number(v, UNITS)
        out.append(parsed.value if parsed else None)
    return out


def _numeric_stats(numbers: list[Decimal]) -> dict:
    floats = sorted(float(n) for n in numbers)
    q1, q3 = (statistics.quantiles(floats, n=4)[0], statistics.quantiles(floats, n=4)[2]) if len(floats) >= 4 else (floats[0], floats[-1])
    return {
        "min": floats[0],
        "max": floats[-1],
        "mean": round(statistics.fmean(floats), 4),
        "median": statistics.median(floats),
        "q1": q1,
        "q3": q3,
        "sum": round(sum(floats), 4),
    }


def words(name: str) -> str:
    """ "NetAmount_SAR" -> "net amount sar", so word boundaries work on headers."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name.strip())
    return re.sub(r"[^\w\u0600-\u06FF]+|_", " ", spaced).lower().strip()


def _meaning(name: str) -> str | None:
    lowered = words(name)
    for pattern, meaning in MEANINGS:
        if re.search(pattern, lowered):
            return meaning
    return None


def infer_column(name: str, values: list[str]) -> ColumnInfo:
    rows = len(values)
    blank = sum(1 for v in values if not v.strip())
    nulls = [v for v in values if v.strip() and is_null(v)]
    filled = [v for v in values if v.strip() and not is_null(v)]
    counts = Counter(clean_spaces(v) for v in filled)
    info = ColumnInfo(
        name=name,
        type="text",
        role="text",
        confidence=0.0,
        rows=rows,
        blank=blank,
        null_tokens=len(nulls),
        distinct=len(counts),
        top_values=counts.most_common(10),
        examples=list(dict.fromkeys(clean_spaces(v) for v in filled))[:SAMPLE],
        meaning=_meaning(name),
    )
    n = len(filled)
    if blank or nulls:
        info.evidence.append(
            f"{blank + len(nulls)} of {rows} values are missing"
            + (f" ({len(nulls)} written as placeholders like {sorted(set(nulls))[0]!r})" if nulls else "")
        )
    if n == 0:
        info.type, info.role, info.confidence = "empty", "empty", 1.0
        info.evidence.append("no values")
        return info

    def settle(kind: str, role: str, hits: int, misfits: list[str], why: str) -> ColumnInfo:
        info.type, info.role = kind, role
        info.confidence = round(hits / n, 3)
        info.misfits = list(dict.fromkeys(misfits))[:SAMPLE]
        info.evidence.insert(0, f"{hits} of {n} filled values ({pct(hits, n)}) {why}")
        return info

    keys = {match_key(v) for v in filled}
    bools = [bool_value(v) for v in filled]
    bool_hits = sum(b is not None for b in bools)
    has_both = any(b is True for b in bools) and any(b is False for b in bools)
    digits_only = keys <= {"0", "1"}
    if bool_hits / n >= 0.97 and len(keys) <= 8 and has_both:
        # 0/1 alone is usually a count, unless the header says it is a flag
        if not digits_only or info.meaning == "a yes/no flag":
            misfits = [v for v, b in zip(filled, bools) if b is None]
            info.stats = {"true": sum(b is True for b in bools), "false": sum(b is False for b in bools)}
            return settle("boolean", "flag", bool_hits, misfits, "are yes/no words such as " + ", ".join(sorted(keys)[:6]))

    dates = infer_date_formats(filled)
    date_hits = n - len(dates.unparsed)
    if dates.formats and date_hits / n >= 0.8:
        info.date_formats = dates.formats
        info.date_ambiguous = bool(dates.ambiguous)
        parsed = [d for v in filled for d in [next((parse_date(v, f) for f in dates.formats if parse_date(v, f)), None)] if d]
        if parsed:
            info.stats = {"first": min(parsed).isoformat(), "last": max(parsed).isoformat()}
        labels = ", ".join(date_label(f) for f in dates.formats)
        why = f"read as dates ({labels})"
        if dates.ambiguous:
            why += "; day and month order cannot be told apart from the values"
        return settle("date", "date", date_hits, [filled[i] for i in dates.unparsed], why)

    emails = sum(is_email(match_key(v)) for v in filled)
    if emails / n >= (0.6 if info.meaning == "an email address" else 0.8):
        return settle("email", "contact", emails, [v for v in filled if not is_email(match_key(v))], "look like email addresses")
    phones = sum(looks_like_phone(v) for v in filled)
    if phones / n >= (0.6 if info.meaning == "a phone number" else 0.8):
        return settle("phone", "contact", phones, [v for v in filled if not looks_like_phone(v)], "read as phone numbers")

    numbers = _numbers(filled)
    number_hits = sum(x is not None for x in numbers)
    if number_hits / n >= 0.8:
        good = [x for x in numbers if x is not None]
        integral = all(x == x.to_integral_value() for x in good)
        info.stats = _numeric_stats(good)
        misfits = [v for v, x in zip(filled, numbers) if x is None]
        long_codes = min(len(re.sub(r"\D", "", v)) for v in filled if parse_number(v, UNITS)) >= 6 if good else False
        looks_id = integral and n > 1 and info.meaning == "an identifier or reference number" and (len(counts) >= 0.9 * n or long_codes)
        if looks_id:
            why = "are distinct whole numbers" if len(counts) >= 0.9 * n else "are long whole numbers"
            return settle("identifier", "identifier", number_hits, misfits, why + " in a column named like an identifier")
        return settle("integer" if integral else "decimal", "measure", number_hits, misfits, "read as numbers")

    if len(counts) >= 0.9 * n and n > 1:
        shapes = Counter(re.sub(r"[0-9]+", "9", re.sub(r"[A-Za-z]+", "A", v.strip())) for v in filled)
        shape, shape_hits = shapes.most_common(1)[0]
        if shape_hits / n >= 0.8 and re.search(r"9", shape) and len(shape) <= 12:
            misfits = [v for v in filled if re.sub(r"[0-9]+", "9", re.sub(r"[A-Za-z]+", "A", v.strip())) != shape]
            return settle("identifier", "identifier", shape_hits, misfits, f"are distinct and share the pattern {shape!r}")

    named_like_category = info.meaning in ("a category", "a status", "a place", "a yes/no flag")
    if len(counts) <= 50 and (len(counts) <= n / 2 or (named_like_category and len(counts) < n)):
        info.stats = {"categories": len(counts)}
        variants = Counter(match_key(v) for v in counts)
        spelled = sum(1 for k, c in variants.items() if c > 1)
        why = f"fall into {len(counts)} repeated values"
        if spelled:
            why += f"; {spelled} of them are spelled in more than one way"
        return settle("category", "category", n, [], why)

    lengths = [len(v) for v in filled]
    info.stats = {"min_length": min(lengths), "max_length": max(lengths)}
    role = "name" if info.meaning == "a name" else "text"
    return settle("text", role, n, [], "are free text")


def infer_table(table) -> list[ColumnInfo]:
    return [infer_column(name, table.column(name)) for name in table.columns]


def dataset_summary(table, columns: list[ColumnInfo]) -> dict:
    """Facts about the whole file, and a guess at what it is, labelled as such."""
    roles = Counter(c.role for c in columns)
    exact_dupes = table.size - len({tuple(r) for r in table.rows})
    guess = None
    names = " ".join(c.name.lower() for c in columns)
    if roles["measure"] and roles["date"] and re.search(r"sale|order|revenue|invoice|amount|price|مبيعات|طلب|فاتورة", names):
        guess = "sales or orders: dated records with amounts"
    elif re.search(r"customer|client|عميل", names):
        guess = "a customer list"
    elif re.search(r"supplier|vendor|مورد", names):
        guess = "a supplier list"
    elif re.search(r"employee|staff|موظف", names):
        guess = "an employee list"
    elif roles["measure"] and roles["date"]:
        guess = "dated measurements"
    return {
        "rows": table.size,
        "columns": len(columns),
        "exact_duplicate_rows": exact_dupes,
        "missing_cells": sum(c.blank + c.null_tokens for c in columns),
        "types": dict(Counter(c.type for c in columns)),
        "guess": guess,
    }
