"""Research report rendering (self-contained HTML and Markdown).

Reports are built only from persisted experiment artifacts: the analysis document, the
equity series, the integrity report and (optionally) the validation results. Nothing is
recomputed here, so a report always matches what the platform displayed.
"""

from __future__ import annotations

import html
import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

UNDEF = "n/a"


def _pct(v: Any, d: int = 1) -> str:
    return (
        UNDEF
        if v is None or (isinstance(v, float) and not math.isfinite(v))
        else f"{v * 100:.{d}f}%"
    )


def _num(v: Any, d: int = 2) -> str:
    return UNDEF if v is None or (isinstance(v, float) and not math.isfinite(v)) else f"{v:,.{d}f}"


def _money(v: Any) -> str:
    return UNDEF if v is None else f"${v:,.0f}"


def _svg_line(
    dates: list[str],
    series: list[tuple[str, list[float | None], str, bool]],
    title: str,
    fmt: Any,
    height: int = 220,
    width: int = 760,
) -> str:
    """Minimal inline SVG line chart (no scripts) with axis labels and a legend."""
    pad_l, pad_r, pad_t, pad_b = 64, 12, 24, 28
    vals = [v for _, ys, _, _ in series for v in ys if v is not None]
    if not vals or len(dates) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi = lo + 1
    w, h = width - pad_l - pad_r, height - pad_t - pad_b

    def x(i: int) -> float:
        return pad_l + w * i / (len(dates) - 1)

    def y(v: float) -> float:
        return pad_t + h * (1 - (v - lo) / (hi - lo))

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}" '
        f'xmlns="http://www.w3.org/2000/svg" font-family="sans-serif" font-size="11">'
    ]
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        parts.append(
            f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y(v):.1f}" y2="{y(v):.1f}" '
            'stroke="#e4e8ed"/>'
        )
        parts.append(
            f'<text x="{pad_l - 6}" y="{y(v) + 4:.1f}" text-anchor="end" '
            f'fill="#5a6675">{html.escape(fmt(v))}</text>'
        )
    for k in range(0, len(dates), max(1, len(dates) // 6)):
        parts.append(
            f'<text x="{x(k):.1f}" y="{height - 8}" text-anchor="middle" '
            f'fill="#5a6675">{dates[k][:7]}</text>'
        )
    for li, (name, ys, color, dashed) in enumerate(series):
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(ys) if v is not None)
        dash = ' stroke-dasharray="4 3"' if dashed else ""
        parts.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="1.6"{dash} points="{pts}"/>'
        )
        lx = pad_l + li * 220
        parts.append(
            f'<line x1="{lx}" x2="{lx + 16}" y1="10" y2="10" stroke="{color}" '
            f'stroke-width="2"{dash}/><text x="{lx + 22}" y="14" fill="#17202b">'
            f"{html.escape(name)}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _rows(analysis: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    Fmt = Callable[[Any], str]
    p = analysis["performance"]["values"]
    g = analysis["gross_performance"]["values"]
    b = (analysis.get("benchmark_performance") or {}).get("values", {})
    spec: list[tuple[str, str, Fmt]] = [
        ("Total return", "total_return", _pct),
        ("CAGR", "cagr", lambda v: _pct(v, 2)),
        ("Volatility", "volatility", _pct),
        ("Sharpe ratio", "sharpe", _num),
        ("Sortino ratio", "sortino", _num),
        ("Max drawdown", "max_drawdown", _pct),
        ("Calmar ratio", "calmar", _num),
    ]
    return [(label, f(p.get(k)), f(g.get(k)), f(b.get(k))) for label, k, f in spec]


def report_context(
    experiment: dict[str, Any],
    dataset: dict[str, Any],
    analysis: dict[str, Any],
    equity: dict[str, Any],
    integrity: dict[str, Any],
    validation: dict[str, Any] | None,
) -> dict[str, Any]:
    perf = analysis["performance"]["values"]
    bench = analysis["meta"].get("benchmark")
    wf = (validation or {}).get("walk_forward") or {}
    conclusions = []
    excess = perf.get("excess_total_return")
    if excess is not None and bench:
        conclusions.append(
            f"Over the sample the strategy {'outperformed' if excess > 0 else 'underperformed'} "
            f"{bench} by {_pct(abs(excess))} in total return, net of modelled costs."
        )
    if wf and not wf.get("error"):
        conclusions.append(
            f"Walk-forward mean Sharpe was {_num(wf.get('mean_in_sample_sharpe'))} in-sample and "
            f"{_num(wf.get('mean_oos_sharpe'))} out-of-sample across {len(wf['folds'])} folds."
        )
    boot = (validation or {}).get("bootstrap") or {}
    if boot.get("sharpe"):
        s = boot["sharpe"]
        conclusions.append(
            f"The 95% bootstrap interval for the Sharpe ratio is {_num(s['lower'])} to "
            f"{_num(s['upper'])}"
            + (
                "; it includes zero, so the result is not statistically distinguishable from "
                "no skill."
                if s["lower"] <= 0
                else "."
            )
        )
    warnings = [c for c in integrity["checks"] if c["status"] in ("warning", "failed")]
    if warnings:
        conclusions.append(
            "Open integrity concerns: " + "; ".join(c["title"].lower() for c in warnings) + "."
        )
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "experiment": experiment,
        "dataset": dataset,
        "analysis": analysis,
        "equity": equity,
        "integrity": integrity,
        "validation": validation,
        "conclusions": conclusions,
    }


