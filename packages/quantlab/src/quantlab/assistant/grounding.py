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

    Template numbers declared on the fact (e.g. a 95% confidence level) and numbers in
    text-valued facts generated by QuantLab (e.g. integrity findings) are evidence too.
    """
    label_numbers = [(x, 0.0) for x in f.label_numbers]
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


# Sentence boundaries: end punctuation followed by space, or a line break. Decimal points
# ("0.46") are not followed by whitespace, so they never split a sentence, and neither does
# the space before a citation ("... regimes. [F34]" keeps its citation).
_SENTENCE = re.compile(r"(?<=[.!?;])\s+(?!\[F\d+\])|\n+")

# What kind of quantity each fact is, so prose that names a quantity can only be backed by a
# fact of that kind (#15): "Total return was 28.3% [F2]" is unsupported when F2 is the
# drawdown, even though 28.3% is F2's magnitude. Keys not listed are unrestricted.
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
    ("deflated_sharpe", "sharpe"),
    ("beta", "beta"),
    ("alpha", "alpha"),
    ("historical_var", "var"),
    ("historical_es", "shortfall"),
    ("top_contributor_", "pnl"),
    ("bottom_contributor_", "pnl"),
    ("sessions", "sessions"),
]

# Words that name a kind of quantity in prose. Longer phrases come first.
_KIND_WORDS: list[tuple[str, re.Pattern[str]]] = [
    ("tracking error", re.compile(r"\btracking error\b", re.I)),
    ("information ratio", re.compile(r"\binformation ratio\b", re.I)),
    ("shortfall", re.compile(r"\bexpected shortfall\b|\bshortfall\b", re.I)),
    ("var", re.compile(r"\bvalue at risk\b|\bVaR\b")),
    ("drawdown", re.compile(r"\bdraw-?downs?\b", re.I)),
    ("volatility", re.compile(r"\bvolatility\b|\bvol\b", re.I)),
    ("sharpe", re.compile(r"\bsharpe\b", re.I)),
    ("sortino", re.compile(r"\bsortino\b", re.I)),
    ("calmar", re.compile(r"\bcalmar\b", re.I)),
    ("beta", re.compile(r"\bbeta\b", re.I)),
    ("alpha", re.compile(r"\balpha\b", re.I)),
    ("turnover", re.compile(r"\bturnover\b", re.I)),
    ("dividends", re.compile(r"\bdividends?\b", re.I)),
    ("costs", re.compile(r"\bcosts?\b|\bfees?\b|\bcommissions?\b|\bslippage\b", re.I)),
    ("pnl", re.compile(r"\bP&L\b|\bPnL\b|\bprofit and loss\b", re.I)),
    ("sessions", re.compile(r"\bsessions\b|\btrading days\b", re.I)),
    (
        "return",
        re.compile(
            r"\b(?:total )?returns?\b|\breturned\b|\bCAGR\b|\bprofit(?:s|able|ed)?\b|"
            r"\bmade\b|\bearned\b|\bgain(?:s|ed)?\b|\blost\b|\blos(?:s|ses)\b|\blosing\b",
            re.I,
        ),
    ),
]

# Verbs that assert a gain or a loss govern the first number after them.
# Verbs that assert a return's gain or loss state a sign for any figure they govern
# ("returned 28.3%" is a gain, whatever is cited). Neutral movement verbs ("grew", "fell")
# state a sign only for signed kinds: a drawdown that "grew to 28.3%" deepened.
_GAIN_RETURN = re.compile(r"\b(?:gain(?:ed|s)?|profit(?:ed|s)?|earned|made|returned)\b", re.I)
_GAIN = re.compile(r"\b(?:rose|grew|increased|up)\b", re.I)
_LOSS_RETURN = re.compile(r"\b(?:lost|los(?:s|ses)|losing)\b", re.I)
_LOSS = re.compile(r"\b(?:fell|declined|dropped|down|shed)\b", re.I)
# Kinds where a gain or loss verb states the sign of the quantity itself. For a drawdown,
# "grew to 28.3%" describes a deeper (more negative) drawdown, so verbs say nothing.
_SIGNED_KINDS = {"return", "pnl", "alpha"}
_LABEL_MARK = re.compile(r"\x00L(\d+)\x00")


def _kind(f: Fact) -> str | None:
    """The kind of quantity a fact is: from its key, else (for numeric facts) from the first
    kind word in its label. Text facts (integrity findings) have no kind."""
    for prefix, kind in _KIND_OF_KEY:
        if f.key == prefix or (prefix.endswith("_") and f.key.startswith(prefix)):
            return kind
    if isinstance(f.value, (int, float)):
        found = [(m.start(), kind) for kind, p in _KIND_WORDS for m in p.finditer(f.label)]
        if found:
            return min(found)[1]
    return None


Mention = tuple[int, int, str | None, "Fact | None"]


def _mentions(body: str, cited: list[Fact]) -> list[Mention]:
    """(start, end, kind, fact) for each quantity named in ``body``: a cited fact's own
    label (bound to that fact) or a kind word such as "drawdown" or "returned"."""
    out: list[Mention] = []
    for m in _LABEL_MARK.finditer(body):
        f = cited[int(m.group(1))]
        out.append((m.start(), m.end(), _kind(f), f))
    for kind, pattern in _KIND_WORDS:
        out += [(m.start(), m.end(), kind, None) for m in pattern.finditer(body)]
    return out


Binding = tuple[str | None, "Fact | None"]


def _assign(
    mentions: list[Mention], numbers: list[tuple[int, int]]
) -> dict[tuple[int, int], list[Binding]]:
    """Give each mention to the adjacent number it is closest to (the following one on a
    tie: "Sharpe 0.46, drawdown 28.3%"). A number must be supported under every mention
    it gets, so a kind word placed after a number cannot rebind it away from the one
    before it: in "Total return was 28.3% drawdown" both words go to 28.3%, and they
    disagree."""
    out: dict[tuple[int, int], list[Binding]] = {n: [] for n in numbers}
    for m in mentions:
        prev = [n for n in numbers if n[1] <= m[0]]
        nxt = [n for n in numbers if n[0] >= m[1]]
        options = []
        if prev:
            p = max(prev, key=lambda n: n[1])
            options.append((m[0] - p[1], 1, p))
        if nxt:
            q = min(nxt, key=lambda n: n[0])
            options.append((q[0] - m[1], 0, q))
        if options:
            out[min(options)[2]].append((m[2], m[3]))
    return out


def _nearest(start: int, end: int, mentions: list[Mention]) -> Binding | None:
    """The mention nearest a number in either direction (the earlier one on a tie), or
    None when the sentence names nothing. A number no word was assigned to ("Total return
    was 15.2% and 28.3% [F2]") still speaks about what the sentence names."""
    if not mentions:
        return None

    def distance(m: Mention) -> tuple[int, int]:
        return (start - m[1], 0) if m[1] <= start else (m[0] - end, 1)

    nearest = min(mentions, key=distance)
    return nearest[2], nearest[3]


def verify_answer(text: str, facts: list[Fact]) -> dict[str, Any]:
    """Check that every number in ``text`` is supported by a fact cited next to it.

    * A sentence that states a number must cite at least one fact, and each number must
      come from a fact cited in that sentence, not from any fact in the pool.
    * The number must come from the fact the sentence talks about: next to a cited fact's
      label, only that fact; next to a word that names a kind of quantity ("returned",
      "drawdown", "Sharpe"), only a fact of that kind. "The strategy returned 28.3% [F2]"
      is unsupported when F2 is the -28.3% drawdown.
    * Signs: an explicitly negative number needs a negative fact. For returns and P&L, a
      gain verb needs a non-negative fact and a loss verb a negative one; prose may state a
      negative fact's magnitude ("a 28.3% drawdown", "the drawdown grew to 28.3%").
    """
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
        verbs = {
            name: [g.end() for g in pattern.finditer(body)]
            for name, pattern in (
                ("gain", _GAIN),
                ("loss", _LOSS),
                ("gain_return", _GAIN_RETURN),
                ("loss_return", _LOSS_RETURN),
            )
        }
        assigned = _assign(mentions, [(m.start(), m.end()) for m in _NUM.finditer(body)])
        prev_end = 0
        for m in _NUM.finditer(body):
            raw = m.group(1)
            try:
                x = float(raw.replace("$", "").replace(",", ""))
            except ValueError:
                continue
            claim = raw + (m.group(2) or "")
            claims.append(claim)
            # A verb governs the first number after it ("gained 15.2% with a 28.3%
            # drawdown" asserts a gain of 15.2% only).
            said = {k: any(prev_end <= e <= m.start() for e in v) for k, v in verbs.items()}
            prev_end = m.end()
            bindings: list[Binding | None] = list(assigned[(m.start(), m.end())])
            bindings.append(_nearest(m.start(), m.end(), mentions))
            if all(_supported(x, cited, b, said) for b in bindings):
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
    x: float,
    cited: list[Fact],
    bound: tuple[str | None, Fact | None] | None,
    said: dict[str, bool],
) -> bool:
    """Whether a cited fact supports the number ``x`` as the sentence states it, given the
    gain and loss verbs (``said``) that govern it."""
    if bound is not None and bound[1] is not None:
        scope = [bound[1]]  # next to a fact's own label: that fact, nothing else
    elif bound is not None and bound[0] is not None:
        scope = [f for f in cited if _kind(f) in (bound[0], None)]
    else:
        scope = cited
    for f in scope:
        kind = _kind(f)
        for v, tol in _candidates(f):
            if abs(abs(x) - abs(v)) > tol:
                continue
            # Numbers inside a text fact carry their own sign (v is parsed from the text).
            signed = float(f.value) if isinstance(f.value, (int, float)) else v
            if x < 0 and signed > 0:
                continue  # "-15.2%" for a +15.2% fact
            if v in f.label_numbers:
                return True  # a number from the label template ("VaR (95%)")
            # Unknown kinds keep the conservative rule: any verb states the sign.
            signed_kind = kind in _SIGNED_KINDS or kind is None
            gain = said["gain_return"] or (signed_kind and said["gain"])
            loss = said["loss_return"] or (signed_kind and said["loss"])
            if x > 0 and gain and signed < 0:
                continue  # "returned 28.3%" for a -28.3% fact
            if x > 0 and loss and signed > 0:
                continue  # "lost 15.2%" for a +15.2% fact
            return True
    return False
