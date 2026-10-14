"""Data-quality findings for any table, each with the fixes that can be previewed.

Severity says how sure the finding is, not how bad it is:

- ``error``: breaks what the column was confirmed or inferred to be
  (a value that is not a date in a date column, a repeated identifier).
- ``anomaly``: looks wrong but may be right (an extreme number, two
  spellings of what may be one category).
- ``recommendation``: a format that works but is inconsistent.

Every count comes from the data. Fixes are operation specs from ``ops``;
nothing here changes the table.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict

from ..normalize import clean_spaces, is_email, match_key, parse_number
from .infer import UNITS, ColumnInfo, date_label, is_null, words
from .table import Table

EXAMPLES = 6


SINGULAR = [(" are ", " is "), (" appear ", " appears "), (" look ", " looks "), (" repeat ", " repeats "),
            (" match ", " matches "), (" have ", " has ")]


def _singular(title: str) -> str:
    """'1 values in X are not ...' reads as '1 value in X is not ...'."""
    m = re.match(r"1 (unusually large or small )?(\w+?)s\b", title)
    if not m:
        return title
    title = title[: m.end() - 1] + title[m.end():]
    for plural, single in SINGULAR:
        if plural in title:
            title = title.replace(plural, single, 1)
            break
    m = re.search(r" is not (.+?)s$", title)
    if m:
        article = "an" if m.group(1)[0].lower() in "aeiou" else "a"
        title = title[: m.start()] + f" is not {article} {m.group(1)}"
    return title


def _finding(column, severity, kind, title, detail, count, examples=None, fixes=None, rows=None) -> dict:
    title = _singular(title)
    return {
        "id": f"{kind}:{column or '*'}",
        "column": column,
        "severity": severity,
        "kind": kind,
        "title": title,
        "detail": detail,
        "count": count,
        "examples": (examples or [])[:EXAMPLES],
        "rows": (rows or [])[:50],
        "fixes": fixes or [],
    }


def column_findings(table: Table, info: ColumnInfo) -> list[dict]:
    name, out = info.name, []
    values = table.column(name)
    filled = [v for v in values if v.strip() and not is_null(v)]

    if info.type == "empty":
        return [
            _finding(name, "recommendation", "empty", f"{name} is empty", "No row has a value in this column.",
                     info.rows, fixes=[{"op": "drop_column", "column": name}])
        ]

    if info.null_tokens:
        tokens = Counter(v.strip() for v in values if v.strip() and is_null(v))
        out.append(_finding(
            name, "recommendation", "placeholders", f"Placeholders for missing values in {name}",
            f"{info.null_tokens} cells hold text such as {', '.join(repr(t) for t in tokens)} instead of being empty.",
            info.null_tokens, list(tokens), [{"op": "set_missing", "column": name}],
        ))
    missing = info.blank + info.null_tokens
    if missing and info.rows and missing / info.rows >= 0.05:
        out.append(_finding(
            name, "recommendation", "missing", f"{missing / info.rows:.0%} of {name} is missing",
            f"{missing} of {info.rows} rows have no value. Decide whether to leave them, fill them with a value you "
            "choose, or remove those rows; Mapwright does not guess missing values.",
            missing,
        ))

    if info.misfits and info.type not in ("text", "category", "email"):
        bad = [v for v in filled if v in set(info.misfits)] or info.misfits
        count = sum(1 for v in filled if _misfit(info, v))
        out.append(_finding(
            name, "error", "type", f"{count} values in {name} are not {info.label}s",
            f"{info.confidence:.0%} of the values read as {info.label}; these do not.", count, list(dict.fromkeys(bad)),
            rows=[rid for rid, v in zip(table.row_ids, values) if v.strip() and not is_null(v) and _misfit(info, v)],
        ))

    if info.type == "date":
        iso = all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v.strip()) for v in filled)
        if info.date_ambiguous:
            out.append(_finding(
                name, "anomaly", "date_order", f"Day and month order in {name} is unclear",
                "Every value reads both as day/month and month/day, so the order has to come from you.",
                len(filled), filled[:EXAMPLES],
                [{"op": "parse_date", "column": name, "order": "day_first"},
                 {"op": "parse_date", "column": name, "order": "month_first"}],
            ))
        elif not iso:
            formats = ", ".join(date_label(f) for f in info.date_formats)
            out.append(_finding(
                name, "recommendation", "date_format", f"Dates in {name} are not in one standard format",
                f"Written as {formats}. A single format (yyyy-mm-dd) sorts and compares correctly.",
                len(filled), filled[:EXAMPLES], [{"op": "parse_date", "column": name}],
            ))

    if info.type in ("integer", "decimal"):
        plain = re.compile(r"^-?\d+(\.\d+)?$")
        styled = [v for v in filled if not plain.match(v.strip()) and parse_number(v, UNITS) is not None]
        if styled:
            out.append(_finding(
                name, "recommendation", "number_format", f"Numbers in {name} carry separators or other characters",
                "Values such as these are read as numbers but are not stored as plain numbers.",
                len(styled), list(dict.fromkeys(styled)), [{"op": "parse_number", "column": name}],
            ))
        units = Counter(p.unit for v in filled if (p := parse_number(v, UNITS)) is not None and p.unit)
        if len(units) > 1:
            out.append(_finding(
                name, "anomaly", "currencies", f"{name} mixes currencies",
                "Values carry " + ", ".join(f"{u} ({c})" for u, c in units.most_common())
                + ". Sums and averages are not meaningful until they are converted to one currency.",
                sum(units.values()) - units.most_common(1)[0][1],
                [v for v in filled if (p := parse_number(v, UNITS)) is not None and p.unit and p.unit != units.most_common(1)[0][0]],
            ))
        out.extend(_outliers(table, info))

    if info.type == "boolean":
        keys = {v.strip() for v in filled}
        if len(keys) > 2:
            out.append(_finding(
                name, "recommendation", "boolean_words", f"{name} uses {len(keys)} different words for yes/no",
                "For example " + ", ".join(sorted(keys)[:8]) + ".", len(filled), sorted(keys),
                [{"op": "to_boolean", "column": name}],
            ))

    if info.type == "email":
        upper = [v for v in filled if v != clean_spaces(v).lower()]
        if upper:
            out.append(_finding(name, "recommendation", "email_case", f"Emails in {name} mix upper case or spaces",
                                "Emails are not case-sensitive; one form makes duplicates visible.", len(upper), upper,
                                [{"op": "normalize_email", "column": name}]))
        invalid = [v for v in filled if not is_email(clean_spaces(v).lower())]
        if invalid:
            out.append(_finding(name, "anomaly", "email_invalid", f"{len(invalid)} values in {name} are not valid emails",
                                "They may be typos or another kind of value in the wrong column.", len(invalid), invalid))

    if info.type == "phone":
        out.append(_finding(name, "recommendation", "phone_format", f"Phone numbers in {name} are written in different ways",
                            "International format (+966...) makes them comparable.", len(filled), filled[:EXAMPLES],
                            [{"op": "normalize_phone", "column": name}]))

    if info.type in ("category", "text", "identifier"):
        spaced = [v for v in filled if v != clean_spaces(v)]
        if spaced:
            out.append(_finding(name, "recommendation", "spaces", f"Extra spaces in {name}",
                                "Leading, trailing or double spaces make equal values look different.", len(spaced),
                                [repr(v) for v in spaced], [{"op": "normalize_text", "column": name}]))

    if info.type in ("category", "text"):
        groups = defaultdict(Counter)
        for v in filled:
            groups[match_key(v)][clean_spaces(v)] += 1
        variants = {k: c for k, c in groups.items() if len(c) > 1}
        if variants:
            mapping = {}
            for counter in variants.values():
                keep = counter.most_common(1)[0][0]
                for spelling in counter:
                    if spelling != keep:
                        mapping[spelling] = keep
            examples = [" / ".join(c) for c in variants.values()]
            out.append(_finding(
                name, "anomaly", "spellings", f"Same value spelled differently in {name}",
                f"{len(variants)} values appear with different capitalisation or spacing. Merging keeps the most common spelling.",
                sum(sum(c.values()) - c.most_common(1)[0][1] for c in variants.values()), examples,
                [{"op": "map_values", "column": name, "mapping": mapping}],
            ))

    if _key_like(info):
        counts = Counter(match_key(v) for v in filled)
        repeated = {k: c for k, c in counts.items() if c > 1}
        if repeated:
            rows = [rid for rid, v in zip(table.row_ids, values) if match_key(v) in repeated]
            strict = info.type == "identifier" or info.role == "identifier" or KEY_WORDS.search(words(name))
            what = "identifiers" if strict else "values"
            out.append(_finding(
                name, "error" if strict else "anomaly", "duplicate_values",
                f"{len(repeated)} {what} in {name} appear more than once ({sum(repeated.values())} rows)",
                ("An identifier should point to one record. " if strict else
                 "Several rows share the same value; they may be the same record entered twice. ")
                + "Compare the rows before removing any.",
                sum(repeated.values()), [f"{k} ×{c}" for k, c in sorted(repeated.items(), key=lambda kv: -kv[1])],
                [{"op": "deduplicate", "columns": [name]}], rows))
        if info.role in ("name", "text") or info.meaning == "a name":
            out.extend(_near_duplicates(table, name, values))
    return out


# Duplicate detection --------------------------------------------------------

KEY_WORDS = re.compile(
    r"\b(id|code|no|nr|number|ref|key|sku|vat|tax|tin|trn|cr|iban|account|registration|license|licence)\b"
    r"|رقم|كود|ضريب|سجل|ايبان|حساب|ترخيص"
)
LEGAL_WORDS = {
    "co", "company", "corp", "corporation", "inc", "llc", "ltd", "limited", "est", "establishment", "trading",
    "group", "the", "and", "for", "of", "plc", "wll", "llp", "intl", "international",
    "شركة", "شركه", "مؤسسة", "مؤسسه", "للتجارة", "للتجاره", "التجارية", "التجاريه", "المحدودة", "المحدوده", "ذمم", "و",
}


def _key_like(info: ColumnInfo) -> bool:
    """Columns where a repeated value usually means a repeated record."""
    if info.type in ("identifier", "email", "phone") or info.role in ("identifier", "name"):
        return True
    if info.meaning in ("an identifier or reference number", "a name", "an email address", "a phone number"):
        return info.type not in ("category", "boolean", "date")
    return bool(KEY_WORDS.search(words(info.name))) and info.type not in ("boolean", "date")


def name_key(text: str) -> str:
    """A loose form of a name for spotting near-duplicates: case, punctuation,
    Arabic letter variants and legal words such as "Co." or "شركة" are ignored."""
    t = text.casefold()
    t = re.sub("[أإآ]", "ا", t).replace("ة", "ه").replace("ى", "ي")
    t = re.sub(r"[^\w\s]|_", " ", t)
    parts = [w.removeprefix("ال") if len(w) > 4 and w.startswith("ال") else w for w in t.split()]
    parts = [w for w in parts if w not in LEGAL_WORDS]
    return " ".join(parts)


def _near_duplicates(table: Table, name: str, values: list[str]) -> list[dict]:
    groups: dict[str, Counter] = defaultdict(Counter)
    rows: dict[str, list[int]] = defaultdict(list)
    for rid, v in zip(table.row_ids, values):
        if v.strip() and not is_null(v):
            key = name_key(v)
            if key:
                groups[key][clean_spaces(v)] += 1
                rows[key].append(rid)
    # only spellings that differ beyond case and spacing; those are the "spellings" finding
    near = {k: c for k, c in groups.items() if len({match_key(s) for s in c}) > 1}
    if not near:
        return []
    mapping = {}
    for counter in near.values():
        keep = counter.most_common(1)[0][0]
        mapping.update({s: keep for s in counter if s != keep})
    return [_finding(
        name, "anomaly", "near_duplicates", f"{len(near)} names in {name} look like the same entity written differently",
        "Ignoring case, punctuation, Arabic letter variants and words like Co., Ltd, شركة, مؤسسة. "
        "Check them: unifying the spelling makes duplicates visible, removing rows is a separate step.",
        sum(sum(c.values()) for c in near.values()), [" / ".join(c) for c in near.values()],
        [{"op": "map_values", "column": name, "mapping": mapping}],
        [r for k in near for r in rows[k]],
    )]


def _misfit(info: ColumnInfo, value: str) -> bool:
    return value in info.misfits or clean_spaces(value) in info.misfits


def _outliers(table: Table, info: ColumnInfo) -> list[dict]:
    stats = info.stats
    if not stats or "q1" not in stats:
        return []
    q1, q3 = stats["q1"], stats["q3"]
    spread = q3 - q1
    if spread <= 0:
        return []
    low, high = q1 - 3 * spread, q3 + 3 * spread
    hits = []
    for rid, v in zip(table.row_ids, table.column(info.name)):
        parsed = parse_number(v, UNITS) if v.strip() else None
        if parsed is not None and not low <= float(parsed.value) <= high:
            hits.append((rid, v))
    if not hits:
        return []
    return [_finding(
        info.name, "anomaly", "outliers", f"{len(hits)} unusually large or small values in {info.name}",
        f"Outside {low:,.2f} to {high:,.2f} (three times the middle half's spread beyond it). "
        "They may be real; check them before analysis.",
        len(hits), [v for _, v in hits], rows=[r for r, _ in hits],
    )]


def table_findings(table: Table, columns: list[ColumnInfo]) -> list[dict]:
    out = []
    seen, dupes = {}, []
    for rid, row in zip(table.row_ids, table.rows):
        key = tuple(match_key(v) for v in row)
        if key in seen:
            dupes.append(rid)
        else:
            seen[key] = rid
    if dupes:
        out.append(_finding(None, "anomaly", "duplicate_rows", f"{len(dupes)} rows repeat an earlier row exactly",
                            "Every value matches an earlier row (ignoring case and spaces).", len(dupes),
                            [f"row {r}" for r in dupes], [{"op": "deduplicate"}], dupes))
    # the same record entered twice under two different IDs: equal in everything but the ID columns
    ids = [i for i, c in enumerate(columns) if c.type == "identifier" or c.role == "identifier"]
    rest = [i for i in range(len(columns)) if i not in ids and columns[i].type != "empty"]
    if ids and len(rest) >= 2:
        seen2, twins = {}, []
        for rid, row in zip(table.row_ids, table.rows):
            key = tuple(match_key(row[i]) for i in rest)
            if not any(key):
                continue
            if key in seen2 and tuple(match_key(row[i]) for i in ids) != seen2[key][1]:
                twins.append((seen2[key][0], rid))
            else:
                seen2.setdefault(key, (rid, tuple(match_key(row[i]) for i in ids)))
        if twins:
            id_names = ", ".join(columns[i].name for i in ids)
            out.append(_finding(None, "anomaly", "duplicate_except_id",
                                f"{len(twins)} rows match another row in everything except {id_names}",
                                "Often the same record created twice with a new ID. Merging them needs a decision about which ID to keep.",
                                len(twins), [f"rows {a} and {b}" for a, b in twins],
                                [{"op": "deduplicate", "columns": [columns[i].name for i in rest]}], [b for _, b in twins]))
    out.extend(_date_order_checks(table, columns))
    return out


START = re.compile(r"\b(start|from|begin|open|opened|created|issued|order|signup|joined)\b")
END = re.compile(r"\b(end|to|until|close|closed|due|expiry|expires|updated|delivered|shipped|finish)\b")


def _date_order_checks(table: Table, columns: list[ColumnInfo]) -> list[dict]:
    """Cross-column check: an end date before its start date."""
    from ..normalize import parse_date

    dates = [c for c in columns if c.type == "date" and not c.date_ambiguous]
    out = []
    for a in dates:
        for b in dates:
            if a is b or not START.search(words(a.name)) or not END.search(words(b.name)):
                continue
            bad = []
            for rid, x, y in zip(table.row_ids, table.column(a.name), table.column(b.name)):
                dx = next((d for f in a.date_formats if (d := parse_date(x, f))), None)
                dy = next((d for f in b.date_formats if (d := parse_date(y, f))), None)
                if dx and dy and dy < dx:
                    bad.append((rid, f"{x} → {y}"))
            if bad:
                out.append(_finding(f"{a.name}, {b.name}", "anomaly", "date_order_pair",
                                    f"{len(bad)} rows have {b.name} before {a.name}",
                                    "An end date earlier than its start date is usually an entry error.", len(bad),
                                    [t for _, t in bad], rows=[r for r, _ in bad]))
    return out


def all_findings(table: Table, columns: list[ColumnInfo]) -> list[dict]:
    findings = [f for c in columns for f in column_findings(table, c)] + table_findings(table, columns)
    order = {"error": 0, "anomaly": 1, "recommendation": 2}
    findings.sort(key=lambda f: (order[f["severity"]], -f["count"]))
    return findings


def quality_score(table: Table, columns: list[ColumnInfo], findings: list[dict]) -> float:
    """Share of cells with no error or anomaly finding, a rough single number for monitoring."""
    cells = table.size * max(len(table.columns), 1)
    flagged = sum(f["count"] for f in findings if f["severity"] in ("error", "anomaly"))
    return round(max(0.0, 1 - flagged / cells), 4) if cells else 1.0
