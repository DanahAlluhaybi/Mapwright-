"""Small SVG charts drawn from computed results.

Only two forms: horizontal bars for comparing groups and a line for values
over time (with the estimate part dashed). Colours come from CSS variables
on the page, so the charts follow light and dark mode. Every label is
escaped.
"""

from __future__ import annotations

from html import escape

from .analysis import fmt

WIDTH = 640


def _clip(text: str, length: int = 22) -> str:
    return text if len(text) <= length else text[: length - 1] + "…"


def bar(labels: list[str], values: list[float | None], name: str = "") -> str:
    pairs = [(str(l), v) for l, v in zip(labels, values) if v is not None]
    if not pairs:
        return ""
    row, label_w, value_w = 26, 170, 90
    height = row * len(pairs) + 10
    top = max(max(v for _, v in pairs), 0)
    low = min(min(v for _, v in pairs), 0)
    span = (top - low) or 1
    plot_w = WIDTH - label_w - value_w
    zero_x = label_w + (0 - low) / span * plot_w
    parts = [f'<svg class="chart" viewBox="0 0 {WIDTH} {height}" role="img" aria-label="{escape(name)}" xmlns="http://www.w3.org/2000/svg">']
    for i, (label, v) in enumerate(pairs):
        y = 5 + i * row
        x1 = label_w + (min(v, 0) - low) / span * plot_w
        w = abs(v) / span * plot_w
        parts.append(f'<text x="{label_w - 8}" y="{y + 16}" text-anchor="end" class="chart-label"><title>{escape(label)}</title>{escape(_clip(label))}</text>')
        parts.append(f'<rect x="{x1:.1f}" y="{y + 4}" width="{max(w, 1):.1f}" height="{row - 10}" rx="3" class="chart-bar"><title>{escape(label)}: {fmt(v)}</title></rect>')
        parts.append(f'<text x="{x1 + max(w, 1) + 6:.1f}" y="{y + 16}" class="chart-value">{fmt(v)}</text>')
    if low < 0:
        parts.append(f'<line x1="{zero_x:.1f}" y1="0" x2="{zero_x:.1f}" y2="{height}" class="chart-axis"/>')
    parts.append("</svg>")
    return "".join(parts)


def line(labels: list[str], series: list[dict], forecast_from: int | None = None) -> str:
    values = [v for s in series for v in s["values"] if v is not None]
    if not values or len(labels) < 2:
        return ""
    height, left, right, top_pad, bottom = 240, 70, 20, 14, 40
    plot_w, plot_h = WIDTH - left - right, height - top_pad - bottom
    top, low = max(values), min(min(values), 0)
    span = (top - low) or 1

    def xy(i: int, v: float) -> tuple[float, float]:
        return left + i / (len(labels) - 1) * plot_w, top_pad + (1 - (v - low) / span) * plot_h

    parts = [f'<svg class="chart" viewBox="0 0 {WIDTH} {height}" role="img" xmlns="http://www.w3.org/2000/svg">']
    for frac in (0, 0.5, 1):
        v = low + frac * span
        _, y = xy(0, v)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{WIDTH - right}" y2="{y:.1f}" class="chart-grid"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end" class="chart-value">{fmt(v)}</text>')
    if forecast_from:
        x, _ = xy(forecast_from - 1, low)
        parts.append(f'<rect x="{x:.1f}" y="{top_pad}" width="{WIDTH - right - x:.1f}" height="{plot_h}" class="chart-estimate-zone"><title>estimate</title></rect>')
    for k, s in enumerate(series):
        points = [xy(i, v) for i, v in enumerate(s["values"]) if v is not None]
        if len(points) < 2:
            continue
        path = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        css = "chart-line estimate" if s.get("name") == "estimate" else "chart-line"
        parts.append(f'<polyline points="{path}" class="{css}"><title>{escape(str(s.get("name", "")))}</title></polyline>')
        if len(points) <= 60:
            for (x, y), (i, v) in zip(points, [(i, v) for i, v in enumerate(s["values"]) if v is not None]):
                parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" class="{css}-dot"><title>{escape(labels[i])}: {fmt(v)}</title></circle>')
    step = max(1, len(labels) // 8)
    for i in range(0, len(labels), step):
        x, _ = xy(i, low)
        parts.append(f'<text x="{x:.1f}" y="{height - 14}" text-anchor="middle" class="chart-label">{escape(_clip(labels[i], 10))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg(chart: dict) -> str:
    kind = chart.get("type")
    if kind == "bar" and chart.get("series"):
        return bar(chart.get("labels", []), chart["series"][0]["values"], chart["series"][0].get("name", ""))
    if kind == "line" and chart.get("series"):
        return line(chart.get("labels", []), chart["series"], chart.get("forecast_from"))
    return ""


CHART_CSS = """
.chart { width: 100%; height: auto; max-width: 720px; display: block; margin: 8px 0; }
.chart-label { font-size: 12px; fill: var(--muted); }
.chart-value { font-size: 11px; fill: var(--muted); }
.chart-bar { fill: var(--accent); }
.chart-grid, .chart-axis { stroke: var(--line); stroke-width: 1; }
.chart-line { fill: none; stroke: var(--accent); stroke-width: 2; }
.chart-line.estimate { stroke: var(--warn); stroke-dasharray: 6 4; }
.chart-line-dot { fill: var(--accent); }
.chart-line.estimate-dot { fill: var(--warn); }
.chart-estimate-zone { fill: var(--warn-bg); opacity: .6; }
"""
