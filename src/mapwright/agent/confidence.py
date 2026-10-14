"""How sure the agent is about a proposed change, and why.

The score is the product of a few plain factors, each with its own reason,
so the page can show where the doubt comes from instead of one opaque
number:

* understanding: how the request became this operation (a code check on
  the data, an option you picked, a built-in phrase, or an AI model);
* fit: how much of the column the operation actually handles (values it
  cannot convert lower it), and how clearly the column is of the type the
  operation expects;
* data loss: removed rows that differ from the kept ones, or filled values
  that are only estimates;
* your decisions: earlier approvals and rejections of the same kind of
  change in this session.

It is a guide for the reviewer, not a probability that was measured.
"""

from __future__ import annotations

from collections import Counter

from .infer import ColumnInfo

SOURCES = {
    "finding": (0.95, "suggested by a check that ran on the data"),
    "choice": (1.0, "you picked this option yourself"),
    "page": (1.0, "you asked for this exact change"),
    "user": (1.0, "you asked for this exact change"),
    "rules": (0.9, "your request matched a built-in phrase"),
}
MODEL_SOURCE = (0.75, "an AI model read your request; check that the column and the change are what you meant")

# operations that convert values to a type, and the column types they expect
CONVERTS = {
    "to_boolean": ("boolean",),
    "parse_date": ("date",),
    "parse_number": ("integer", "decimal"),
    "normalize_email": ("email",),
    "normalize_phone": ("phone",),
}


def level(score: float) -> str:
    return "high" if score >= 0.85 else "medium" if score >= 0.6 else "low"


def _factor(name: str, value: float, why: str) -> dict:
    return {"name": name, "value": round(max(0.0, min(1.0, value)), 3), "why": why}


def score_proposal(spec: dict, effect: dict, source: str, columns: list[ColumnInfo], history: list[dict]) -> dict:
    info = {c.name: c for c in columns}
    column = info.get(spec.get("column") or "")
    op = spec.get("op")
    factors = []

    value, why = SOURCES.get(source, MODEL_SOURCE)
    factors.append(_factor("Understanding", value, why))

    if not effect["cells_changed"] and not effect["rows_removed"] and not effect.get("columns_removed") and not effect.get("renamed"):
        factors.append(_factor("Effect", 0.5, "nothing in the data would change"))

    if op in CONVERTS and column is not None:
        filled = max(column.filled, 1)
        left = effect.get("unmatched_total", 0)
        share = 1 - left / filled
        why = (f"all {column.filled:,} filled values can be handled" if not left
               else f"{left:,} of {column.filled:,} filled values cannot be converted and stay as they are")
        factors.append(_factor("Fit", share, why))
        if column.type in CONVERTS[op]:
            factors.append(_factor("Column type", max(column.confidence, 0.5),
                                   f"{column.confidence:.0%} of the values look like {column.label}"))
        else:
            factors.append(_factor("Column type", 0.7, f"the column was read as {column.label}, not the type this change expects"))
        if op == "parse_date" and column.date_ambiguous and not spec.get("order"):
            factors.append(_factor("Date order", 0.7, "some dates could be day-first or month-first; pick one to be sure"))
    elif op == "split_column" and column is not None:
        left = effect.get("unmatched_total", 0)
        factors.append(_factor("Fit", 1 - left / max(column.filled, 1),
                               "every value has the separator" if not left else f"{left:,} values have no separator and are left as they are"))
    elif op == "fill_missing" and column is not None:
        strategy = spec.get("strategy")
        missing_share = (column.blank + column.null_tokens) / max(column.rows, 1)
        if strategy == "mode" and column.top_values:
            top = column.top_values[0][1] / max(column.filled, 1)
            factors.append(_factor("Fill value", 0.5 + top / 2, f"the most common value covers {top:.0%} of the filled rows"))
        elif strategy in ("median", "mean"):
            factors.append(_factor("Fill value", 0.65, f"the {strategy} is a typical value, not the real one"))
        else:
            factors.append(_factor("Fill value", 0.9, "you chose the value"))
        if missing_share > 0.3:
            factors.append(_factor("Amount filled", 0.7, f"{missing_share:.0%} of the column would be invented values"))
    elif op == "deduplicate":
        removed = effect["rows_removed"]
        conflicts = (effect.get("stats") or {}).get("conflicts", 0)
        if removed:
            factors.append(_factor("Data loss", 1 - 0.5 * conflicts / removed,
                                   "the removed rows match the kept ones" if not conflicts
                                   else f"{conflicts:,} of {removed:,} removed rows differ from the kept row in other columns"))

    decided = Counter(h["event"] for h in history
                      if h.get("event") in ("applied", "rejected") and (h.get("op") or {}).get("op") == op)
    approved, rejected = decided["applied"], decided["rejected"]
    if rejected and not approved:
        factors.append(_factor("Your decisions", 0.85, f"you rejected this kind of change {rejected} time{'s' * (rejected > 1)} before"))
    elif rejected:
        factors.append(_factor("Your decisions", 0.95, f"you approved this kind of change {approved} and rejected it {rejected} times"))
    elif approved:
        factors.append(_factor("Your decisions", 1.0, f"you approved this kind of change {approved} time{'s' * (approved > 1)} before"))

    score = 1.0
    for f in factors:
        score *= f["value"]
    weakest = min(factors, key=lambda f: f["value"])
    return {"score": round(score, 3), "level": level(score), "factors": factors,
            "summary": "No doubts found." if weakest["value"] >= 0.99 else "Main doubt: " + weakest["why"] + "."}


def score_forecast(error_share: float) -> dict:
    value = 1 - min(error_share, 1)
    return {"score": round(value, 3), "level": level(value),
            "factors": [_factor("Test error", value, f"on periods it did not see, the error was about {error_share:.0%} of typical values")],
            "summary": f"Checked on past periods: off by about {error_share:.0%} on average."}
