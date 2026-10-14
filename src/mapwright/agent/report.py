"""The session report and the dashboard, as standalone HTML pages.

Both are built from what the session holds: profile numbers (observed),
findings (checked by code), approved steps (with before/after counts and
the confidence shown when they were proposed), pinned analyses
(calculated) and forecasts (labelled as estimates). Each section says
which of these it is.
"""

from __future__ import annotations

from html import escape

from .charts import CHART_CSS, donut, line

BASE_CSS = """
:root { --bg:#f6f7f9; --card:#fff; --text:#16181d; --muted:#667085; --line:#e4e7ec; --soft:#f2f4f7; --accent:#3156e0;
  --warn:#b54708; --warn-bg:#fef0c7; --ok:#067647; --ok-bg:#dcfae6; --bad:#c01048; --bad-bg:#fee4e2; --gray:#98a2b3; }
@media (prefers-color-scheme: dark) { :root { --bg:#111317; --card:#1a1d23; --text:#eceef2; --muted:#98a2b3;
  --line:#2c313a; --soft:#22262d; --accent:#7f9cff; --warn:#fdb022; --warn-bg:#3b2a0c; --ok:#47cd89; --ok-bg:#0f2e1f;
  --bad:#fd6f8e; --bad-bg:#3d1320; --gray:#5d6675; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width: 1120px; margin: 0 auto; padding: 28px 16px 64px; }
h1 { font-size: 24px; margin: 0 0 2px; } h2 { font-size: 17px; margin: 0 0 12px; } h3 { font-size: 15px; margin: 0 0 6px; }
.section-title { font-size: 18px; margin: 28px 0 8px; }
.sub, .muted { color: var(--muted); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 18px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; margin: 14px 0; }
.grid > .card, .tiles > .tile { margin: 0; }
.hero { display: grid; grid-template-columns: auto 1fr; gap: 20px; align-items: center; }
@media (max-width: 640px) { .hero { grid-template-columns: 1fr; justify-items: start; } }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; }
.tile { background: var(--soft); border-radius: 10px; padding: 10px 12px; font-size: 13px; color: var(--muted); }
.tile b { display: block; font-size: 22px; color: var(--text); }
.tile.error b { color: var(--bad); } .tile.anomaly b { color: var(--warn); }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 7px 6px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 13px; }
.tag { display: inline-block; font-size: 12px; padding: 1px 9px; border-radius: 99px; background: var(--soft); color: var(--muted); white-space: nowrap; }
.tag.error, .tag.low-conf { color: var(--bad); background: var(--bad-bg); }
.tag.anomaly, .tag.estimate, .tag.medium-conf { color: var(--warn); background: var(--warn-bg); }
.tag.ok, .tag.high-conf { color: var(--ok); background: var(--ok-bg); }
ul { margin: 6px 0; padding-left: 20px; }
.health { display: grid; grid-template-columns: minmax(90px, 180px) 1fr 44px; gap: 6px 10px; align-items: center; font-size: 13px; }
.health .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar { display: flex; height: 10px; border-radius: 99px; overflow: hidden; background: var(--soft); }
.bar span { display: block; height: 100%; }
.seg-ok { background: var(--ok); } .seg-warn { background: var(--warn); } .seg-missing { background: var(--gray); }
.legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--muted); margin-top: 10px; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 5px; vertical-align: -1px; }
.issue { display: flex; gap: 10px; align-items: baseline; padding: 7px 0; border-bottom: 1px solid var(--line); font-size: 14px; }
.issue:last-child { border-bottom: 0; }
.issue .count { margin-left: auto; color: var(--muted); font-size: 13px; white-space: nowrap; }
.meter { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; }
.meter .bar { width: 70px; height: 8px; }
.conf-high { background: var(--ok); } .conf-medium { background: var(--warn); } .conf-low { background: var(--bad); }
form.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; margin: 14px 0 0; }
form.filters label { display: flex; flex-direction: column; font-size: 13px; color: var(--muted); }
input, select, button { font: inherit; padding: 6px 9px; border: 1px solid var(--line); border-radius: 8px; background: var(--card); color: var(--text); }
button { cursor: pointer; font-weight: 600; }
a { color: var(--accent); }
.wide { overflow-x: auto; }
""" + CHART_CSS


def page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title><style>{BASE_CSS}</style></head><body><main>{body}</main></body></html>")


def _table(columns: list, rows: list) -> str:
    head = "".join(f"<th>{escape(str(c))}</th>" for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(v))}</td>" for v in row) + "</tr>" for row in rows)
    return f'<div class="wide"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def meter(confidence: dict | None) -> str:
    """A small bar with the confidence percentage, coloured by level."""
    if not confidence:
        return '<span class="muted">—</span>'
    score, lvl = confidence["score"], confidence["level"]
    return (f'<span class="meter" title="{escape(confidence.get("summary", ""))}"><span class="bar">'
            f'<span class="conf-{lvl}" style="width:{score * 100:.0f}%"></span></span>{score:.0%} {lvl}</span>')


