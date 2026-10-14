"""The session report and the dashboard, as standalone HTML pages.

Both are built from what the session holds: profile numbers (observed),
findings (checked by code), approved steps (with before/after counts),
pinned analyses (calculated) and forecasts (labelled as estimates). Each
section says which of these it is.
"""

from __future__ import annotations

from html import escape

from .charts import CHART_CSS

BASE_CSS = """
:root { --bg:#f7f7f5; --card:#fff; --text:#1d1d1b; --muted:#6b6b66; --line:#e3e3de; --accent:#2f5bea;
  --warn:#a15c00; --warn-bg:#fff4e0; --ok:#1d7a3e; --ok-bg:#e7f6ec; --bad:#b42318; --bad-bg:#fdecea; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --card:#1f1f1d; --text:#ececea; --muted:#a3a39d;
  --line:#33332f; --accent:#7c9bff; --warn:#ffb547; --warn-bg:#332611; --ok:#6fd193; --ok-bg:#12291a; --bad:#ff8a80; --bad-bg:#3a1714; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 24px; margin: 0 0 4px; } h2 { font-size: 18px; margin: 28px 0 8px; } h3 { font-size: 15px; margin: 0 0 6px; }
.sub, .muted { color: var(--muted); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 16px; margin: 12px 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
.tile b { display: block; font-size: 22px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 6px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
.tag { display: inline-block; font-size: 12px; padding: 1px 8px; border-radius: 99px; border: 1px solid var(--line); color: var(--muted); }
.tag.error { color: var(--bad); background: var(--bad-bg); border-color: transparent; }
.tag.anomaly { color: var(--warn); background: var(--warn-bg); border-color: transparent; }
.tag.estimate { color: var(--warn); background: var(--warn-bg); border-color: transparent; }
ul { margin: 6px 0; padding-left: 20px; }
form.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; }
form.filters label { display: flex; flex-direction: column; font-size: 13px; color: var(--muted); }
input, select, button { font: inherit; padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--card); color: var(--text); }
button { cursor: pointer; font-weight: 600; }
.wide { overflow-x: auto; }
""" + CHART_CSS


def page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title><style>{BASE_CSS}</style></head><body><main>{body}</main></body></html>")


