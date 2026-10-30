"""Evidence facts, a deterministic grounded answer engine, and a numeric-claim verifier.

The assistant never computes financial statistics. It can only restate *facts* produced by
the analytics engine, each with a stable id that answers cite inline as ``[F7]``. Every
number in an answer must match a fact (within display rounding) or the answer is rejected.
"""

from __future__ import annotations

import contextlib
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Unit = Literal["pct", "ratio", "usd", "count", "text", "date"]

ADVICE_DECLINE = (
    "I can't tell you whether to buy, sell or allocate money. QuantLab reports what this "
    "experiment did under its stated assumptions; it is not investment advice, and past "
    "simulated performance does not predict future results."
)


@dataclass(frozen=True)
class Fact:
    id: str
    key: str
    label: str
    value: float | str | None
    unit: Unit
    source: str
    topics: tuple[str, ...] = ()

    def display(self) -> str:
        v = self.value
        if v is None:
            return "undefined"
        if isinstance(v, str):
            return v
        if self.unit == "pct":
            return f"{v * 100:.2f}%"
        if self.unit == "usd":
            return f"${v:,.0f}"
        if self.unit == "count":
            return f"{v:,.0f}"
        return f"{v:.2f}"


@dataclass
class Answer:
    text: str
    citations: list[str]
    provider: str
    intents: list[str]
    declined: bool = False
    notes: list[str] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _g(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def build_facts(
    analysis: dict[str, Any],
    integrity: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    benchmark: str | None = None,
) -> list[Fact]:
    facts: list[Fact] = []

    def add(key: str, label: str, value: Any, unit: Unit, source: str, *topics: str) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            value = None
        facts.append(Fact(f"F{len(facts) + 1}", key, label, value, unit, source, topics))

    perf = _g(analysis, "performance", "values") or {}
    bname = benchmark or _g(analysis, "meta", "benchmark") or "the benchmark"
    meta = analysis.get("meta", {})
    add(
        "sample",
        "Sample period",
        f"{meta.get('first_session')} to {meta.get('last_session')}",
        "text",
        "analysis.meta",
        "summary",
    )
    add("sessions", "Trading sessions", meta.get("sessions"), "count", "analysis.meta", "summary")
    spec: list[tuple[str, str, Unit, tuple[str, ...]]] = [
        ("total_return", "Total return (net of costs)", "pct", ("performance", "summary")),
        ("cagr", "CAGR (net)", "pct", ("performance", "summary")),
        ("volatility", "Annualized volatility", "pct", ("risk", "summary")),
        ("sharpe", "Sharpe ratio", "ratio", ("performance", "summary", "risk")),
        ("sortino", "Sortino ratio", "ratio", ("performance", "risk")),
        ("max_drawdown", "Maximum drawdown", "pct", ("drawdown", "risk", "summary")),
        ("calmar", "Calmar ratio", "ratio", ("drawdown",)),
        ("worst_day", "Worst single-day return", "pct", ("drawdown", "risk")),
        ("beta", f"Beta to {bname}", "ratio", ("benchmark", "risk")),
        ("alpha", "Annualized Jensen alpha", "pct", ("benchmark",)),
        ("information_ratio", "Information ratio", "ratio", ("benchmark",)),
        ("tracking_error", "Tracking error", "pct", ("benchmark",)),
        ("benchmark_total_return", f"{bname} total return", "pct", ("benchmark", "summary")),
        ("excess_total_return", f"Total return minus {bname}", "pct", ("benchmark", "summary")),
    ]
    for key, label, unit, topics in spec:
        if key in perf:
            add(key, label, perf.get(key), unit, f"analysis.performance.{key}", *topics)
    costs = _g(analysis, "costs", "values") or {}
    cost_spec: list[tuple[str, str, Unit]] = [
        ("total_costs", "Total trading costs", "usd"),
        ("annual_turnover", "Annual one-way turnover (x equity)", "ratio"),
        ("cost_drag_cagr", "CAGR lost to costs", "pct"),
        ("total_dividends", "Dividends received", "usd"),
    ]
    for key, label, unit in cost_spec:
        if key in costs:
            add(key, label, costs.get(key), unit, f"analysis.costs.{key}", "costs")
    risk = analysis.get("risk") or {}
    for key, label in [
        ("historical_var", "One-day historical VaR (95%)"),
        ("historical_es", "One-day expected shortfall (95%)"),
    ]:
        if key in risk:
            add(key, label, risk.get(key), "pct", f"analysis.risk.{key}", "risk")
    for i, d in enumerate((analysis.get("drawdowns") or [])[:3]):
        rec = (d.get("recovery") or "not recovered")[:10]
        add(
            f"drawdown_{i + 1}",
            f"Drawdown #{i + 1} from {d['peak'][:10]} (trough {d['trough'][:10]}, recovery {rec})",
            d["depth"],
            "pct",
            f"analysis.drawdowns[{i}]",
            "drawdown",
        )
    contrib = analysis.get("contributions") or {}
    for i, c in enumerate((contrib.get("top") or [])[:3]):
        add(
            f"top_contributor_{i + 1}",
            f"P&L from {c['symbol']}",
            c["pnl"],
            "usd",
            f"analysis.contributions.top[{i}]",
            "contributors",
            "drawdown",
        )
    for i, c in enumerate((contrib.get("bottom") or [])[:3]):
        add(
            f"bottom_contributor_{i + 1}",
            f"P&L from {c['symbol']}",
            c["pnl"],
            "usd",
            f"analysis.contributions.bottom[{i}]",
            "contributors",
            "drawdown",
        )
    if integrity:
        s = integrity.get("summary", {})
        add(
            "integrity_summary",
            "Integrity checks (verified/warning/failed/unassessed)",
            f"{s.get('verified', 0)}/{s.get('warning', 0)}/{s.get('failed', 0)}/"
            f"{s.get('unassessed', 0)}",
            "text",
            "integrity.summary",
            "integrity",
        )
        for c in integrity.get("checks", []):
            if c["status"] in ("warning", "failed"):
                add(
                    f"integrity_{c['key']}",
                    f"{c['title']} ({c['status']})",
                    c["finding"],
                    "text",
                    f"integrity.checks.{c['key']}",
                    "integrity",
                    "validation",
                )
    if validation:
        wf = validation.get("walk_forward") or {}
        if wf and not wf.get("error"):
            add(
                "wf_in_sharpe",
                "Walk-forward mean in-sample Sharpe",
                wf.get("mean_in_sample_sharpe"),
                "ratio",
                "validation.walk_forward",
                "validation",
            )
            add(
                "wf_oos_sharpe",
                "Walk-forward mean out-of-sample Sharpe",
                wf.get("mean_oos_sharpe"),
                "ratio",
                "validation.walk_forward",
                "validation",
            )
            add(
                "wf_folds",
                "Walk-forward folds",
                len(wf.get("folds", [])),
                "count",
                "validation.walk_forward",
                "validation",
            )
        sh = _g(validation, "bootstrap", "sharpe")
        if sh:
            add(
                "sharpe_ci_low",
                "Sharpe 95% bootstrap lower bound",
                sh["lower"],
                "ratio",
                "validation.bootstrap",
                "validation",
                "risk",
            )
            add(
                "sharpe_ci_high",
                "Sharpe 95% bootstrap upper bound",
                sh["upper"],
                "ratio",
                "validation.bootstrap",
                "validation",
                "risk",
            )
        dsr = _g(validation, "deflated", "deflated_sharpe")
        if dsr is not None:
            add(
                "deflated_sharpe",
                "Deflated Sharpe ratio (probability)",
                dsr,
                "ratio",
                "validation.deflated",
                "validation",
            )
        be = _g(validation, "cost", "breakeven_multiplier")
        if be is not None:
            add(
                "breakeven_cost",
                "Cost multiple at which the edge over the benchmark vanishes",
                be,
                "ratio",
                "validation.cost",
                "costs",
                "validation",
            )
    return facts


INTENTS: list[tuple[str, tuple[str, ...]]] = [
    (
        "advice",
        (
            "should i buy",
            "should i invest",
            "should i sell",
            "should i trade",
            "is it a good investment",
            "recommend",
            "put my money",
            "allocate my",
            "will it make money",
            "buy this",
            "invest in",
        ),
    ),
    ("drawdown", ("drawdown", "worst", "loss", "lost", "crash", "decline", "underwater")),
    ("costs", ("cost", "fee", "commission", "slippage", "turnover", "trading friction")),
    (
        "benchmark",
        ("benchmark", "alpha", "beta", "outperform", "underperform", "excess", "index", "passive"),
    ),
    (
        "validation",
        (
            "out-of-sample",
            "out of sample",
            "walk-forward",
            "walk forward",
            "overfit",
            "robust",
            "luck",
            "significant",
            "confidence",
            "bootstrap",
            "deflated",
        ),
    ),
    (
        "integrity",
        (
            "integrity",
            "trust",
            "reliable",
            "bias",
            "leak",
            "look-ahead",
            "lookahead",
            "survivorship",
            "warning",
            "reproduc",
        ),
    ),
    (
        "contributors",
        (
            "contribut",
            "which stock",
            "which symbol",
            "which asset",
            "driver",
            "drove",
            "best position",
            "worst position",
        ),
    ),
    ("risk", ("risk", "volatil", "var", "value at risk", "shortfall", "tail", "sharpe", "sortino")),
    ("performance", ("return", "cagr", "perform", "profit", "how did", "how well", "growth")),
    ("summary", ("summar", "overview", "explain", "tell me about", "results")),
]

TOPIC_FOR_INTENT = {
    "summary": "summary",
    "performance": "performance",
    "risk": "risk",
    "drawdown": "drawdown",
    "costs": "costs",
    "benchmark": "benchmark",
    "validation": "validation",
    "integrity": "integrity",
    "contributors": "contributors",
}


def classify(question: str) -> list[str]:
    q = question.lower()
    hits = [name for name, kws in INTENTS if any(k in q for k in kws)]
    return hits or ["unknown"]


def _cite(f: Fact) -> str:
    return f"{f.label}: {f.display()} [{f.id}]"


def deterministic_answer(question: str, facts: list[Fact]) -> Answer:
    intents = classify(question)
    if "advice" in intents:
        return Answer(
            ADVICE_DECLINE + " I can explain the experiment's measured performance, "
            "risk, costs and validation results instead.",
            [],
            "deterministic",
            intents,
            declined=True,
        )
    if intents == ["unknown"]:
        topics = sorted({t for f in facts for t in f.topics})
        return Answer(
            "I can only answer from this experiment's computed results. Try asking "
            "about: " + ", ".join(topics) + ".",
            [],
            "deterministic",
            intents,
            notes=["question did not match any evidence topic"],
        )
    chosen: list[Fact] = []
    # Facts the question names explicitly come first (e.g. "Sharpe", "drawdown").
    q = question.lower()
    words = {w for w in re.findall(r"[a-z]{3,}", q)} - {"the", "and", "was", "what", "how"}
    for f in facts:
        label = f.label.lower()
        if any(w in label or w in f.key for w in words) and f not in chosen:
            chosen.append(f)
    for intent in intents:
        topic = TOPIC_FOR_INTENT.get(intent)
        for f in facts:
            if topic in f.topics and f not in chosen:
                chosen.append(f)
    chosen = chosen[:12]
    if not chosen:
        return Answer(
            "The evidence for that topic has not been computed for this experiment"
            + (" (run the validation suite first)." if "validation" in intents else "."),
            [],
            "deterministic",
            intents,
            notes=["no facts for requested topic"],
        )
    lines = [f"- {_cite(f)}" for f in chosen]
    undefined = [f for f in chosen if f.value is None]
    text = "From this experiment's computed results:\n" + "\n".join(lines)
    if undefined:
        text += (
            "\n\nSome statistics are undefined for this sample (for example zero "
            "variance or no recovery), so no value is reported for them."
        )
    if "validation" in intents and not any(f.source.startswith("validation.") for f in facts):
        text += (
            "\n\nThe validation suite has not been run for this experiment, so "
            "out-of-sample and bootstrap results are not available yet."
        )
    if "validation" in intents or "integrity" in intents or "summary" in intents:
        text += (
            "\n\nThese figures describe a simulation under stated assumptions and carry "
            "statistical uncertainty; they do not predict future returns."
        )
    return Answer(text, [f.id for f in chosen], "deterministic", intents)


_NUM = re.compile(r"(?<![\w.])(-?\$?\d[\d,]*(?:\.\d+)?)(\s*%|x\b|×)?")
_CITE = re.compile(r"\[F\d+\]")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _numbers_in(text: str, tol: float) -> list[tuple[float, float]]:
    out = []
    for m in _NUM.finditer(_DATE.sub(" ", text)):
        with contextlib.suppress(ValueError):
            out.append((float(m.group(1).replace("$", "").replace(",", "")), tol))
    return out


def _candidates(f: Fact) -> list[tuple[float, float]]:
    """(value, tolerance) pairs under which a number in prose may express the fact.

    Numbers in the fact's label (e.g. a 95% confidence level) and in text-valued facts
    (e.g. integrity findings) are evidence too.
    """
    # Only template percentages in labels (e.g. a 95% confidence level) count as evidence;
    # other digits in labels may come from dataset-controlled names such as symbols.
    label_numbers = [(float(m), 0.0) for m in re.findall(r"(\d+(?:\.\d+)?)%", f.label)]
    if not isinstance(f.value, (int, float)):
        text_numbers = _numbers_in(f.value, 0.051) if isinstance(f.value, str) else []
        return label_numbers + text_numbers
    v = float(f.value)
    if f.unit == "pct":
        return [*label_numbers, (v * 100, 0.051), (v, 0.0051)]
    if f.unit == "usd":
        return [*label_numbers, (v, max(1.0, abs(v) * 0.005))]
    if f.unit == "count":
        return [*label_numbers, (v, 0.5)]
    return [*label_numbers, (v, 0.0051)]


def verify_answer(text: str, facts: list[Fact]) -> dict[str, Any]:
    """Check that every number in ``text`` is supported by some fact."""
    body = _CITE.sub(" ", _DATE.sub(" ", text))
    body = re.sub(r"#\d+", " ", body)  # ordinal references such as "Drawdown #1"
    body = re.sub(r"\b\d+(?:\.\d+)?%? (?:bootstrap|confidence)", " ", body)  # "95% bootstrap"
    claims, unsupported = [], []
    pool = [c for f in facts for c in _candidates(f)]
    for m in _NUM.finditer(body):
        raw = m.group(1)
        try:
            x = float(raw.replace("$", "").replace(",", ""))
        except ValueError:
            continue
        claims.append(raw + (m.group(2) or ""))
        ok = any(abs(abs(x) - abs(v)) <= tol for v, tol in pool)
        if not ok:
            unsupported.append(raw + (m.group(2) or ""))
    cited = set(re.findall(r"\[(F\d+)\]", text))
    known = {f.id for f in facts}
    return {
        "numeric_claims": len(claims),
        "unsupported": unsupported,
        "unknown_citations": sorted(cited - known),
        "cited": sorted(cited & known),
        "grounded": not unsupported and not (cited - known),
    }