def result_card(result: dict) -> str:
    if result.get("error"):
        return f'<div class="card"><h3>{escape(str(result.get("title")))}</h3><p class="muted">Could not be computed: {escape(result["error"])}</p></div>'
    tag = '<span class="tag estimate">estimate</span> ' if result.get("estimate") else '<span class="tag">calculated</span> '
    facts = "".join(f"<li>{escape(f)}</li>" for f in result.get("facts", []))
    caveats = "".join(f"<li>{escape(c)}</li>" for c in result.get("caveats", []))
    table = result.get("table")
    conf = (f'<p>Confidence: {meter(result["confidence"])}</p>' if result.get("confidence") else "")
    return (f'<div class="card"><h3>{tag}{escape(result["title"])}</h3>{result.get("svg", "")}{conf}'
            f"<ul>{facts}</ul>" + (f'<ul class="muted">{caveats}</ul>' if caveats else "")
            + (_table(table["columns"], table["rows"][:15]) if table and table.get("rows") else "") + "</div>")


# Quality visuals ------------------------------------------------------------


def column_health(columns: list[dict]) -> list[dict]:
    """For each column: the share of rows that fit its type, do not fit, or are missing."""
    out = []
    for c in columns:
        rows = max(c["rows"], 1)
        missing = (c["blank"] + c["null_tokens"]) / rows
        filled = c["filled"] / rows
        fit = filled * (c["confidence"] if c["type"] != "empty" else 0)
        out.append({"name": c["name"], "label": c["label"], "fit": fit, "misfit": max(filled - fit, 0), "missing": missing,
                    "findings": c.get("findings", 0)})
    return out


def health_card(columns: list[dict]) -> str:
    rows = []
    for h in sorted(column_health(columns), key=lambda h: (h["fit"], -h["findings"])):
        rows.append(
            f'<div class="name" title="{escape(h["name"])} ({escape(h["label"])})">{escape(h["name"])}</div>'
            f'<div class="bar" title="{h["fit"]:.0%} fit the type, {h["misfit"]:.0%} do not, {h["missing"]:.0%} missing">'
            f'<span class="seg-ok" style="width:{h["fit"] * 100:.1f}%"></span>'
            f'<span class="seg-warn" style="width:{h["misfit"] * 100:.1f}%"></span>'
            f'<span class="seg-missing" style="width:{h["missing"] * 100:.1f}%"></span></div>'
            f'<div class="muted">{h["fit"]:.0%}</div>')
    return ('<div class="card"><h2>Column health</h2><p class="muted">Weakest first. How much of each column fits the type it was read as.</p>'
            f'<div class="health">{"".join(rows)}</div>'
            '<div class="legend"><span><i class="seg-ok"></i>fits the type</span><span><i class="seg-warn"></i>does not fit</span>'
            '<span><i class="seg-missing"></i>missing</span></div></div>')


def issues_card(findings: list[dict], limit: int = 8) -> str:
    if not findings:
        return '<div class="card"><h2>Top issues</h2><p>No issues found by the checks.</p></div>'
    items = "".join(
        f'<div class="issue"><span class="tag {escape(f["severity"])}">{escape(f["severity"])}</span>'
        f'<span>{escape(f["title"])}</span><span class="count">{f["count"]:,}</span></div>' for f in findings[:limit])
    more = f'<p class="muted">{len(findings) - limit} more in the report.</p>' if len(findings) > limit else ""
    return f'<div class="card"><h2>Top issues</h2>{items}{more}</div>'


def decisions(history: list[dict]) -> list[dict]:
    """Approved and rejected proposals, with the confidence shown when they were proposed."""
    proposed = {h["id"]: h for h in history if h["event"] == "proposed"}
    out = []
    for h in history:
        if h["event"] in ("applied", "rejected"):
            p = proposed.get(h.get("id"), {})
            score = p.get("confidence")
            out.append({**h, "confidence": None if score is None else
                        {"score": score, "level": "high" if score >= 0.85 else "medium" if score >= 0.6 else "low"},
                        "source": p.get("source", "")})
    return out