def _table(columns: list, rows: list) -> str:
    head = "".join(f"<th>{escape(str(c))}</th>" for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(v))}</td>" for v in row) + "</tr>" for row in rows)
    return f'<div class="wide"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def result_card(result: dict) -> str:
    if result.get("error"):
        return f'<div class="card"><h3>{escape(str(result.get("title")))}</h3><p class="muted">Could not be computed: {escape(result["error"])}</p></div>'
    tag = '<span class="tag estimate">estimate</span> ' if result.get("estimate") else '<span class="tag">calculated</span> '
    facts = "".join(f"<li>{escape(f)}</li>" for f in result.get("facts", []))
    caveats = "".join(f"<li>{escape(c)}</li>" for c in result.get("caveats", []))
    table = result.get("table")
    return (f'<div class="card"><h3>{tag}{escape(result["title"])}</h3>{result.get("svg", "")}'
            f"<ul>{facts}</ul>" + (f'<ul class="muted">{caveats}</ul>' if caveats else "")
            + (_table(table["columns"], table["rows"][:15]) if table and table.get("rows") else "") + "</div>")


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
        '<div class="tiles">'
        f'<div class="tile"><b>{summary["rows"]:,}</b>rows now</div>'
        f'<div class="tile"><b>{summary["columns"]}</b>columns</div>'
        f'<div class="tile"><b>{overview["quality_score"]:.0%}</b>cells without errors or anomalies</div>'
        f'<div class="tile"><b>{overview["steps"]}</b>approved steps</div></div>',
        "<h2>What the file contains <span class='tag'>observed</span></h2>",
        f"<p>{summary['rows']:,} rows, {summary['columns']} columns, {summary['missing_cells']:,} missing cells, "
        f"{summary['exact_duplicate_rows']:,} exact duplicate rows."
        + (f" It looks like <b>{escape(summary['guess'])}</b> (a guess from the column names and types)." if summary.get("guess") else "") + "</p>",
        _table(["Column", "Type", "Evidence", "Missing", "Distinct", "Examples"],
               [[c["name"], c["label"], c["evidence"][0] if c["evidence"] else "", c["blank"] + c["null_tokens"], c["distinct"],
                 ", ".join(c["examples"][:3])] for c in overview["columns"]]),
        "<h2>Data quality <span class='tag'>checked by code</span></h2>",
        "<p class='muted'>Errors break what a column was found or confirmed to be; anomalies look wrong but may be right; "
        "recommendations are formats that work but are inconsistent.</p>",
    ]
    if findings:
        parts.append(_table(["Severity", "Finding", "Count", "Examples"],
                            [[f["severity"], f["title"], f["count"], ", ".join(map(str, f["examples"][:3]))] for f in findings]))
    else:
        parts.append("<p>No findings.</p>")
    applied = [h for h in history if h["event"] in ("applied", "undone", "rejected", "type_set")]
    parts.append("<h2>Changes <span class='tag'>approved steps</span></h2>")
    if applied:
        rows = []
        for h in applied:
            what = (h.get("op") or {}).get("op", h.get("type", ""))
            col = (h.get("op") or {}).get("column") or h.get("column") or ""
            v = h.get("validation") or {}
            effect = f"{h.get('cells', 0)} cells" + (f", {h['rows_removed']} rows removed" if h.get("rows_removed") else "")
            check = (f"errors {v['errors_before']}→{v['errors_after']}, anomalies {v['anomalies_before']}→{v['anomalies_after']}"
                     if v else "")
            rows.append([h["at"], h["event"], what, col, effect if h["event"] == "applied" else "", h.get("actor", ""), check])
        parts.append(_table(["When", "Event", "Operation", "Column", "Effect", "By", "Validation after"], rows))
        parts.append("<p class='muted'>The original upload is unchanged. The cleaned file is the original with these steps replayed in order "
                     "(download the plan to repeat them on another file).</p>")
    else:
        parts.append("<p>No changes yet.</p>")
    parts.append("<h2>Analysis <span class='tag'>calculated</span></h2>")
    if pinned:
        parts.append('<div class="grid">' + "".join(result_card(r) for r in pinned) + "</div>")
    else:
        parts.append("<p class='muted'>No analysis added. Use “Add to dashboard” on a result to include it here.</p>")
    return page(f"Report: {meta.get('filename', '')}", "".join(parts))


def build_dashboard(session, where: list[dict], token: str | None = None) -> str:
    data = session.dashboard(where)
    overview = session.overview()
    columns = {c["name"]: c for c in overview["columns"]}
    filter_column = where[0]["column"] if where else ""
    filter_value = where[0].get("value", "") if where else ""
    if isinstance(filter_value, list):
        filter_value = " to ".join(filter_value)
    options = "".join(f'<option value="{escape(n)}"{" selected" if n == filter_column else ""}>{escape(n)} ({escape(columns[n]["label"])})</option>'
                      for n in data["filterable"])
    parts = [
        f"<h1>Dashboard: {escape(overview['meta'].get('filename', ''))}</h1>",
        '<p class="sub">Every number is calculated from the current, cleaned data. Estimates are marked.</p>',
        '<form class="filters" method="get">'
        + (f'<input type="hidden" name="token" value="{escape(token)}">' if token else "") +
        f'<label>Filter column<select name="column"><option value="">(none)</option>{options}</select></label>'
        f'<label>Value, or for dates from,to<input name="value" value="{escape(str(filter_value))}" placeholder="Jeddah   or   2024-01-01,2024-12-31"></label>'
        '<button type="submit">Apply</button> <a href="?">Clear</a></form>',
        '<div class="tiles" style="margin-top:12px">'
        f'<div class="tile"><b>{data["rows"]:,}</b>rows</div>'
        f'<div class="tile"><b>{data["quality_score"]:.0%}</b>cells without errors or anomalies</div>'
        f'<div class="tile"><b>{len(data["items"])}</b>results</div></div>',
    ]
    if data["filters"]:
        parts.append(f"<p>Filtered: {escape(str(data['filters']))}</p>")
    if data["items"]:
        parts.append('<div class="grid">' + "".join(result_card(r) for r in data["items"]) + "</div>")
    else:
        parts.append('<div class="card">No results yet. Ask a question on the agent page and press “Add to dashboard”.</div>')
    return page(f"Dashboard: {overview['meta'].get('filename', '')}", "".join(parts))


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
