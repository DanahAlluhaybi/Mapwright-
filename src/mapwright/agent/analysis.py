"""Analysis as a fixed set of queries that code runs.

A question becomes one of these query specs (by the request parser or a
model); this module computes every number from the table, and writes the
plain-language facts from those numbers only. A model never does
arithmetic here.

Queries: overview, summary, group, trend, share, relation, outliers,
missing, forecast. Any query takes ``where`` filters:
``[{"column": "City", "op": "in", "value": ["Jeddah"]}]`` with op
eq, in, not_blank, between (dates or numbers, inclusive).
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from collections import Counter, defaultdict

from ..normalize import match_key, parse_date, parse_number
import re

from .infer import UNITS, ColumnInfo, is_null, words
from .table import Table

AGGS = ("count", "sum", "mean", "median", "min", "max")
PERIODS = ("day", "week", "month", "quarter", "year")
MAX_GROUPS = 25


class QueryError(ValueError):
    """The query does not fit this table."""


# Reading typed values ------------------------------------------------------


class Typed:
    """Column values read as numbers or dates, using what inference found."""

    def __init__(self, table: Table, columns: list[ColumnInfo]):
        self.table = table
        self.info = {c.name: c for c in columns}

    def need(self, name: object, kinds: tuple[str, ...] | None = None) -> str:
        if not isinstance(name, str) or name not in self.table.columns:
            raise QueryError(f"no column {name!r}; columns are: {', '.join(self.table.columns)}")
        if kinds and self.info[name].type not in kinds:
            raise QueryError(f"{name} is {self.info[name].label}, not {' or '.join(kinds)}")
        return name

    def number(self, name: str, value: str) -> float | None:
        if not value.strip() or is_null(value):
            return None
        parsed = parse_number(value, UNITS)
        return float(parsed.value) if parsed else None

    def date(self, name: str, value: str) -> dt.date | None:
        if not value.strip() or is_null(value):
            return None
        for f in self.info[name].date_formats or ["%Y-%m-%d"]:
            if (d := parse_date(value, f)) is not None:
                return d
        return parse_date(value, "%Y-%m-%d")

    def label(self, value: str) -> str:
        return " ".join(value.split()) if value.strip() and not is_null(value) else "(missing)"


def _where(typed: Typed, rows: list[list[str]], where: object) -> tuple[list[list[str]], list[str]]:
    if not where:
        return rows, []
    if not isinstance(where, list):
        raise QueryError("where must be a list of filters")
    described = []
    for f in where:
        if not isinstance(f, dict):
            raise QueryError("each filter needs column, op and value")
        name = typed.need(f.get("column"))
        i = typed.table.index(name)
        op, value = f.get("op", "eq"), f.get("value")
        if op == "eq":
            rows = [r for r in rows if match_key(r[i]) == match_key(str(value))]
            described.append(f"{name} = {value}")
        elif op == "in":
            keys = {match_key(str(v)) for v in (value or [])}
            rows = [r for r in rows if match_key(r[i]) in keys]
            described.append(f"{name} in {', '.join(map(str, value or []))}")
        elif op == "not_blank":
            rows = [r for r in rows if r[i].strip() and not is_null(r[i])]
            described.append(f"{name} not missing")
        elif op == "between":
            low, high = (value[0], value[1]) if isinstance(value, list) and len(value) == 2 else (None, None)
            if typed.info[name].type == "date":
                lo = dt.date.fromisoformat(low) if low else None
                hi = dt.date.fromisoformat(high) if high else None
                keep = []
                for r in rows:
                    d = typed.date(name, r[i])
                    if d and (not lo or d >= lo) and (not hi or d <= hi):
                        keep.append(r)
                rows = keep
            else:
                keep = []
                for r in rows:
                    x = typed.number(name, r[i])
                    if x is not None and (low is None or x >= float(low)) and (high is None or x <= float(high)):
                        keep.append(r)
                rows = keep
            described.append(f"{name} between {low or '…'} and {high or '…'}")
        else:
            raise QueryError("filter op must be eq, in, not_blank or between")
    return rows, described


def _agg(values: list[float], agg: str) -> float:
    if agg == "count":
        return float(len(values))
    if not values:
        return float("nan")
    return {
        "sum": sum,
        "mean": statistics.fmean,
        "median": statistics.median,
        "min": min,
        "max": max,
    }[agg](values)


def fmt(x: float | int | None) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "–"
    if float(x).is_integer() and abs(x) < 1e15:
        return f"{int(x):,}"
    return f"{x:,.2f}"


def cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _metric_label(metric: str | None, agg: str) -> str:
    return "rows" if agg == "count" or not metric else f"{agg} of {metric}"


# Queries --------------------------------------------------------------------


def q_overview(typed: Typed, rows, spec) -> dict:
    facts = [f"{len(rows):,} rows and {len(typed.table.columns)} columns."]
    table_rows = []
    for name in typed.table.columns:
        c = typed.info[name]
        missing = c.blank + c.null_tokens
        table_rows.append([name, c.label, c.distinct, missing, ", ".join(c.examples[:3])])
    return {"title": "What is in this file", "table": {"columns": ["Column", "Type", "Distinct", "Missing", "Examples"], "rows": table_rows},
            "facts": facts, "chart": {"type": "none"}}


def q_summary(typed: Typed, rows, spec) -> dict:
    name = typed.need(spec.get("column"))
    i, c = typed.table.index(name), typed.info[name]
    values = [r[i] for r in rows]
    filled = [v for v in values if v.strip() and not is_null(v)]
    facts = [f"{len(filled):,} of {len(values):,} rows have a value ({len(values) - len(filled):,} missing)."]
    out = {"title": f"Summary of {name}", "facts": facts, "chart": {"type": "none"}}
    if c.type in ("integer", "decimal"):
        nums = [x for v in filled if (x := typed.number(name, v)) is not None]
        if nums:
            facts.append(f"Ranges from {fmt(min(nums))} to {fmt(max(nums))}; average {fmt(statistics.fmean(nums))}, "
                         f"median {fmt(statistics.median(nums))}, total {fmt(sum(nums))}.")
            out["table"] = {"columns": ["Statistic", "Value"], "rows": [
                ["count", fmt(len(nums))], ["min", fmt(min(nums))], ["max", fmt(max(nums))],
                ["mean", fmt(statistics.fmean(nums))], ["median", fmt(statistics.median(nums))], ["sum", fmt(sum(nums))]]}
    elif c.type == "date":
        dates = [d for v in filled if (d := typed.date(name, v))]
        if dates:
            facts.append(f"From {min(dates).isoformat()} to {max(dates).isoformat()}.")
    else:
        counts = Counter(typed.label(v) for v in filled)
        top = counts.most_common(10)
        facts.append(f"{len(counts):,} different values; the most common is {top[0][0]!r} ({top[0][1]:,} rows)." if top else "")
        out["table"] = {"columns": [name, "Rows"], "rows": [[k, fmt(v)] for k, v in top]}
        out["chart"] = {"type": "bar", "labels": [k for k, _ in top], "series": [{"name": "rows", "values": [v for _, v in top]}]}
    return out


def q_group(typed: Typed, rows, spec) -> dict:
    by = typed.need(spec.get("by"))
    agg = spec.get("agg") or ("sum" if spec.get("metric") else "count")
    if agg not in AGGS:
        raise QueryError(f"agg must be one of {', '.join(AGGS)}")
    metric = spec.get("metric")
    if agg != "count" or metric:
        metric = typed.need(metric, ("integer", "decimal")) if agg != "count" else metric
    bi = typed.table.index(by)
    mi = typed.table.index(metric) if metric and agg != "count" else None
    groups: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        key = typed.label(r[bi])
        if mi is None:
            groups[key].append(1.0)
        else:
            x = typed.number(metric, r[mi])
            if x is not None:
                groups[key].append(x)
    results = [(k, _agg(v, agg)) for k, v in groups.items()]
    order = spec.get("order", "desc")
    results.sort(key=lambda kv: (math.isnan(kv[1]), -kv[1] if order != "asc" else kv[1]))
    top = int(spec.get("top") or 10)
    shown = results[: max(1, min(top, MAX_GROUPS))]
    label = _metric_label(metric, agg)
    total = sum(v for _, v in results if not math.isnan(v)) if agg in ("sum", "count") else None
    facts = []
    if shown:
        first = shown[0]
        share = f" ({first[1] / total:.0%} of the total)" if total else ""
        word = "Highest" if order != "asc" else "Lowest"
        facts.append(f"{word} {label} by {by}: {first[0]} with {fmt(first[1])}{share}.")
        if len(shown) > 1:
            facts.append(f"Next: {shown[1][0]} with {fmt(shown[1][1])}.")
        facts.append(f"{len(results):,} groups in all" + (f"; showing the {'top' if order != 'asc' else 'bottom'} {len(shown)}." if len(results) > len(shown) else "."))
    return {
        "title": f"{cap(label)} by {by}",
        "total": total,
        "table": {"columns": [by, label], "rows": [[k, fmt(v)] for k, v in shown]},
        "chart": {"type": "bar", "labels": [k for k, _ in shown], "series": [{"name": label, "values": [None if math.isnan(v) else v for _, v in shown]}]},
        "facts": facts,
    }


def _period_start(d: dt.date, period: str) -> dt.date:
    if period == "day":
        return d
    if period == "week":
        return d - dt.timedelta(days=d.weekday())
    if period == "month":
        return d.replace(day=1)
    if period == "quarter":
        return d.replace(month=(d.month - 1) // 3 * 3 + 1, day=1)
    return d.replace(month=1, day=1)


def _next_period(d: dt.date, period: str) -> dt.date:
    if period == "day":
        return d + dt.timedelta(days=1)
    if period == "week":
        return d + dt.timedelta(days=7)
    months = {"month": 1, "quarter": 3, "year": 12}[period]
    m = d.month - 1 + months
    return d.replace(year=d.year + m // 12, month=m % 12 + 1, day=1)


def _series(typed: Typed, rows, spec, drop_partial: bool = False) -> tuple[list[dt.date], list[float], str, str, str | None]:
    date_col = typed.need(spec.get("date"), ("date",))
    period = spec.get("period", "month")
    if period not in PERIODS:
        raise QueryError(f"period must be one of {', '.join(PERIODS)}")
    agg = spec.get("agg") or ("sum" if spec.get("metric") else "count")
    if agg not in AGGS:
        raise QueryError(f"agg must be one of {', '.join(AGGS)}")
    metric = typed.need(spec.get("metric"), ("integer", "decimal")) if agg != "count" else None
    di = typed.table.index(date_col)
    mi = typed.table.index(metric) if metric else None
    buckets: dict[dt.date, list[float]] = defaultdict(list)
    last_date = None
    for r in rows:
        d = typed.date(date_col, r[di])
        if not d:
            continue
        last_date = max(last_date or d, d)
        if mi is None:
            buckets[_period_start(d, period)].append(1.0)
        else:
            x = typed.number(metric, r[mi])
            if x is not None:
                buckets[_period_start(d, period)].append(x)
    if not buckets:
        raise QueryError(f"no rows have both a date in {date_col}" + (f" and a number in {metric}" if metric else ""))
    start, end = min(buckets), max(buckets)
    typed.partial = None
    counts = sorted(len(v) for k, v in buckets.items() if k != end)
    thin = bool(counts) and len(buckets[end]) <= 0.5 * counts[len(counts) // 2]
    if period != "day" and last_date and last_date < _next_period(end, period) - dt.timedelta(days=1) and thin:
        typed.partial = (_period_label(end, period), last_date.isoformat())
        if drop_partial:
            buckets.pop(end)
            end = max(buckets)
    # fill empty periods so gaps show as gaps rather than disappearing
    periods, values = [], []
    current = start
    while current <= end and len(periods) < 2000:
        periods.append(current)
        values.append(_agg(buckets.get(current, []), agg) if buckets.get(current) else (0.0 if agg in ("count", "sum") else float("nan")))
        current = _next_period(current, period)
    return periods, values, agg, period, metric


def _period_label(d: dt.date, period: str) -> str:
    if period == "year":
        return str(d.year)
    if period == "quarter":
        return f"{d.year} Q{(d.month - 1) // 3 + 1}"
    if period == "month":
        return d.strftime("%Y-%m")
    return d.isoformat()


def q_trend(typed: Typed, rows, spec) -> dict:
    periods, values, agg, period, metric = _series(typed, rows, spec)
    label = _metric_label(metric, agg)
    labels = [_period_label(p, period) for p in periods]
    facts = []
    clean = [(l, v) for l, v in zip(labels, values) if not math.isnan(v)]
    if clean:
        best = max(clean, key=lambda lv: lv[1])
        worst = min(clean, key=lambda lv: lv[1])
        facts.append(f"{len(labels)} {period}s from {labels[0]} to {labels[-1]}.")
        facts.append(f"Highest {label}: {best[0]} ({fmt(best[1])}); lowest: {worst[0]} ({fmt(worst[1])}).")
        if len(clean) >= 2:
            first, last = clean[0][1], clean[-1][1]
            if first:
                facts.append(f"The last {period} is {(last - first) / abs(first):+.0%} against the first.")
            slope = _slope([v for _, v in clean])
            direction = "up" if slope > 0 else "down" if slope < 0 else "flat"
            facts.append(f"Overall direction: {direction} ({fmt(slope)} per {period} on a straight-line fit).")
    return {
        "title": f"{cap(label)} per {period}",
        "table": {"columns": [period, label], "rows": [[l, fmt(v)] for l, v in zip(labels, values)]},
        "chart": {"type": "line", "labels": labels, "series": [{"name": label, "values": [None if math.isnan(v) else v for v in values]}]},
        "facts": facts,
        "caveats": (["Periods with no rows count as zero for counts and sums."] if agg in ("count", "sum") else [])
        + ([f"The data ends on {typed.partial[1]}, so {typed.partial[0]} is incomplete and looks lower than it is."] if typed.partial else []),
    }


def _slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    xs = range(n)
    mx, my = (n - 1) / 2, statistics.fmean(values)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, values)) / den if den else 0.0


def q_share(typed: Typed, rows, spec) -> dict:
    out = q_group(typed, rows, {**spec, "agg": spec.get("agg") or ("sum" if spec.get("metric") else "count"), "top": spec.get("top", 10)})
    total = out.pop("total", None) or sum(v for v in out["chart"]["series"][0]["values"] if v)
    out["title"] = "Share of " + out["title"][0].lower() + out["title"][1:]
    out["table"]["columns"].append("Share")
    for row, v in zip(out["table"]["rows"], out["chart"]["series"][0]["values"]):
        row.append(f"{v / total:.1%}" if total and v else "–")
    return out


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return None


def q_relation(typed: Typed, rows, spec) -> dict:
    a, b = typed.need(spec.get("a")), typed.need(spec.get("b"))
    ia, ib = typed.table.index(a), typed.table.index(b)
    ta, tb = typed.info[a].type, typed.info[b].type
    numeric = ("integer", "decimal")
    caveat = "This shows how the two columns move together in this file. It does not show that one causes the other."
    if ta in numeric and tb in numeric:
        pairs = [(x, y) for r in rows if (x := typed.number(a, r[ia])) is not None and (y := typed.number(b, r[ib])) is not None]
        r = _pearson([p[0] for p in pairs], [p[1] for p in pairs])
        if r is None:
            raise QueryError("not enough rows with numbers in both columns")
        strength = "strong" if abs(r) >= 0.7 else "moderate" if abs(r) >= 0.4 else "weak" if abs(r) >= 0.1 else "no clear"
        facts = [f"Correlation r = {r:.2f} over {len(pairs):,} rows: a {strength} {'positive' if r > 0 else 'negative'} linear relationship."]
        return {"title": f"{a} and {b}", "facts": facts, "caveats": [caveat], "chart": {"type": "none"},
                "table": {"columns": ["Measure", "Value"], "rows": [["rows used", fmt(len(pairs))], ["correlation (r)", f"{r:.3f}"]]}}
    if tb in numeric or ta in numeric:
        cat, num = (a, b) if tb in numeric else (b, a)
        out = q_group(typed, rows, {"by": cat, "metric": num, "agg": "mean", "top": 15})
        out["title"] = f"Average {num} by {cat}"
        out["caveats"] = [caveat]
        return out
    # two categories: the most common combinations
    pairs = Counter((typed.label(r[ia]), typed.label(r[ib])) for r in rows)
    top = pairs.most_common(15)
    facts = [f"{len(pairs):,} different combinations; the most common is {top[0][0][0]} with {top[0][0][1]} ({top[0][1]:,} rows)."] if top else []
    return {"title": f"{a} by {b}", "facts": facts, "caveats": [caveat], "chart": {"type": "none"},
            "table": {"columns": [a, b, "Rows"], "rows": [[x, y, fmt(c)] for (x, y), c in top]}}


def q_outliers(typed: Typed, rows, spec) -> dict:
    name = typed.need(spec.get("column"), ("integer", "decimal"))
    i = typed.table.index(name)
    nums = [(rid, x) for rid, r in zip(typed.table.row_ids, rows) if (x := typed.number(name, r[i])) is not None]
    if len(nums) < 4:
        raise QueryError("too few numbers to judge what is unusual")
    q1, _, q3 = statistics.quantiles([x for _, x in nums], n=4)
    spread = q3 - q1
    low, high = q1 - 1.5 * spread, q3 + 1.5 * spread
    hits = sorted([(rid, x) for rid, x in nums if x < low or x > high], key=lambda t: -abs(t[1]))
    facts = [f"Typical range (middle half) is {fmt(q1)} to {fmt(q3)}; {len(hits):,} values fall outside {fmt(low)} to {fmt(high)}."]
    return {"title": f"Unusual values in {name}", "facts": facts, "chart": {"type": "none"},
            "caveats": ["Unusual is not the same as wrong: check these rows before excluding them."],
            "table": {"columns": ["Row", name], "rows": [[str(rid), fmt(x)] for rid, x in hits[:30]]}}


def q_missing(typed: Typed, rows, spec) -> dict:
    out = []
    for name in typed.table.columns:
        i = typed.table.index(name)
        missing = sum(1 for r in rows if not r[i].strip() or is_null(r[i]))
        if missing:
            out.append([name, missing, missing / len(rows) if rows else 0])
    out.sort(key=lambda r: -r[1])
    facts = [f"{len(out)} of {len(typed.table.columns)} columns have missing values."]
    if out:
        facts.append(f"Most missing: {out[0][0]} ({out[0][1]:,} rows, {out[0][2]:.0%}).")
    return {"title": "Missing values", "facts": facts,
            "table": {"columns": ["Column", "Missing rows", "Share"], "rows": [[n, fmt(m), f"{s:.0%}"] for n, m, s in out]},
            "chart": {"type": "bar", "labels": [r[0] for r in out[:15]], "series": [{"name": "missing rows", "values": [r[1] for r in out[:15]]}]}}


def _forecast_methods(history: list[float], horizon: int) -> dict[str, list[float]]:
    n = len(history)
    slope = _slope(history)
    intercept = statistics.fmean(history) - slope * (n - 1) / 2
    return {
        "last value": [history[-1]] * horizon,
        "average": [statistics.fmean(history)] * horizon,
        "straight-line trend": [intercept + slope * (n + h) for h in range(horizon)],
    }


def q_forecast(typed: Typed, rows, spec) -> dict:
    periods, values, agg, period, metric = _series(typed, rows, spec, drop_partial=True)
    partial = typed.partial
    horizon = int(spec.get("horizon") or 3)
    if not 1 <= horizon <= 24:
        raise QueryError("horizon must be 1 to 24 periods")
    history = [v for v in values if not math.isnan(v)]
    label = _metric_label(metric, agg)
    if len(history) < 8:
        return {"title": f"Forecast of {label}", "chart": {"type": "none"}, "facts": [
            f"Not enough history: {len(history)} {period}s. A forecast needs at least 8 to be checked against data it did not see."],
            "caveats": ["Try a shorter period (for example week instead of month) if the data allows it."]}
    test = max(2, min(horizon, len(history) // 4))
    train, actual = history[:-test], history[-test:]
    errors = {}
    for name, guess in _forecast_methods(train, test).items():
        errors[name] = statistics.fmean(abs(g - a) for g, a in zip(guess, actual))
    best = min(errors, key=errors.get)
    forecast = _forecast_methods(history, horizon)[best]
    future, current = [], periods[-1]
    for _ in range(horizon):
        current = _next_period(current, period)
        future.append(_period_label(current, period))
    labels = [_period_label(p, period) for p in periods]
    scale = statistics.fmean(abs(a) for a in actual) or 1
    facts = [
        f"Method chosen by testing on the last {test} {period}s it did not see: {best} "
        f"(average error {fmt(errors[best])}, about {errors[best] / scale:.0%} of typical values).",
        "Compared with: " + "; ".join(f"{k} {fmt(v)}" for k, v in errors.items() if k != best) + ".",
        f"Estimate for {future[0]}: {fmt(forecast[0])}" + (f"; for {future[-1]}: {fmt(forecast[-1])}." if horizon > 1 else "."),
    ]
    from .confidence import score_forecast

    return {
        "title": f"Forecast of {label} per {period} (estimate)",
        "estimate": True,
        "confidence": score_forecast(errors[best] / scale),
        "chart": {"type": "line", "labels": labels + future,
                  "series": [{"name": label, "values": [None if math.isnan(v) else v for v in values] + [None] * horizon},
                             {"name": "estimate", "values": [None] * (len(labels) - 1) + [values[-1]] + forecast}],
                  "forecast_from": len(labels)},
        "table": {"columns": [period, "estimate"], "rows": [[f, fmt(v)] for f, v in zip(future, forecast)]},
        "facts": facts,
        "caveats": ["These are estimates from past values alone; they do not know about prices, campaigns or other events.",
                    "Simple methods only: last value, average, straight-line trend."]
        + (["The test error is large compared with typical values, so treat the estimate as rough."] if errors[best] / scale > 0.25 else [])
        + ([f"{partial[0]} was left out because the data ends on {partial[1]}, part-way through it."] if partial else []),
    }


QUERIES = {
    "overview": q_overview,
    "summary": q_summary,
    "group": q_group,
    "trend": q_trend,
    "share": q_share,
    "relation": q_relation,
    "outliers": q_outliers,
    "missing": q_missing,
    "forecast": q_forecast,
}


def run_query(table: Table, columns: list[ColumnInfo], spec: object) -> dict:
    if not isinstance(spec, dict) or spec.get("q") not in QUERIES:
        raise QueryError(f"unknown query; known: {', '.join(QUERIES)}")
    typed = Typed(table, columns)
    rows, filters = _where(typed, table.rows, spec.get("where"))
    if not rows:
        raise QueryError("no rows match the filters")
    result = QUERIES[spec["q"]](typed, rows, spec)
    result.setdefault("caveats", [])
    result.setdefault("estimate", False)
    result["facts"] = [f for f in result.get("facts", []) if f]
    metric = spec.get("metric") or (spec.get("column") if spec["q"] == "summary" else None)
    if metric and spec.get("agg", "sum") != "count":
        for c in columns:
            filtered = any(f.get("column") == c.name and (f.get("op") == "eq" or len(f.get("value") or []) == 1)
                           for f in spec.get("where") or [] if isinstance(f, dict))
            if c.name != metric and not filtered and re.search(r"\b(curr|currency|ccy)\b|عملة", words(c.name)) and c.distinct > 1:
                result["caveats"].append(
                    f"{c.name} has {c.distinct} different currency values, so totals and averages of {metric} mix "
                    f"currencies unless you filter to one.")
    result["query"] = spec
    result["rows_used"] = len(rows)
    if filters:
        result["filters"] = filters
        result["facts"].insert(0, f"Filtered to {len(rows):,} rows: " + "; ".join(filters) + ".")
    return result