def render_markdown(ctx: dict[str, Any]) -> str:
    e, d, a, integ = ctx["experiment"], ctx["dataset"], ctx["analysis"], ctx["integrity"]
    cfg = e["config"]
    lines = [
        f"# {e['name']}",
        "",
        f"Generated {ctx['generated_at']} by QuantLab from persisted experiment results.",
        "",
    ]
    if d.get("is_synthetic"):
        lines += [
            "> **Synthetic data.** These results describe a simulated market, not real "
            "observations.",
            "",
        ]
    lines += [
        "## Overview",
        "",
        f"* Strategy: `{e['strategy_key']}` with parameters `{e['strategy_params']}`",
        f"* Sample: {a['meta']['first_session']} to {a['meta']['last_session']} "
        f"({a['meta']['sessions']} sessions), {len(a['meta']['universe'])} symbols",
        f"* Benchmark: {a['meta'].get('benchmark') or 'none'}",
        f"* Execution: {cfg.get('execution')} fills, {cfg.get('rebalance')} rebalance, "
        f"{cfg.get('commission_bps')} bps commission, {cfg.get('slippage_bps')} bps slippage",
        f"* Dataset version: `{d['version_hash']}` ({d['provenance'].get('provider')})",
        f"* Engine {e['engine_version']}, code {e['code_version']}, inputs `{e['input_hash']}`, "
        f"output fingerprint `{e.get('fingerprint')}`",
        "",
        "## Performance",
        "",
        "| Measure | Strategy | Before costs | Benchmark |",
        "|---|---|---|---|",
    ]
    lines += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} |" for r in _rows(a)]
    risk = a["risk"]
    lines += [
        "",
        "## Risk",
        "",
        f"* One-day historical VaR 95%: {_pct(risk.get('historical_var'), 2)}; expected "
        f"shortfall {_pct(risk.get('historical_es'), 2)}",
        f"* Annual turnover {_num(a['costs']['values'].get('annual_turnover'))}x; "
        f"total costs {_money(a['costs']['values'].get('total_costs'))}",
        "",
        "| Drawdown peak | Depth | Sessions | Recovered |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {x['peak'][:10]} | {_pct(x['depth'])} | {x['total_sessions']} | "
        f"{(x['recovery'] or 'not yet')[:10]} |"
        for x in a["drawdowns"]
    ]
    lines += ["", "## Research integrity", "", "| Check | Status | Finding |", "|---|---|---|"]
    lines += [f"| {c['title']} | {c['status']} | {c['finding']} |" for c in integ["checks"]]
    if ctx["conclusions"]:
        lines += ["", "## Conclusions", ""] + [f"* {c}" for c in ctx["conclusions"]]
    lines += [
        "",
        "## Limitations",
        "",
        integ["disclaimer"],
        "See docs/METHODOLOGY.md for definitions and known modelling limitations.",
        "",
        "## Reproduce",
        "",
        "```",
        f"POST /api/v1/experiments/{e['id']}/reproduce",
        "```",
        "",
    ]
    return "\n".join(lines)


CSS = """
body{font:15px/1.55 Georgia,serif;color:#17202b;background:#fff;max-width:880px;margin:2rem auto;
padding:0 1rem}h1,h2{font-weight:600;line-height:1.25}h2{margin-top:2.2rem;border-bottom:1px solid
#d3d9e0;padding-bottom:.3rem}table{border-collapse:collapse;width:100%;font:13px/1.4 sans-serif;
font-variant-numeric:tabular-nums}th,td{border-bottom:1px solid #e4e8ed;padding:.35rem .5rem;
text-align:left}td.r,th.r{text-align:right}.note{background:#f7ecd2;border:1px solid #e5d3a4;
padding:.6rem .8rem;border-radius:4px;font-family:sans-serif;font-size:13px}.meta{font:13px/1.6
sans-serif;color:#5a6675}.s-verified{color:#1f7a5a}.s-warning{color:#9a6a00}.s-failed{color:#b4372f}
.s-unassessed{color:#5a6675}code{font-size:12px}svg{width:100%;height:auto;margin:.5rem 0}
@media print{body{margin:0}}
"""


