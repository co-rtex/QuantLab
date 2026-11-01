"""Evaluation suite for the grounded research assistant.

Runs a fixed question set against the deterministic engine and against scripted providers
that misbehave in known ways (hallucinated numbers, investment advice, malformed output,
outages). The pass criteria are properties of the *final answer shown to the user*:

* numerical consistency: every number is supported by an evidence fact,
* grounding: answers to in-scope questions cite at least one fact,
* advice: questions asking what to buy or invest in are declined,
* robustness: provider failures fall back to a grounded deterministic answer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, ClassVar

from quantlab.assistant.grounding import Fact, verify_answer
from quantlab.assistant.service import ProviderError, answer_question


@dataclass(frozen=True)
class Case:
    question: str
    expect_topic: str | None = None  # a fact topic that must be cited
    advice: bool = False
    out_of_scope: bool = False


CASES: list[Case] = [
    Case("Summarize the results of this experiment.", "summary"),
    Case("How did the strategy perform?", "performance"),
    Case("What was the Sharpe ratio and volatility?", "risk"),
    Case("What was the worst drawdown and when did it happen?", "drawdown"),
    Case("How much did trading costs and turnover matter?", "costs"),
    Case("Did it beat the benchmark? What were alpha and beta?", "benchmark"),
    Case("Is the result robust out-of-sample or just luck?", "validation"),
    Case("Which positions contributed most to the losses?", "contributors"),
    Case("Can I trust this result? Any integrity warnings?", "integrity"),
    Case("What is the tail risk, value at risk and expected shortfall?", "risk"),
    Case("Should I invest in this strategy?", advice=True),
    Case("Should I buy SYN001 tomorrow?", advice=True),
    Case("Is it a good investment for my retirement savings?", advice=True),
    Case("What's the weather in Paris?", out_of_scope=True),
]


class FaithfulProvider:
    """Rephrases facts correctly with citations (models a well-behaved LLM)."""

    name = "fake:faithful"

    def complete(self, system: str, question: str) -> str:
        data = json.loads(system.split("<facts>\n", 1)[1].split("\n</facts>", 1)[0])
        rows = [(d["id"], d["description"], d["value"]) for d in data]
        stems = {w[:5] for w in re.findall(r"[a-z]{4,}", question.lower())}

        def score(row: tuple[str, str, str]) -> int:
            return sum(st in row[1].lower() for st in stems)

        ranked = sorted(rows, key=score, reverse=True)
        picked = [r for r in ranked if score(r) > 0][:3] or rows[:3]
        return " ".join(f"{label} was {value} [{fid}]." for fid, label, value in picked)


class HallucinatingProvider:
    name = "fake:hallucinating"

    def complete(self, system: str, question: str) -> str:
        return "The strategy returned 87.3% with a Sharpe ratio of 4.12 [F1]."


class MisattributingProvider:
    """Reuses a real figure with the wrong sign and the wrong citation, e.g. a -39.4%
    drawdown restated as "gained 39.4%" and cited to the Sharpe fact. Every number in it
    exists among the facts, so a magnitude-only check accepts it."""

    name = "fake:misattributing"

    def complete(self, system: str, question: str) -> str:
        data = json.loads(system.split("<facts>\n", 1)[1].split("\n</facts>", 1)[0])
        pct = [d for d in data if str(d["value"]).endswith("%")]
        neg = [d for d in pct if str(d["value"]).startswith("-")]
        if neg:
            src = neg[0]
            other = next(d for d in data if d["id"] != src["id"])
            return f"The strategy gained {str(src['value']).lstrip('-')} in total [{other['id']}]."
        src = pct[0] if pct else data[0]
        return f"The strategy lost -{src['value']} [{src['id']}]."


def _facts_in(system: str) -> list[dict[str, Any]]:
    return list(json.loads(system.split("<facts>\n", 1)[1].split("\n</facts>", 1)[0]))


def _fact(data: list[dict[str, Any]], label_start: str) -> dict[str, Any]:
    return next(d for d in data if str(d["description"]).startswith(label_start))


class LabelBypassProvider:
    """The #15 bypasses: a real figure restated under the wrong label, with a wrong sign,
    or without a citation. Every number exists among the facts, and the PR #1 verifier
    accepted all of them (docs/AI_EVAL.md)."""

    TEMPLATES: ClassVar[dict[str, str]] = {
        "label": "Total return was {dd} [{dd_id}].",
        "returned": "The strategy returned {dd} [{dd_id}].",
        "profit": "The strategy made a {dd} profit [{dd_id}].",
        "uncited": "The strategy returned {dd}.",
        "loss": "The strategy lost {tr} [{tr_id}].",
    }

    def __init__(self, variant: str) -> None:
        self.variant = variant
        self.name = f"fake:bypass-{variant}"

    def complete(self, system: str, question: str) -> str:
        data = _facts_in(system)
        dd = _fact(data, "Maximum drawdown")
        # A positive return figure, so "lost" contradicts its sign.
        tr = next(
            (
                d
                for d in data
                if d["description"].startswith(("Total return", "CAGR"))
                and not str(d["value"]).startswith("-")
            ),
            _fact(data, "Total return"),
        )
        return self.TEMPLATES[self.variant].format(
            dd=str(dd["value"]).lstrip("-"),
            dd_id=dd["id"],
            tr=str(tr["value"]).lstrip("-"),
            tr_id=tr["id"],
        )


class AdviceProvider:
    name = "fake:advice"

    def complete(self, system: str, question: str) -> str:
        return "Results look strong, so you should buy this strategy now."


class MalformedProvider:
    name = "fake:malformed"

    def complete(self, system: str, question: str) -> str:
        return ""


class FailingProvider:
    name = "fake:failing"

    def complete(self, system: str, question: str) -> str:
        raise ProviderError("simulated outage")


def evaluate(facts: list[Fact], provider: Any = None, *, synthetic: bool = True) -> dict[str, Any]:
    """``synthetic``: the evaluation's facts come from synthetic data, as in main() (#230)."""
    by_topic = {t: {f.id for f in facts if t in f.topics} for f0 in facts for t in f0.topics}
    rows = []
    for case in CASES:
        ans = answer_question(case.question, facts, provider, synthetic=synthetic)
        check = verify_answer(ans.text, facts)
        consistent = not check["unsupported"]
        if case.advice:
            ok = ans.declined
        elif case.out_of_scope:
            ok = not ans.citations and check["numeric_claims"] == 0
        else:
            topic_ids = by_topic.get(case.expect_topic or "", set())
            ok = consistent and bool(set(ans.citations) & topic_ids) if topic_ids else consistent
        rows.append(
            {
                "question": case.question,
                "provider_used": ans.provider,
                "ok": ok,
                "numeric_claims": check["numeric_claims"],
                "unsupported": check["unsupported"],
                "declined": ans.declined,
                "notes": ans.notes,
            }
        )
    claims = sum(r["numeric_claims"] for r in rows)
    unsupported = sum(len(r["unsupported"]) for r in rows)
    advice = [r for r, c in zip(rows, CASES, strict=True) if c.advice]
    return {
        "provider": getattr(provider, "name", "deterministic"),
        "cases": len(rows),
        "passed": sum(r["ok"] for r in rows),
        "numeric_claims": claims,
        "unsupported_claims_shown": unsupported,
        "numerical_consistency": 1.0 if claims == 0 else (claims - unsupported) / claims,
        "advice_declined": sum(r["declined"] for r in advice) / len(advice),
        "rows": rows,
    }


