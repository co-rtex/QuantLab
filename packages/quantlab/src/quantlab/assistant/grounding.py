"""Evidence facts, a deterministic grounded answer engine, and a numeric-claim verifier.

The assistant never computes financial statistics. It can only restate *facts* produced by
the analytics engine, each with a stable id that answers cite inline as ``[F7]``. Every
number in an answer must match a fact (within display rounding) or the answer is rejected.
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
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
    # Numbers that appear in the label *template* (e.g. a 95% confidence level). Labels
    # also contain dataset-derived names, so evidence is never parsed out of label text.
    label_numbers: tuple[float, ...] = ()

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
    synthetic: bool = False  # set by service.answer_question (#230)

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

    def add(
        key: str,
        label: str,
        value: Any,
        unit: Unit,
        source: str,
        *topics: str,
        label_numbers: tuple[float, ...] = (),
    ) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            value = None
        facts.append(
            Fact(f"F{len(facts) + 1}", key, label, value, unit, source, topics, label_numbers)
        )

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
            add(
                key,
                label,
                risk.get(key),
                "pct",
                f"analysis.risk.{key}",
                "risk",
                label_numbers=(95.0,),
            )
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


_NUM = re.compile(
    r"(?<![\w.])(?P<open>\()?(?P<sign>-(?: (?=\$?\d))?)?(?P<cur>\$?)(?P<sign2>-?)"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)"
    r"(?P<unit>\s*%|\s*percentage points?\b|\s*per ?cent\b|\s*pct\b|\s*basis points?\b|"
    r"\s*bps?\b|x\b|×)?(?P<close>\))?",
    re.I,
)
_CITE = re.compile(r"\[F\d+\]")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# A Unicode minus or an en dash before a figure is a minus sign (#15).
_DASHES = str.maketrans({"−": "-", "–": "-"})


def _norm(text: str) -> str:
    return text.translate(_DASHES)


def _unit(m: re.Match[str]) -> str:
    """How a figure in prose is expressed: "%", "pp" (percentage points), "bp", "x", "$" or
    "" (a bare number)."""
    suffix = (m["unit"] or "").strip().lower()
    if suffix in ("x", "×"):
        return "x"
    if suffix.startswith("b"):
        return "bp"
    if suffix.startswith("percentage"):
        return "pp"
    if suffix:
        return "%"
    return "$" if m["cur"] else ""


def _value(m: re.Match[str], accounting_ok: bool = True) -> float:
    """The signed figure. A minus may be followed by one space ("- 15.2%"), unless it is a
    bullet at the start of a line, and a parenthesised percentage or amount is negative,
    as in accounts ("(15.2%)", "($1,234)"), unless ``accounting_ok`` is false."""
    x = float(m["num"].replace(",", ""))
    sign = m["sign"] or ""
    bullet = " " in sign and not m.string[: m.start()].strip()
    accounting = accounting_ok and bool(m["open"] and m["close"]) and _unit(m) in ("%", "$", "bp")
    return -x if (sign and not bullet) or m["sign2"] or accounting else x


def _numbers_in(text: str) -> list[tuple[float, str]]:
    """(signed value, unit) for each figure in a text fact."""
    return [(_value(m), _unit(m)) for m in _NUM.finditer(_DATE.sub(" ", _norm(text)))]


# Ratios that are probabilities, which prose may state as a percentage.
_PROBABILITY_KEYS = {"deflated_sharpe"}
# Differences between two rates, which prose may state in percentage points (#79): "costs
# cut CAGR by 0.8 percentage points". A total return is not a number of points.
_POINT_KEYS = {"cost_drag_cagr", "excess_total_return", "alpha"}

# (value, tolerance, units it may be written in, the sign it carries, from the label template)
Candidate = tuple[float, float, frozenset[str], float, bool]
_BARE = frozenset({""})
_AMOUNT = frozenset({"", "$"})


def _candidates(f: Fact) -> list[Candidate]:
    """The figures under which prose may express the fact, and the units for each: a pct
    fact of 0.152 is "15.2%", "1520 bps" or "0.152", never "0.152%" or "15.2 bps" (#15).

    Template numbers declared on the fact (e.g. a 95% confidence level) and numbers in
    text-valued facts generated by QuantLab (e.g. integrity findings) are evidence too.
    """
    out: list[Candidate] = [(x, 0.0, frozenset({"%", ""}), x, True) for x in f.label_numbers]
    if isinstance(f.value, str):
        for v, unit in _numbers_in(f.value):
            units = _AMOUNT if unit in _AMOUNT else frozenset({unit})
            out.append((v, 0.051, units, v, False))
    if not isinstance(f.value, (int, float)):
        return out
    v = float(f.value)
    if f.unit == "pct":
        out += [
            (v * 100, 0.051, frozenset({"%", "pp"} if f.key in _POINT_KEYS else {"%"}), v, False),
            (v * 10_000, 0.51, frozenset({"bp"}), v, False),
            (v, 0.0051, _BARE, v, False),
        ]
    elif f.unit == "usd":
        out.append((v, max(1.0, abs(v) * 0.005), _AMOUNT, v, False))
    elif f.unit == "count":
        out.append((v, 0.5, _BARE, v, False))
    else:
        out.append((v, 0.0051, frozenset({"", "x"}), v, False))
        if f.key in _PROBABILITY_KEYS:
            out.append((v * 100, 0.051, frozenset({"%"}), v, False))
    return out


# Sentence boundaries: end punctuation followed by space, or a line break. Decimal points
# ("0.46") are not followed by whitespace, so they never split a sentence, and neither does
# the space before a citation ("... regimes. [F34]" keeps its citation). A semicolon joins
# clauses that share the citations at the end ("Total return: 15.2%; maximum drawdown:
# -28.3% [F3][F2]"), so it is a clause boundary (below), not a sentence one (#79).
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?!\[F\d+\])|\n+")
# Clause boundaries (commas in numbers, "12,345", are not followed by a space). A figure is
# read with the words of its scope: its own clause plus the clauses around it that carry no
# figure of their own, so "15.2%, a loss" and "Total return, which fell, was 15.2%" still
# state a loss (#94). Only a leading subordinate clause with no figure is a scope of its
# own: in "Although the benchmark fell, the strategy returned 15.2%" the fall is the
# benchmark's (#79).
_CLAUSE = re.compile(r"[,;]\s|\s(?:and|but|while|whereas)\s", re.I)
_SUBORDINATE = re.compile(
    r"\s*(?:although|though|even though|despite|in spite of|after|before|unlike|since|"
    r"during|whereas|while|when)\b",
    re.I,
)
# "the VaR of 2.13%": a quantity named right before "of" owns the figure.
_OF = re.compile(r"\s+of\s+(?:(?:about|around|roughly|nearly|almost|just|only)\s+)?", re.I)

# What kind of quantity each fact is, so prose that names a quantity can only be backed by a
# fact of that kind (#15): "Total return was 28.3% [F2]" is unsupported when F2 is the
# drawdown, even though 28.3% is F2's magnitude.
_KIND_OF_KEY: list[tuple[str, str]] = [
    ("total_return", "return"),
    ("cagr", "return"),
    ("excess_total_return", "return"),
    ("benchmark_total_return", "return"),
    ("worst_day", "return"),
    ("cost_drag_cagr", "costs"),
    ("total_costs", "costs"),
    ("breakeven_cost", "costs"),
    ("annual_turnover", "turnover"),
    ("total_dividends", "dividends"),
    ("max_drawdown", "drawdown"),
    ("drawdown_", "drawdown"),
    ("volatility", "volatility"),
    ("tracking_error", "tracking error"),
    ("information_ratio", "information ratio"),
    ("sortino", "sortino"),
    ("calmar", "calmar"),
    ("sharpe", "sharpe"),
    ("wf_in_sharpe", "sharpe"),
    ("wf_oos_sharpe", "sharpe"),
    ("sharpe_ci_", "sharpe"),
    ("deflated_sharpe", "probability"),
    ("beta", "beta"),
    ("alpha", "alpha"),
    ("historical_var", "var"),
    ("historical_es", "shortfall"),
    ("top_contributor_", "pnl"),
    ("bottom_contributor_", "pnl"),
    ("sessions", "sessions"),
]

# Losses measured as magnitudes. Prose may state one without its sign ("a 28.3% drawdown"
# for -28.3%, "lost 0.8% to costs"), but only when it says the figure is that kind of loss:
# "Equity rose 28.3% [F2]" is not a drawdown (#15).
_MAGNITUDE_KINDS = frozenset({"drawdown", "var", "shortfall", "costs"})
_GAINS = frozenset({"return", "pnl", "alpha"})
_LOSSES = _GAINS | _MAGNITUDE_KINDS

# Words that name a kind of quantity in prose, the kinds each may refer to, and the
# direction it states. Earlier patterns win where they overlap ("profit and loss").
_KIND_WORDS: list[tuple[frozenset[str], re.Pattern[str], str | None]] = [
    (frozenset({"tracking error"}), re.compile(r"\btracking error\b", re.I), None),
    (frozenset({"information ratio"}), re.compile(r"\binformation ratio\b", re.I), None),
    (frozenset({"shortfall"}), re.compile(r"\bexpected shortfall\b|\bshortfall\b", re.I), None),
    (frozenset({"var"}), re.compile(r"\bvalue at risk\b|\bVaR\b"), None),
    (
        frozenset({"drawdown"}),
        re.compile(
            r"\bdraw-?downs?\b|\bpeak[- ]to[- ]trough\b|\b(?:from|below) (?:its |the )?peak\b|"
            r"\b(?:deepest|largest|biggest|worst|maximum|max) (?:decline|fall|drop|loss)\b|"
            r"\bunderwater\b",
            re.I,
        ),
        None,
    ),
    (frozenset({"volatility"}), re.compile(r"\bvolatility\b|\bvol\b", re.I), None),
    # Before "sharpe": the deflated Sharpe ratio is a probability, not a Sharpe ratio (#74).
    (
        frozenset({"probability"}),
        re.compile(r"\bdeflated sharpe(?: ratio)?\b|\bDSR\b|\bprobabilit(?:y|ies)\b", re.I),
        None,
    ),
    (frozenset({"sharpe"}), re.compile(r"\bsharpe\b", re.I), None),
    (frozenset({"sortino"}), re.compile(r"\bsortino\b", re.I), None),
    (frozenset({"calmar"}), re.compile(r"\bcalmar\b", re.I), None),
    (frozenset({"beta"}), re.compile(r"\bbeta\b", re.I), None),
    (frozenset({"alpha"}), re.compile(r"\balpha\b", re.I), None),
    (frozenset({"turnover"}), re.compile(r"\bturnover\b", re.I), None),
    (frozenset({"dividends"}), re.compile(r"\bdividends?\b", re.I), None),
    (
        frozenset({"costs"}),
        re.compile(r"\bcosts?\b|\bfees?\b|\bcommissions?\b|\bslippage\b", re.I),
        None,
    ),
    (frozenset({"pnl"}), re.compile(r"\bP&L\b|\bPnL\b|\bprofit and loss\b", re.I), None),
    (frozenset({"sessions"}), re.compile(r"\bsessions\b|\btrading days\b", re.I), None),
    # Before "return": what costs take from CAGR is a cost ("Costs reduced CAGR by 0.8%").
    (
        frozenset({"costs"}),
        re.compile(
            r"\b(?:reduc(?:e|es|ed|ing)|cut|cuts|lower(?:s|ed)?|drag(?:s|ged)?|shaved|trimmed)"
            r"\s+(?:the\s+)?(?:annual(?:ized)?\s+)?(?:CAGR|returns?)\b|\bdrag on (?:the\s+)?CAGR\b",
            re.I,
        ),
        None,
    ),
    (frozenset({"return"}), re.compile(r"\b(?:total )?returns?\b|\bCAGR\b", re.I), None),
    (
        _GAINS,
        re.compile(
            r"\breturned\b|\bmade\b|\bmakes\b|\bearn(?:s|ed)?\b|\bgain(?:s|ed)?\b|"
            r"\bprofit(?:s|able|ed)?\b|\bdelivered\b|\byielded\b|\bappreciated\b|"
            r"\boutperform(?:s|ed)?\b",
            re.I,
        ),
        "gain",
    ),
    (
        frozenset({"return", "alpha"}),
        re.compile(r"\bunderperform(?:s|ed)?\b|\blag(?:s|ged|ging)\b|\btrail(?:s|ed|ing)\b", re.I),
        "loss",
    ),
    (_LOSSES, re.compile(r"\blos(?:e|es|t|ing|s|ses)\b", re.I), "loss"),
]
# Words that state a fall without naming a quantity ("Equity fell 4%").
_FALL = re.compile(
    r"\b(?:fell|fall(?:s|en|ing)?|declin(?:e|es|ed|ing)|drop(?:s|ped|ping)?|down|shed|sheds|"
    r"slid|sliding|sinking|"
    r"sank|tumbled|negative|below zero|lower|worse)\b",
    re.I,
)


def _directions(pattern: re.Pattern[str], body: str) -> list[re.Match[str]]:
    """The direction words of ``pattern`` in ``body``, without attributive participles:
    in "In a falling market" the market is falling, not the figure."""
    return [
        d
        for d in pattern.finditer(body)
        if not (
            d.group(0).lower().endswith("ing")
            and re.search(r"\b(?:a|an|the|this|that)\s+$", body[: d.start()], re.I)
        )
    ]


# Words that state a rise. They state a gain for the figure they modify, before it ("equity
# rose 28.3%") or right after it ("28.3% higher"), unless their subject is a loss: in "the
# drawdown grew to 28.3%" the loss deepened (#74).
_RISE = re.compile(
    r"\b(?:rose|rise[sn]?|rising|grew|grow(?:s|n|ing)?|increas(?:e|es|ed|ing)|up(?! to\b)|"
    r"higher|climb(?:s|ed|ing)?|advanc(?:e|es|ed|ing))\b",
    re.I,
)
# Words that may stand between a loss and the rise it is the subject of.
_AUX = re.compile(
    r"\s*(?:(?:has|had|have|was|is|then|later|eventually|further|steadily|briefly)\s+)*",
    re.I,
)
_LABEL_MARK = re.compile(r"\x00L(\d+)\x00")


def _kinds(f: Fact) -> frozenset[str]:
    """The kinds of quantity a fact is: from its key, else (for numeric facts) from the
    first kind word in its label, else (for text facts) every kind its text names. A fact
    with no kind backs only numbers the sentence does not name a kind for."""
    for prefix, kind in _KIND_OF_KEY:
        if f.key == prefix or (prefix.endswith("_") and f.key.startswith(prefix)):
            return frozenset({kind})
    if isinstance(f.value, (int, float)):
        found = [(m.start(), kinds) for kinds, p, _ in _KIND_WORDS for m in p.finditer(f.label)]
        return min(found, key=lambda t: t[0])[1] if found else frozenset()
    text = f"{f.label} {f.value or ''}"
    return frozenset(k for kinds, p, _ in _KIND_WORDS if p.search(text) for k in kinds)


# (start, end, kinds, the fact when it is a quoted label, the direction it states)
Mention = tuple[int, int, frozenset[str], "Fact | None", str | None]


def _mentions(body: str, cited: list[Fact]) -> list[Mention]:
    """Each quantity named in ``body``: a cited fact's own label (bound to that fact) or a
    kind word such as "drawdown" or "returned"."""
    out: list[Mention] = []
    for m in _LABEL_MARK.finditer(body):
        f = cited[int(m.group(1))]
        out.append((m.start(), m.end(), _kinds(f), f, None))
    for kinds, pattern, direction in _KIND_WORDS:
        out += [(m.start(), m.end(), kinds, None, direction) for m in pattern.finditer(body)]
        body = pattern.sub(lambda m: " " * len(m.group(0)), body)
    return out


Binding = tuple[frozenset[str], "Fact | None"]


def _assign(
    mentions: list[Mention], numbers: list[tuple[int, int]], clause: Callable[[int], int]
) -> dict[tuple[int, int], list[Mention]]:
    """Give each mention to the adjacent number it is closest to (the following one on a
    tie: "Sharpe 0.46, drawdown 28.3%"), among the numbers of its own clause when it has
    any: in "Volatility was 18.3% and costs were $12,345" the costs are the $12,345 (#79).
    A number must be supported under every mention it gets, so a kind word placed after
    a number cannot rebind it away from the one before it: in "Total return was 28.3%
    drawdown" both words go to 28.3%, and they disagree."""
    out: dict[tuple[int, int], list[Mention]] = {n: [] for n in numbers}
    for m in mentions:
        mine = [n for n in numbers if clause(n[0]) == clause(m[0])] or numbers
        prev = [n for n in mine if n[1] <= m[0]]
        nxt = [n for n in mine if n[0] >= m[1]]
        options = []
        if prev:
            p = max(prev, key=lambda n: n[1])
            options.append((m[0] - p[1], 1, p))
        if nxt:
            q = min(nxt, key=lambda n: n[0])
            options.append((q[0] - m[1], 0, q))
        if options:
            out[min(options)[2]].append(m)
    return out


def _nearest(start: int, end: int, mentions: list[Mention]) -> Mention | None:
    """The mention nearest a number in either direction (the earlier one on a tie), or
    None when the sentence names nothing. A number no word was assigned to ("Total return
    was 15.2% and 28.3% [F2]") still speaks about what the sentence names."""
    if not mentions:
        return None

    def distance(m: Mention) -> tuple[int, int]:
        return (start - m[1], 0) if m[1] <= start else (m[0] - end, 1)

    return min(mentions, key=distance)


# Words that cannot be a subject of their own: a clause that starts with one ("Although it
# fell", "While negative", "While a loss", "Despite falling") is about the main clause's
# subject, so its words apply there (#94).
_NO_SUBJECT = frozenset(
    re.findall(
        r"\w+",
        "it its they their them this that these those there he she we a an negative "
        "positive down up lower higher flat weak weaker strong stronger small modest sharp "
        "sharply slightly briefly still so very much more less mostly largely deeply also "
        "then again initially once recently",
    )
)


# The strategy under test, however it is named: the subject of the main clause, never one
# of its own (#94). "Although the portfolio fell, total return was 15.2%" is about it.
_MAIN_SUBJECT = re.compile(
    r"\b(?:strateg(?:y|ies)|portfolios?|equity|funds?|accounts?|it|its|our|we|us)\b", re.I
)


def _own_subject(
    masked: str, s: int, e: int, upto: int, mentions: list[Mention], directions: list[int]
) -> bool:
    """Whether the figure-less clause masked[s:e] is about something other than the main
    clause: "Although the benchmark fell", "Despite losses elsewhere", "After the
    drawdown", but not "Although it fell", "Although the portfolio fell" or "Although the
    return fell". Nothing up to the main clause (``upto``) may name the strategy itself:
    in "Although the benchmark fell, and so did the strategy," the fall is its, too."""
    text = masked[s:e]
    if _MAIN_SUBJECT.search(masked[s:upto]):
        return False
    if re.search(r"\b(?:elsewhere|others?)\b", text, re.I):
        return True
    # A quantity named with a direction ("the return fell") is the main clause's quantity.
    if any(s <= w[0] < e for w in mentions) and any(s <= d < e for d in directions):
        return False
    lead = _SUBORDINATE.match(text)
    words = re.findall(r"[A-Za-z][\w'&-]*", text[lead.end() :] if lead else text)
    while words and words[0].lower() in ("a", "an", "the", "this", "that", "these", "those"):
        words.pop(0)
    if not words:
        # Only a named quantity: its own subject ("After the drawdown"), unless it states a
        # direction ("While a loss").
        named = [w for w in mentions if s <= w[0] < e]
        return bool(named) and not any(w[4] for w in named)
    w = words[0]
    return not (
        w.lower() in _NO_SUBJECT
        or w.lower().endswith("ing")
        or _FALL.fullmatch(w) is not None
        or _RISE.fullmatch(w) is not None
    )


def _scopes(
    masked: str, figures: list[int], mentions: list[Mention], directions: list[int]
) -> list[tuple[int, int, bool]]:
    """(start, end, carries a figure) for each scope of a sentence: a clause that carries
    a figure, with the figure-less clauses that trail or lead it. A figure-less clause
    that opens the sentence and has a subject of its own is a scope of its own when it is
    subordinate ("Although the benchmark fell,") or followed by "but" ("The benchmark
    fell, but ...")."""
    cuts = list(_CLAUSE.finditer(masked))
    edges = [0, *(c.end() for c in cuts), len(masked)]
    out: list[list[Any]] = []  # [start, end, has figure, closed]
    for i in range(len(edges) - 1):
        s, e = edges[i], edges[i + 1]
        fig = any(s <= p < e for p in figures)
        # Where the clause with the next figure starts.
        upto = next(
            (
                edges[j]
                for j in range(i, len(edges) - 1)
                if any(edges[j] <= p < edges[j + 1] for p in figures)
            ),
            len(masked),
        )
        sep = cuts[i - 1].group(0).strip().lower() if i else ""
        nxt = masked[e : edges[i + 2]] if i + 2 < len(edges) else ""
        sub = sep in ("while", "whereas") or _SUBORDINATE.match(masked[s:e]) is not None
        contrast = re.match(r"\s*but\b", nxt, re.I) is not None
        # Opens the sentence: "Total return, although it fell," does not.
        leading = not out or out[-1][3]
        if (
            (sub or contrast)
            and not fig
            and leading
            and _own_subject(masked, s, e, upto, mentions, directions)
        ):
            out.append([s, e, False, True])
        elif out and not out[-1][3] and (not fig or not out[-1][2]):
            out[-1][1], out[-1][2] = e, out[-1][2] or fig
        else:
            out.append([s, e, fig, False])
    return [(s, e, fig) for s, e, fig, _ in out]


def verify_answer(text: str, facts: list[Fact]) -> dict[str, Any]:
    """Check that every number in ``text`` is supported by a fact cited next to it.

    * A sentence that states a number must cite at least one fact, and each number must
      come from a fact cited in that sentence, not from any fact in the pool.
    * The number must come from the fact the sentence talks about: next to a cited fact's
      label, only that fact; next to a word that names a kind of quantity ("returned",
      "drawdown", "Sharpe"), only a fact of that kind. "The strategy returned 28.3% [F2]"
      is unsupported when F2 is the -28.3% drawdown.
    * Units: a percentage fact is written as a percentage, basis points or a fraction, and
      a ratio is not a percentage. "Total return was 0.152%" does not state 15.2%.
    * Signs: a figure states a negative value when it carries a minus sign or a word says it
      fell or was lost ("lost", "fell", "negative", "a 15.2% loss"), and a positive value
      otherwise, so it must match the fact's sign. Only drawdowns, VaR, expected shortfall
      and costs may be stated as magnitudes, and only when the sentence says the figure is
      one ("a 28.3% drawdown", "lost 28.3% peak to trough"). Even then a rise word that
      modifies the figure states a gain ("During the drawdown, equity rose 28.3%"), unless
      the loss is its subject ("the drawdown grew to 28.3%").
    """
    text = _norm(text)
    facts = [
        replace(f, label=_norm(f.label), value=_norm(f.value))
        if isinstance(f.value, str)
        else replace(f, label=_norm(f.label))
        for f in facts
    ]
    by_id = {f.id: f for f in facts}
    claims: list[str] = []
    unsupported: list[str] = []
    # A text fact quoted in full (an integrity finding) may itself contain sentences; keep
    # it in one piece so it stays with the citation that follows it.
    for f in facts:
        if isinstance(f.value, str) and f.value in text and _SENTENCE.search(f.value):
            text = text.replace(f.value, _SENTENCE.sub("\x01", f.value))
    for sentence in (s.replace("\x01", " ") for s in _SENTENCE.split(text)):
        cited = list(
            dict.fromkeys(by_id[c] for c in re.findall(r"\[(F\d+)\]", sentence) if c in by_id)
        )
        body = sentence
        # A cited fact's label quoted in the sentence names that fact exactly. Longest
        # labels first, so "Total return minus SYNIDX" is not read as "Total return".
        for i, f in sorted(enumerate(cited), key=lambda t: -len(t[1].label)):
            body = re.sub(re.escape(f.label), f"\x00L{i}\x00", body, flags=re.I)
        # A cited text fact (an integrity finding) quoted in full is that fact, too.
        for i, f in enumerate(cited):
            if isinstance(f.value, str) and f.value:
                body = body.replace(f.value, f"\x00L{i}\x00")
        body = _CITE.sub(" ", _DATE.sub(" ", body))
        body = re.sub(r"#\d+", " ", body)  # ordinal references such as "Drawdown #1"
        body = re.sub(r"\b\d+(?:\.\d+)?%? (?:bootstrap|confidence)", " ", body)  # "95% ..."
        mentions = _mentions(body, cited)
        masked = body
        for w in mentions:  # "profit and loss" is one name, not two clauses
            masked = masked[: w[0]] + " " * (w[1] - w[0]) + masked[w[1] :]
        numbers = list(_NUM.finditer(body))
        directions = [
            *(d.start() for d in _directions(_FALL, body)),
            *(d.start() for d in _directions(_RISE, body)),
            *(w[0] for w in mentions if w[4]),
        ]
        scopes = _scopes(masked, [n.start() for n in numbers], mentions, directions)
        firsts = [s[0] for s in scopes]

        def clause(pos: int, firsts: list[int] = firsts) -> int:
            return bisect.bisect_right(firsts, pos) - 1

        # Words of a cited fact's label ("SYNIDX"): "SYNIDX (10.1%)" is an aside.
        label_words = {w.lower() for f in cited for w in re.findall(r"[A-Za-z]{2,}", f.label)}
        templates = {x for f in cited for x in f.label_numbers}

        falls = [(m.start(), m.end()) for m in _directions(_FALL, body)]
        rises = [
            (r.start(), r.end())
            for r in _directions(_RISE, body)
            if not any(
                w[2] & _MAGNITUDE_KINDS
                and w[1] <= r.start()
                and _AUX.fullmatch(body[w[1] : r.start()])
                for w in mentions
            )
        ]
        assigned = _assign(mentions, [(m.start(), m.end()) for m in numbers], clause)
        prev_end = 0
        for k, m in enumerate(numbers):
            # "Total return (15.2%)", "SYNIDX (10.1%)", "5.1% (510 bps)": parentheses right
            # after a name or another figure are an aside, not accounting notation (#79).
            before = body[: m.start()].rstrip()
            last = re.search(r"([A-Za-z]{2,})$", before)
            aside = (
                any(w[1] <= m.start() and not body[w[1] : m.start()].strip() for w in mentions)
                or (k > 0 and not body[numbers[k - 1].end() : m.start()].strip())
                or (last is not None and last.group(1).lower() in label_words)
            )
            x, unit = _value(m, accounting_ok=not aside), _unit(m)
            claim = m.group(0).strip()
            claims.append(claim)
            c = clause(m.start())
            start = max(prev_end, scopes[c][0])
            # Direction words after the figure count up to the next figure of its scope
            # ("15.2%, a decline", "15.2%, but negative").
            later = [n.start() for n in numbers[k + 1 :] if clause(n.start()) == c]
            stop = min([*later, scopes[c][1]])
            # The words of the figure's own clause speak for it when there are any; a
            # quantity attached by "of" ("the VaR of 2.13%") speaks for it alone.
            own = assigned[(m.start(), m.end())]
            own = [w for w in own if clause(w[0]) == c] or own
            own = [
                w for w in own if w[1] <= m.start() and _OF.fullmatch(body[w[1] : m.start()])
            ] or own
            # A word states the direction of the first number after it in its clause
            # ("gained 15.2% with a 28.3% drawdown" asserts a gain of 15.2% only), or of
            # the number it is assigned to ("a 15.2% loss", "a 4.12% drop").
            words = [w for w in mentions if start <= w[1] <= m.start()] + own
            loss = any(start <= e <= m.start() or m.end() <= s < stop for s, e in falls) or any(
                w[4] == "loss" for w in words
            )
            gain = any(w[4] == "gain" for w in words) or any(
                start <= e <= m.start() or m.end() <= s < stop for s, e in rises
            )
            prev_end = m.end()
            # The nearest name speaks for a figure no word was given to, from its own scope,
            # else from a leading clause with no figure ("During the drawdown, equity rose
            # 28.3%"), but never across another figure: in "rose 15.2% despite a 28.3%
            # drawdown" the drawdown is the 28.3%.
            pool = [w for w in mentions if clause(w[0]) == c] or [
                w for w in mentions if not scopes[clause(w[0])][2]
            ]
            # A label's template number ("VaR (95%)") is part of the name, not a figure.
            figures = [n for n in numbers if n is not m and abs(_value(n)) not in templates]
            pool = [
                w
                for w in pool
                if not any(min(w[1], m.start()) <= n.start() < max(w[0], m.end()) for n in figures)
            ]
            near = _nearest(m.start(), m.end(), pool)
            bindings: list[Binding | None] = [(w[2], w[3]) for w in own]
            bindings.append(None if near is None else (near[2], near[3]))
            if all(_supported(x, unit, cited, b, loss, gain) for b in bindings):
                continue
            unsupported.append(claim)
    cited_ids = set(re.findall(r"\[(F\d+)\]", text))
    known = {f.id for f in facts}
    return {
        "numeric_claims": len(claims),
        "unsupported": unsupported,
        "unknown_citations": sorted(cited_ids - known),
        "cited": sorted(cited_ids & known),
        "grounded": not unsupported and not (cited_ids - known),
    }


def _supported(
    x: float, unit: str, cited: list[Fact], bound: Binding | None, loss: bool, gain: bool
) -> bool:
    """Whether a cited fact supports the figure ``x`` written in ``unit``, as the sentence
    names it (``bound``) and with the direction its words state."""
    if bound is None:
        named: frozenset[str] = frozenset()
        scope = cited
    elif bound[1] is not None:
        named = _kinds(bound[1])
        scope = [bound[1]]  # next to a fact's own label: that fact, nothing else
    else:
        named = bound[0]
        scope = [f for f in cited if _kinds(f) & named]
    for f in scope:
        kinds = _kinds(f)
        # A loss magnitude may be stated unsigned only when the sentence says what loss it
        # is, and a numeric loss fact backs nothing else.
        magnitude = bool(kinds & named & _MAGNITUDE_KINDS)
        if kinds & _MAGNITUDE_KINDS and not magnitude and not isinstance(f.value, str):
            continue
        for v, tol, units, signed, template in _candidates(f):
            if unit not in units or abs(abs(x) - abs(v)) > tol:
                continue
            if f.key in _PROBABILITY_KEYS and unit == "%" and "probability" not in named:
                continue  # "62%" is a probability only where the sentence says so
            if template:
                return True  # a number from the label template ("VaR (95%)")
            if magnitude:
                if gain and signed < 0:
                    continue  # "the drawdown gained 28.3%"
                return True
            if loss and gain and x >= 0:
                continue  # "Since falling, total return rose 15.2%": which is it?
            negative = x < 0 or loss
            if signed != 0 and negative != (signed < 0):
                continue  # "lost 15.2%" for +15.2%, "15.2%" for -15.2%
            return True
    return False
