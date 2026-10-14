"""Turning a request in plain language into operations or queries.

Two interpreters share one output format and one validator:

- ``rules``: keyword patterns in English and Arabic. Free, instant, works
  with no model, and handles the common requests.
- ``model``: a language model (Gemini or a local Ollama model) is shown the
  column summaries, the selected columns and the vocabulary, and answers
  with JSON. Its answer is validated exactly like the rules' answer; an
  invalid answer becomes a question back to the user, never an action.

Output kinds: ``proposal`` (operations to preview), ``clarify`` (a question
with options), ``analysis`` (queries to run), ``findings``, ``answer``,
``report``, ``dashboard``, ``unknown``.
"""

from __future__ import annotations

import json
import re

from ..normalize import match_key
from . import analysis, ops
from .infer import FALSE_TOKENS, TRUE_TOKENS, ColumnInfo, words
from .table import Table

PROMPT_VERSION = "agent-v1"


def _has(text: str, pattern: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


def mentioned_columns(text: str, columns: list[ColumnInfo]) -> list[str]:
    lowered = " " + words(text) + " "
    found = []
    for c in sorted(columns, key=lambda c: -len(c.name)):
        key = words(c.name)
        if key and f" {key} " in lowered and c.name not in found:
            found.append(c.name)
            lowered = lowered.replace(f" {key} ", " ")
    order = {c.name: i for i, c in enumerate(columns)}
    return sorted(found, key=lambda n: order[n])


def _by_type(columns: list[ColumnInfo], *types: str) -> list[ColumnInfo]:
    return [c for c in columns if c.type in types]


def _money_first(columns: list[ColumnInfo]) -> list[ColumnInfo]:
    measures = _by_type(columns, "integer", "decimal")
    return sorted(measures, key=lambda c: c.meaning != "a money amount")


def _pick_column_question(action: str, candidates: list[ColumnInfo], build) -> dict:
    options = [{"label": c.name, **build(c.name)} for c in candidates[:8]]
    return {"kind": "clarify", "question": f"Which column should I {action}?", "options": options}


PERIOD_WORDS = [
    (r"\bday|daily|يوم", "day"),
    (r"\bweek|weekly|أسبوع|اسبوع", "week"),
    (r"\bquarter|quarterly|ربع", "quarter"),
    (r"\byear|annual|yearly|سنة|سنوي|سنه", "year"),
    (r"\bmonth|monthly|شهر", "month"),
]


def _period(text: str) -> str:
    for pattern, period in PERIOD_WORDS:
        if _has(text, pattern):
            return period
    return "month"


def interpret_rules(text: str, selected: list[str], table: Table, columns: list[ColumnInfo]) -> dict:
    t = text.strip()
    info = {c.name: c for c in columns}
    named = mentioned_columns(t, columns)
    cols = named or [s for s in selected if s in info]

    # --- reports, dashboards, overview -----------------------------------
    if _has(t, r"dashboard|لوحة"):
        return {"kind": "dashboard", "message": "Opening the dashboard with the results you added."}
    if _has(t, r"\breport\b|تقرير"):
        return {"kind": "report", "message": "Here is the report for this session."}
    if _has(t, r"quality|issues|problems|errors|مشاكل|جودة|أخطاء|اخطاء") and not _has(t, r"fix|صلح|عالج"):
        return {"kind": "findings", "columns": cols, "message": "These are the data-quality findings" + (f" for {', '.join(cols)}." if cols else ".")}
    if _has(t, r"useful|relevant|which columns|أي أعمدة|اي اعمدة|مفيد"):
        return {"kind": "answer", "message": _useful_columns(t, columns)}

    # --- renames and structure --------------------------------------------
    m = re.search(r"rename\s+(.+?)\s+(?:to|as)\s+(.+)", t, re.IGNORECASE) or re.search(r"(?:سم[ّ]?ي|غير اسم)\s+(.+?)\s+(?:إلى|الى|ل)\s*(.+)", t)
    if m:
        source = mentioned_columns(m.group(1), columns) or cols
        if source:
            return {"kind": "proposal", "ops": [{"op": "rename_column", "column": source[0], "new_name": m.group(2).strip(" '\".")}]}
    if _has(t, r"(drop|remove|delete)\s+(the\s+)?column|احذف\s+(ال)?عمود|شيل\s+(ال)?عمود"):
        if cols:
            return {"kind": "proposal", "ops": [{"op": "drop_column", "column": c} for c in cols]}
        return _pick_column_question("remove", columns, lambda n: {"op": {"op": "drop_column", "column": n}})
    m = re.search(r"split\s+.*?\s+(?:by|on|at)\s+['\"]?(.+?)['\"]?\s+into\s+(.+)", t, re.IGNORECASE)
    if m and cols:
        names = [n.strip(" '\"") for n in re.split(r",|\band\b|و", m.group(2)) if n.strip(" '\"")]
        return {"kind": "proposal", "ops": [{"op": "split_column", "column": cols[0], "separator": m.group(1), "into": names}]}

    # --- explicit value mappings: "a = b, c -> d" ---------------------------
    body = t.rsplit(":", 1)[-1] if ":" in t else t
    pairs = re.findall(r"['\"]?([^,;=\n>]+?)['\"]?\s*(?:=>|->|=|→)\s*['\"]?([^,;\n]+?)['\"]?(?:[,;\n]|$)", body)
    if pairs and cols:
        return {"kind": "proposal", "ops": [{"op": "map_values", "column": cols[0], "mapping": {a.strip(): b.strip() for a, b in pairs}}]}

    # --- missing values ------------------------------------------------------
    if _has(t, r"missing|empty|blank|null|ناقص|فارغ|مفقود|فاضي"):
        targets = cols or [c.name for c in columns if c.blank + c.null_tokens]
        if _has(t, r"remove|delete|drop|احذف|شيل") and targets:
            return {"kind": "proposal", "ops": [{"op": "drop_rows", "column": c, "where": "missing"} for c in targets[:1]]}
        if _has(t, r"fill|impute|replace|عب|املأ|املا|عوض") and targets:
            name = targets[0]
            numeric = info[name].type in ("integer", "decimal")
            options = [{"label": "Leave them empty (no change)", "message": "Nothing will change."},
                       {"label": "Fill with the most common value", "op": {"op": "fill_missing", "column": name, "strategy": "mode"}}]
            if numeric:
                options.append({"label": "Fill with the median", "op": {"op": "fill_missing", "column": name, "strategy": "median"}})
            options.append({"label": "Remove the rows where it is missing", "op": {"op": "drop_rows", "column": name, "where": "missing"}})
            options.append({"label": "Fill with a value I choose", "ask_value": {"op": "fill_missing", "column": name, "strategy": "constant"}})
            return {"kind": "clarify", "question": f"How should the missing values in {name} be handled? Filled values are estimates, not real data.", "options": options}
        result = {"kind": "analysis", "queries": [{"q": "missing"}]}
        placeholders = [{"op": "set_missing", "column": c} for c in targets if info[c].null_tokens]
        if placeholders:
            result["ops"] = placeholders
        return result

    # --- duplicates ----------------------------------------------------------
    if _has(t, r"duplicat|repeated|مكرر|تكرار"):
        spec = {"op": "deduplicate"}
        if cols:
            spec["columns"] = cols
        return {"kind": "proposal", "ops": [spec]}

    # --- conversions on columns ---------------------------------------------
    conversions = [
        (r"bool|yes\s*/?\s*no|true\s*/?\s*false|نعم\s*/?\s*لا|منطقي|صح\s*و\s*خطأ", "to_boolean", ("boolean", "category", "integer", "text")),
        (r"phone|mobile|جوال|هاتف", "normalize_phone", ("phone", "integer", "text", "identifier")),
        (r"e-?mail|بريد|ايميل|إيميل", "normalize_email", ("email",)),
        (r"\bdate|تاريخ|تواريخ", "parse_date", ("date",)),
        (r"number|numeric|amount|رقم|ارقام|أرقام|مبلغ", "parse_number", ("integer", "decimal")),
    ]
    wants_change = _has(t, r"convert|change|format|standard|consistent|clean|fix|turn|make|write|plain|parse|normali|حول|غير|وحد|نظف|صلح|خلي|صيغة|موحد|اكتب")
    for pattern, op, types in conversions:
        if _has(t, pattern) and (wants_change or op == "to_boolean"):
            targets = cols or [c.name for c in _by_type(columns, *types)]
            if not targets:
                return {"kind": "unknown", "message": f"I could not find a column to {op.replace('_', ' ')}. Select one and ask again."}
            if not cols and len(targets) > 1:
                return _pick_column_question("change", [info[n] for n in targets], lambda n, op=op: {"op": {"op": op, "column": n}})
            return _conversion(op, targets, table, info)

    case = None
    if _has(t, r"upper|capital|كبيرة|كابتل"):
        case = "upper"
    elif _has(t, r"lower|صغيرة"):
        case = "lower"
    elif _has(t, r"title|proper|first letter|أول حرف"):
        case = "title"
    if case or _has(t, r"trim|spaces|tidy|مسافات|رتب"):
        if not cols:
            return _pick_column_question("tidy", _by_type(columns, "text", "category", "identifier"),
                                         lambda n: {"op": {"op": "normalize_text", "column": n, "case": case or "none"}})
        return {"kind": "proposal", "ops": [{"op": "normalize_text", "column": c, "case": case or "none"} for c in cols]}

    if _has(t, r"standard|unify|consistent|merge|same|\bmap\b|group (these|the) values|توحيد|وحد|موحد|ادمج|صنف"):
        if not cols:
            return _pick_column_question("standardise", _by_type(columns, "category", "text"),
                                         lambda n: {"op": {"op": "normalize_text", "column": n}})
        from .quality import column_findings

        fixes = [f for c in cols for x in column_findings(table, info[c]) if x["kind"] in ("spellings", "spaces") for f in x["fixes"]]
        for c in cols:
            if info[c].role == "name":
                fixes = [f for f in fixes if not (f["op"] == "normalize_text" and f["column"] == c)]
                fixes.insert(0, {"op": "normalize_text", "column": c, "case": "title"})
        if fixes:
            return {"kind": "proposal", "ops": fixes}
        values = info[cols[0]].top_values[:12]
        return {"kind": "clarify", "question": (
            f"{cols[0]} has no spelling variants I can merge safely. To map values to standard ones, write them like "
            f"'old = new, old = new'. Current values: " + ", ".join(v for v, _ in values) + "."), "options": []}

    # --- analysis ------------------------------------------------------------
    numeric = [c for c in cols if info[c].type in ("integer", "decimal")]
    categories = [c for c in cols if info[c].type in ("category", "text", "boolean", "identifier")]
    dates = [c for c in cols if info[c].type == "date"] or [c.name for c in _by_type(columns, "date")]
    metric = numeric[0] if numeric else None

    if _has(t, r"forecast|predict|projection|next (month|quarter|year)|توقع|تنبؤ|المستقبل"):
        if not dates:
            return {"kind": "unknown", "message": "A forecast needs a date column, and this file has none I can read."}
        metric = metric or (_money_first(columns)[0].name if _money_first(columns) else None)
        horizon = int(m.group(1)) if (m := re.search(r"(\d+)", t)) else 3
        return {"kind": "analysis", "queries": [{"q": "forecast", "date": dates[0], "metric": metric, "agg": "sum" if metric else "count",
                                                  "period": _period(t), "horizon": min(max(horizon, 1), 24)}]}

    if _has(t, r"trend|over time|per (day|week|month|quarter|year)|monthly|weekly|yearly|growth|اتجاه|تطور|خلال|شهري|سنوي|نمو"):
        if not dates:
            return {"kind": "unknown", "message": "A trend needs a date column, and this file has none I can read."}
        metric = metric or (_money_first(columns)[0].name if _money_first(columns) else None)
        return {"kind": "analysis", "queries": [{"q": "trend", "date": dates[0], "metric": metric, "agg": "sum" if metric else "count", "period": _period(t)}]}

    if _has(t, r"outlier|unusual|suspicious|anomal|strange|شاذ|غريب|مشبوه|غير طبيعي"):
        targets = numeric or [c.name for c in _by_type(columns, "integer", "decimal")]
        queries = [{"q": "outliers", "column": n} for n in targets[:3]]
        return {"kind": "analysis", "queries": queries, "findings": True,
                "message": "Unusual numbers below, followed by the anomalies the quality checks found."}

    if _has(t, r"relation|correlat|affect|impact|between|depend|علاقة|ارتباط|تأثير"):
        pair = cols[:2]
        if len(pair) < 2:
            return {"kind": "clarify", "question": "Which two columns should I compare? Select them, or name them in the request.", "options": []}
        return {"kind": "analysis", "queries": [{"q": "relation", "a": pair[0], "b": pair[1]}]}

    if _has(t, r"share|percent|proportion|distribution|breakdown|نسبة|توزيع|حصة"):
        by = categories[0] if categories else None
        if not by:
            return _pick_column_question("break down", _by_type(columns, "category", "boolean"),
                                         lambda n: {"query": {"q": "share", "by": n}})
        return {"kind": "analysis", "queries": [{"q": "share", "by": by, "metric": metric, "agg": "sum" if metric else "count"}]}

    top_words = r"top|best|highest|most|largest|biggest|أعلى|اعلى|أفضل|افضل|أكثر|اكثر|أكبر"
    low_words = r"worst|lowest|least|smallest|bottom|أقل|اقل|أسوأ|اسوأ|أضعف"
    if _has(t, top_words + "|" + low_words + r"|\bby\b|\bper\b|حسب|لكل"):
        by = categories[0] if categories else None
        if not by:
            return _pick_column_question("group by", _by_type(columns, "category", "boolean", "text"),
                                         lambda n: {"query": {"q": "group", "by": n, "metric": metric, "agg": "sum" if metric else "count"}})
        n = int(m.group(1)) if (m := re.search(r"\b(\d{1,2})\b", t)) else 10
        agg = "mean" if _has(t, r"average|mean|متوسط|معدل") else "count" if (not metric or _has(t, r"count|how many|عدد|كم")) else "sum"
        return {"kind": "analysis", "queries": [{"q": "group", "by": by, "metric": metric, "agg": agg, "top": n,
                                                  "order": "asc" if _has(t, low_words) else "desc"}]}

    if cols and _has(t, r"what|explain|describe|summar|tell me|meaning|وش|ايش|اشرح|وصف|ملخص|يعني"):
        return {"kind": "analysis", "queries": [{"q": "summary", "column": c} for c in cols[:3]], "explain": cols[:3]}
    if _has(t, r"overview|summar|what is in|describe the (file|data)|نظرة|ملخص"):
        return {"kind": "analysis", "queries": [{"q": "overview"}, {"q": "missing"}]}

    return {"kind": "unknown", "message": (
        "I did not understand that. Try for example: 'convert Active to yes/no', 'standardise City', "
        "'fill missing Credit', 'top 5 City by Credit', 'trend of sales per month', 'find unusual values', "
        "or turn on an AI model for free-form requests.")}


def _conversion(op: str, targets: list[str], table: Table, info: dict[str, ColumnInfo]) -> dict:
    specs = []
    for name in targets:
        spec = {"op": op, "column": name}
        if op == "to_boolean":
            keys = {match_key(v) for v, _ in info[name].top_values}
            unknown = keys - TRUE_TOKENS - FALSE_TOKENS
            if unknown:
                values = [v for v, _ in info[name].top_values]
                if len(values) == 2:
                    a, b = values
                    return {"kind": "clarify", "question": f"Which value in {name} means yes?", "options": [
                        {"label": f"{a} = yes, {b} = no", "op": {**spec, "true_values": [a], "false_values": [b]}},
                        {"label": f"{b} = yes, {a} = no", "op": {**spec, "true_values": [b], "false_values": [a]}}]}
                return {"kind": "clarify", "question": (
                    f"Some values in {name} are not yes/no words: {', '.join(sorted(unknown)[:8])}. "
                    "Convert the ones that are, and leave the others for review?"), "options": [
                    {"label": "Yes, convert the yes/no words only", "op": spec}]}
        if op == "parse_date" and info[name].date_ambiguous:
            return {"kind": "clarify", "question": f"In {name}, does the day or the month come first? Example: {info[name].examples[0]}", "options": [
                {"label": "Day first (dd/mm)", "op": {**spec, "order": "day_first"}},
                {"label": "Month first (mm/dd)", "op": {**spec, "order": "month_first"}}]}
        specs.append(spec)
    return {"kind": "proposal", "ops": specs}


def _useful_columns(text: str, columns: list[ColumnInfo]) -> str:
    dates = [c.name for c in columns if c.type == "date"]
    money = [c.name for c in columns if c.type in ("integer", "decimal") and c.meaning == "a money amount"]
    measures = [c.name for c in columns if c.type in ("integer", "decimal") and c.name not in money]
    cats = [c.name for c in columns if c.type in ("category", "boolean")]
    ids = [c.name for c in columns if c.type == "identifier"]
    parts = []
    if money:
        parts.append(f"Amounts to add up: {', '.join(money)}.")
    if measures:
        parts.append(f"Other numbers: {', '.join(measures)}.")
    if dates:
        parts.append(f"Dates for trends: {', '.join(dates)}.")
    if cats:
        parts.append(f"Groups to compare: {', '.join(cats)}.")
    if ids:
        parts.append(f"Identifiers for counting distinct records: {', '.join(ids)}.")
    if not (money or measures):
        parts.append("There is no numeric column, so analysis is limited to counts.")
    return " ".join(parts) + " (Based on the column types found; confirm what each one means.)"


# Model interpreter ---------------------------------------------------------

SYSTEM = """You turn a user's request about a table into JSON for a data tool.
You never compute numbers yourself and never invent column names or values.
Use only the operations and queries listed. If the request is ambiguous, ask one
focused question with up to four options. Answer with one JSON object only."""


def _vocabulary() -> str:
    return json.dumps({
        "operations": {
            "to_boolean": {"column": "name", "true_values": "[optional list]", "false_values": "[optional list]"},
            "parse_date": {"column": "name", "order": "day_first|month_first (only when needed)"},
            "parse_number": {"column": "name", "decimals": "optional int"},
            "normalize_text": {"column": "name", "case": "none|upper|lower|title"},
            "normalize_email": {"column": "name"},
            "normalize_phone": {"column": "name"},
            "map_values": {"column": "name", "mapping": {"old value": "new value"}},
            "set_missing": {"column": "name"},
            "fill_missing": {"column": "name", "strategy": "constant|mode|median|mean", "value": "for constant"},
            "rename_column": {"column": "name", "new_name": "text"},
            "drop_column": {"column": "name"},
            "split_column": {"column": "name", "separator": "text", "into": ["new", "names"]},
            "drop_rows": {"column": "name", "where": "missing|equals", "values": "for equals"},
            "deduplicate": {"columns": "optional list of key columns"},
        },
        "queries": {
            "overview": {}, "missing": {}, "summary": {"column": "name"},
            "group": {"by": "name", "metric": "numeric column or null", "agg": "count|sum|mean|median|min|max", "top": 10, "order": "desc|asc"},
            "share": {"by": "name", "metric": "optional"},
            "trend": {"date": "date column", "metric": "optional", "agg": "sum|count|mean", "period": "day|week|month|quarter|year"},
            "forecast": {"date": "date column", "metric": "optional", "period": "month", "horizon": 3},
            "relation": {"a": "name", "b": "name"},
            "outliers": {"column": "numeric column"},
            "any query may add": {"where": [{"column": "name", "op": "eq|in|not_blank|between", "value": "..."}]},
        },
        "answer formats": [
            {"kind": "proposal", "ops": ["operation objects with an 'op' key"], "message": "one sentence"},
            {"kind": "analysis", "queries": ["query objects with a 'q' key"], "message": "one sentence"},
            {"kind": "clarify", "question": "text", "options": [{"label": "text", "op": "operation or omit", "query": "query or omit"}]},
            {"kind": "answer", "message": "text about the columns only, no numbers you did not see"},
        ],
    }, ensure_ascii=False)


def model_prompt(text: str, selected: list[str], columns: list[ColumnInfo]) -> str:
    summary = [
        {"name": c.name, "type": c.type, "distinct": c.distinct, "missing": c.blank + c.null_tokens,
         "examples": c.examples[:5], "common": [v for v, _ in c.top_values[:6]]}
        for c in columns
    ]
    return json.dumps({"request": text, "selected_columns": selected, "columns": summary, "vocabulary": json.loads(_vocabulary())},
                      ensure_ascii=False)


def interpret_model(text: str, selected: list[str], table: Table, columns: list[ColumnInfo], provider) -> dict:
    from ..llm import LLMRequest

    request = LLMRequest(prompt=model_prompt(text, selected, columns), system=SYSTEM, model=provider.model,
                         prompt_version=PROMPT_VERSION)
    answer = provider.complete(request).json()
    if not isinstance(answer, dict) or answer.get("kind") not in ("proposal", "analysis", "clarify", "answer"):
        raise ValueError("the model's answer did not follow the format")
    return answer


def validate(result: dict, table: Table, columns: list[ColumnInfo]) -> dict:
    """Keep only operations and queries that the code accepts; report the rest."""
    problems = []
    good_ops = []
    for spec in result.get("ops") or []:
        try:
            ops.preview(table, ops.check(spec))
            good_ops.append(spec)
        except (ops.OpError, KeyError, TypeError, ValueError) as error:
            if isinstance(error, ops.OpError) and "day and month order" in str(error):
                name = spec.get("column")
                return {"kind": "clarify", "question": f"{error}.", "options": [
                    {"label": "Day first (dd/mm)", "op": {**spec, "order": "day_first"}},
                    {"label": "Month first (mm/dd)", "op": {**spec, "order": "month_first"}}], "column": name}
            problems.append(f"{spec.get('op', 'operation')}: {error}")
    good_queries = []
    for spec in result.get("queries") or []:
        try:
            analysis.run_query(table, columns, spec)
            good_queries.append(spec)
        except (analysis.QueryError, KeyError, TypeError, ValueError) as error:
            problems.append(f"{spec.get('q', 'query') if isinstance(spec, dict) else 'query'}: {error}")
    if "ops" in result:
        result["ops"] = good_ops
    if "queries" in result:
        result["queries"] = good_queries
    if problems:
        result["problems"] = problems
    if result.get("kind") in ("proposal", "analysis") and not good_ops and not good_queries:
        return {"kind": "unknown", "message": "I could not turn that into a valid step: " + "; ".join(problems)}
    return result


def interpret(text: str, selected: list[str], table: Table, columns: list[ColumnInfo], provider=None) -> dict:
    if not isinstance(text, str) or not text.strip():
        return {"kind": "unknown", "message": "Type a request first."}
    source = "rules"
    result = None
    if provider is not None:
        try:
            result = interpret_model(text, selected, table, columns, provider)
            source = provider.model
        except Exception as error:  # model down, bad JSON: fall back, and say so
            result = interpret_rules(text, selected, table, columns)
            result["note"] = f"The AI model could not answer ({error}); used the built-in rules instead."
    if result is None:
        result = interpret_rules(text, selected, table, columns)
    result = validate(result, table, columns)
    result["interpreted_by"] = source
    return result