def progress_card(history: list[dict]) -> str:
    steps = [h for h in history if h["event"] == "applied" and h.get("validation")]
    if not steps:
        return ('<div class="card"><h2>Progress</h2><p class="muted">No approved changes yet. '
                'Each approved change is checked again; the open issues will be drawn here.</p></div>')
    first = steps[0]["validation"]
    labels = ["Start"] + [f"Step {h['step']}" for h in steps]
    errors = [first["errors_before"]] + [h["validation"]["errors_after"] for h in steps]
    anomalies = [first["anomalies_before"]] + [h["validation"]["anomalies_after"] for h in steps]
    chart = line(labels, [{"name": "errors", "values": errors, "tone": "bad"},
                                    {"name": "anomalies", "values": anomalies, "tone": "warn"}]) if len(labels) > 1 else ""
    return (f'<div class="card"><h2>Progress</h2><p class="muted">Errors and anomalies after each approved change.</p>{chart}'
            '<div class="legend"><span><i style="background:var(--bad)"></i>errors</span><span><i style="background:var(--warn)"></i>anomalies</span></div>'
            f'<p>Errors {errors[0]} → {errors[-1]}, anomalies {anomalies[0]} → {anomalies[-1]}.</p></div>')


def decisions_card(history: list[dict]) -> str:
    rows = decisions(history)
    if not rows:
        return '<div class="card"><h2>Decisions</h2><p class="muted">No decisions yet.</p></div>'
    body = "".join(
        f'<tr><td>{"approved" if d["event"] == "applied" else "rejected"}</td>'
        f'<td>{escape((d.get("op") or {}).get("op", "").replace("_", " "))} '
        f'<span class="muted">{escape(str((d.get("op") or {}).get("column") or ", ".join((d.get("op") or {}).get("columns") or [])))}</span></td>'
        f'<td>{meter(d["confidence"])}</td><td>{escape(d.get("actor", ""))}</td></tr>' for d in rows[-12:])
    return ('<div class="card"><h2>Decisions</h2><p class="muted">How sure the agent was when it proposed each change, and who decided.</p>'
            f'<div class="wide"><table><thead><tr><th>Decision</th><th>Change</th><th>Confidence</th><th>By</th></tr></thead>'
            f'<tbody>{body}</tbody></table></div></div>')


def hero(overview: dict, extra_tiles: str = "") -> str:
    s, counts = overview["summary"], overview["finding_counts"]
    return ('<div class="card hero">'
            f'<div>{donut(overview["quality_score"], "quality")}<div class="muted" style="text-align:center;font-size:13px">quality</div></div>'
            '<div><p style="margin:0 0 10px">Share of cells without errors or anomalies. '
            + (f'It looks like <b>{escape(s["guess"])}</b> (a guess from the columns).' if s.get("guess") else "") + "</p>"
            '<div class="tiles">'
            f'<div class="tile"><b>{s["rows"]:,}</b>rows</div>'
            f'<div class="tile"><b>{s["columns"]}</b>columns</div>'
            f'<div class="tile error"><b>{counts["error"]}</b>errors</div>'
            f'<div class="tile anomaly"><b>{counts["anomaly"]}</b>anomalies</div>'
            f'<div class="tile"><b>{counts["recommendation"]}</b>tips</div>'
            f'<div class="tile"><b>{overview["steps"]}</b>approved changes</div>{extra_tiles}</div></div></div>')


# Pages ----------------------------------------------------------------------


def build_report(session) -> str:
    overview = session.overview()
    meta, summary = overview["meta"], overview["summary"]
    findings = session.findings()
    history = session.history()
    pinned = session.dashboard()["items"]
    parts = [
        f"<h1>Data report: {escape(meta.get('filename', ''))}</h1>",
        f'<p class="sub">Session {escape(session.id)} · uploaded {escape(meta.get("created", ""))}'
        + (f" · sheet {escape(meta['read'].get('sheet'))}" if meta.get("read", {}).get("sheet") else "") + "</p>",
        hero(overview),
        "<h2 class='section-title'>What the file contains <span class='tag'>observed</span></h2>",
        f"<p>{summary['rows']:,} rows, {summary['columns']} columns, {summary['missing_cells']:,} missing cells, "
        f"{summary['exact_duplicate_rows']:,} exact duplicate rows.</p>",
        '<div class="card">' + _table(["Column", "Type", "Sure of type", "Evidence", "Missing", "Distinct", "Examples"],
               [[c["name"], c["label"], f"{c['confidence']:.0%}", c["evidence"][0] if c["evidence"] else "", c["blank"] + c["null_tokens"],
                 c["distinct"], ", ".join(c["examples"][:3])] for c in overview["columns"]]) + "</div>",
        "<h2 class='section-title'>Data quality <span class='tag'>checked by code</span></h2>",
        "<p class='muted'>Errors break what a column was found or confirmed to be; anomalies look wrong but may be right; "
        "recommendations are formats that work but are inconsistent.</p>",
        f'<div class="grid">{health_card(overview["columns"])}{issues_card(findings)}</div>',
    ]
    if findings:
        parts.append('<div class="card">' + _table(["Severity", "Finding", "Count", "Examples"],
                     [[f["severity"], f["title"], f["count"], ", ".join(map(str, f["examples"][:3]))] for f in findings]) + "</div>")
    applied = [h for h in history if h["event"] in ("applied", "undone", "rejected", "type_set")]
    parts.append("<h2 class='section-title'>Changes <span class='tag'>approved steps</span></h2>")
    if applied:
        parts.append(f'<div class="grid">{progress_card(history)}{decisions_card(history)}</div>')
        rows = []
        for h in applied:
            what = (h.get("op") or {}).get("op", h.get("type", ""))
            col = (h.get("op") or {}).get("column") or h.get("column") or ""
            v = h.get("validation") or {}
            effect = f"{h.get('cells', 0)} cells" + (f", {h['rows_removed']} rows removed" if h.get("rows_removed") else "")
            check = (f"errors {v['errors_before']}→{v['errors_after']}, anomalies {v['anomalies_before']}→{v['anomalies_after']}"
                     if v else "")
            rows.append([h["at"], h["event"], what, col, effect if h["event"] == "applied" else "", h.get("actor", ""), check])
        parts.append('<div class="card">' + _table(["When", "Event", "Operation", "Column", "Effect", "By", "Validation after"], rows) + "</div>")
        parts.append("<p class='muted'>The original upload is unchanged. The cleaned file is the original with these steps replayed in order "
                     "(download the plan to repeat them on another file).</p>")
    else:
        parts.append("<p>No changes yet.</p>")
    parts.append("<h2 class='section-title'>Analysis <span class='tag'>calculated</span></h2>")
    if pinned:
        parts.append('<div class="grid">' + "".join(result_card(r) for r in pinned) + "</div>")
    else:
        parts.append("<p class='muted'>No analysis added. Use “Add to dashboard” on a result to include it here.</p>")
    return page(f"Report: {meta.get('filename', '')}", "".join(parts))