def render_html(ctx: dict[str, Any]) -> str:
    e, d, a, integ, eq = (
        ctx["experiment"],
        ctx["dataset"],
        ctx["analysis"],
        ctx["integrity"],
        ctx["equity"],
    )
    esc = html.escape
    cfg = e["config"]
    series = [("Strategy (net)", eq.get("equity") or [], "#2747c8", False)]
    if eq.get("benchmark"):
        series.append(
            (f"{a['meta'].get('benchmark')} (total return)", eq["benchmark"], "#8a94a3", True)
        )
    chart = _svg_line(eq["dates"], series, "Portfolio value", lambda v: f"{v / 1e6:.2f}M")
    dd_chart = _svg_line(
        eq["dates"],
        [("Drawdown", eq.get("drawdown") or [], "#b4372f", False)],
        "Drawdown",
        lambda v: f"{v * 100:.0f}%",
        height=150,
    )
    perf_rows = "".join(
        f"<tr><td>{esc(r[0])}</td><td class=r>{r[1]}</td><td class=r>{r[2]}</td>"
        f"<td class=r>{r[3]}</td></tr>"
        for r in _rows(a)
    )
    checks = "".join(
        f"<tr><td>{esc(c['title'])}</td><td class='s-{c['status']}'>{esc(c['status'])}</td>"
        f"<td>{esc(c['finding'])}</td></tr>"
        for c in integ["checks"]
    )
    dds = "".join(
        f"<tr><td>{esc(x['peak'][:10])}</td><td class=r>{_pct(x['depth'])}</td>"
        f"<td class=r>{x['total_sessions']}</td><td>{esc((x['recovery'] or 'not yet')[:10])}"
        "</td></tr>"
        for x in a["drawdowns"]
    )
    val = ctx.get("validation") or {}
    wf = val.get("walk_forward") or {}
    wf_html = ""
    if wf and not wf.get("error"):
        rows = "".join(
            f"<tr><td>{f['fold']}</td><td>{esc(f['test'][0])} to {esc(f['test'][1])}</td>"
            f"<td>{esc(', '.join(f'{k}={v}' for k, v in f['selected_params'].items()))}</td>"
            f"<td class=r>{_num(f['in_sample_sharpe'])}</td><td class=r>{_num(f['oos_sharpe'])}"
            f"</td><td class=r>{_pct(f['oos_return'])}</td></tr>"
            for f in wf["folds"]
        )
        wf_html = (
            "<h2>Out-of-sample validation</h2><table><tr><th>Fold</th><th>Test window"
            "</th><th>Parameters</th><th class=r>Sharpe in</th><th class=r>Sharpe out"
            f"</th><th class=r>Return out</th></tr>{rows}</table>"
        )
    concl = "".join(f"<li>{esc(c)}</li>" for c in ctx["conclusions"])
    synthetic = (
        "<p class=note><strong>Synthetic data.</strong> These results describe a "
        "simulated market, not real observations.</p>"
        if d.get("is_synthetic")
        else ""
    )
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{esc(e["name"])} · QuantLab research report</title><style>{CSS}</style></head><body>
<h1>{esc(e["name"])}</h1>
<p class=meta>Generated {esc(ctx["generated_at"])} from persisted QuantLab results ·
experiment <code>{esc(e["id"])}</code></p>{synthetic}
<h2>Overview</h2><table>
<tr><td>Strategy</td><td><code>{esc(e["strategy_key"])}</code>
{esc(str(e["strategy_params"]))}</td></tr>
<tr><td>Sample</td><td>{esc(a["meta"]["first_session"])} to {esc(a["meta"]["last_session"])}
({a["meta"]["sessions"]} sessions), {len(a["meta"]["universe"])} symbols</td></tr>
<tr><td>Benchmark</td><td>{esc(str(a["meta"].get("benchmark") or "none"))}</td></tr>
<tr><td>Execution</td><td>{esc(str(cfg.get("execution")))} fills, {esc(str(cfg.get("rebalance")))}
rebalance, {cfg.get("commission_bps")} bps commission,
{cfg.get("slippage_bps")} bps slippage</td></tr>
<tr><td>Dataset</td><td><code>{esc(d["version_hash"])}</code>
({esc(str(d["provenance"].get("provider")))})</td></tr>
<tr><td>Reproducibility</td><td>engine {esc(e["engine_version"])}, code {esc(e["code_version"])},
inputs <code>{esc(e["input_hash"][:16])}</code>, output <code>{esc(str(e.get("fingerprint"))[:16])}
</code></td></tr></table>
<h2>Performance</h2>{chart}{dd_chart}
<table><tr><th>Measure</th><th class=r>Strategy</th><th class=r>Before costs</th>
<th class=r>Benchmark</th></tr>{perf_rows}</table>
<h2>Risk</h2><p>One-day historical VaR (95%) {_pct(a["risk"].get("historical_var"), 2)},
expected shortfall {_pct(a["risk"].get("historical_es"), 2)}. Annual turnover
{_num(a["costs"]["values"].get("annual_turnover"))}x; total trading costs
{_money(a["costs"]["values"].get("total_costs"))}.</p>
<table><tr><th>Drawdown peak</th><th class=r>Depth</th><th class=r>Sessions</th>
<th>Recovered</th></tr>{dds}</table>
{wf_html}
<h2>Research integrity</h2><table><tr><th>Check</th><th>Status</th><th>Finding</th></tr>
{checks}</table>
<h2>Conclusions</h2><ul>{concl}</ul>
<h2>Limitations</h2><p>{esc(integ["disclaimer"])} Definitions and modelling limitations are
documented in the QuantLab methodology.</p></body></html>"""