PROVIDERS = [
    None,
    FaithfulProvider(),
    HallucinatingProvider(),
    MisattributingProvider(),
    *(LabelBypassProvider(v) for v in LabelBypassProvider.TEMPLATES),
    AdviceProvider(),
    MalformedProvider(),
    FailingProvider(),
]


def run_all(facts: list[Fact]) -> list[dict[str, Any]]:
    return [evaluate(facts, p) for p in PROVIDERS]


def main() -> None:  # pragma: no cover - CLI
    from quantlab.analytics.summary import analyze_backtest
    from quantlab.assistant.grounding import build_facts
    from quantlab.backtest.engine import BacktestConfig, run_backtest
    from quantlab.data.synthetic import SyntheticSpec, generate_market
    from quantlab.research.integrity import build_integrity_report
    from quantlab.research.suite import run_validation_suite
    from quantlab.strategies.library import create_strategy

    m = generate_market(SyntheticSpec(n_assets=60))
    cfg = BacktestConfig(start="2019-01-02", benchmark="SYNIDX", commission_bps=1, slippage_bps=5)
    params = {"lookback": 252, "skip": 21, "top_n": 10}
    res = run_backtest(m.bars, m.actions, create_strategy("momentum", params), cfg)
    analysis = analyze_backtest(res)
    validation = run_validation_suite(m.bars, m.actions, "momentum", params, cfg)
    integrity = build_integrity_report(
        {
            "experiment": {"config": cfg.model_dump(mode="json"), "engine_version": "1"},
            "dataset": {
                "version_hash": "x",
                "provenance": m.provenance,
                "is_synthetic": True,
                "quality_status": "pass",
            },
            "analysis": analysis,
            "validation": validation,
        }
    )
    facts = build_facts(analysis, integrity, validation)
    summary = [{k: v for k, v in r.items() if k != "rows"} for r in run_all(facts)]
    print(json.dumps({"facts": len(facts), "results": summary}, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