def build_dashboard(session, where: list[dict], token: str | None = None) -> str:
    data = session.dashboard(where)
    overview = session.overview()
    history = session.history()
    findings = session.findings()
    columns = {c["name"]: c for c in overview["columns"]}
    filter_column = where[0]["column"] if where else ""
    filter_value = where[0].get("value", "") if where else ""
    if isinstance(filter_value, list):
        filter_value = " to ".join(v for v in filter_value if v)
    options = "".join(f'<option value="{escape(n)}"{" selected" if n == filter_column else ""}>{escape(n)} ({escape(columns[n]["label"])})</option>'
                      for n in data["filterable"])
    parts = [
        f"<h1>Dashboard: {escape(overview['meta'].get('filename', ''))}</h1>",
        '<p class="sub">Every number is calculated from the current, cleaned data. Estimates are marked.</p>',
        '<div class="grid" style="grid-template-columns:1fr">' + hero(overview) + "</div>",
        f'<div class="grid">{health_card(overview["columns"])}{issues_card(findings)}</div>',
        f'<div class="grid">{progress_card(history)}{decisions_card(history)}</div>',
        "<h2 class='section-title'>Your analyses</h2>",
        '<form class="filters" method="get">'
        + (f'<input type="hidden" name="token" value="{escape(token)}">' if token else "") +
        f'<label>Filter column<select name="column"><option value="">(none)</option>{options}</select></label>'
        f'<label>Value, or for dates from,to<input name="value" value="{escape(str(filter_value))}" placeholder="Jeddah   or   2024-01-01,2024-12-31"></label>'
        '<button type="submit">Apply</button> <a href="?' + (f"token={escape(token)}" if token else "") + '">Clear</a></form>',
    ]
    if data["filters"]:
        parts.append(f"<p>Filtered: {escape(_describe_filters(data['filters']))} · {data['rows']:,} rows in the file.</p>")
    if data["items"]:
        parts.append('<div class="grid">' + "".join(result_card(r) for r in data["items"]) + "</div>")
    else:
        parts.append('<div class="card">No analyses yet. Ask a question on the agent page and press “Add to dashboard”.</div>')
    return page(f"Dashboard: {overview['meta'].get('filename', '')}", "".join(parts))


def _describe_filters(filters: list[dict]) -> str:
    out = []
    for f in filters:
        value = f.get("value")
        if isinstance(value, list):
            value = " to ".join(str(v) for v in value if v) or "any"
        out.append(f"{f['column']} {f['op'].replace('_', ' ')} {value if value is not None else ''}".strip())
    return "; ".join(out)


def where_from_query(columns: dict, column: str | None, value: str | None) -> list[dict]:
    if not column or column not in columns:
        return []
    value = (value or "").strip()
    if columns[column]["type"] == "date":
        bits = [b.strip() for b in value.split(",")] if value else []
        return [{"column": column, "op": "between", "value": [bits[0] if bits else None, bits[1] if len(bits) > 1 else None]}]
    if not value:
        return [{"column": column, "op": "not_blank"}]
    return [{"column": column, "op": "in", "value": [v.strip() for v in value.split(",") if v.strip()]}]
