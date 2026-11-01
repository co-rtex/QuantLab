"""Child process for the reproducibility tests (#182). Synthetic data only.

    python -m tests.unit.repro_child [unseeded]

Runs the library chain behind an experiment (backtest, analysis, validation suite, ML run,
integrity report, Markdown and HTML report) on a fixed synthetic market and prints one
JSON object of SHA-256 digests, one per output. The parent runs it in fresh interpreters
with different ``PYTHONHASHSEED`` values and thread counts and compares the digests.

The report's ``generated_at`` is the one documented timestamp; it is pinned before
rendering. ``unseeded`` injects nondeterminism (the ML models without a random state), so
the parent can show the comparison catches it.
"""

from __future__ import annotations

import hashlib
import json
import sys
from typing import Any

import pandas as pd

GENERATED_AT = "2000-01-01 00:00 UTC"


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _frame(df: pd.DataFrame) -> str:
    return _h(df.to_csv(float_format="%.17g"))


def _json(obj: Any) -> str:
    from quantlab.analytics.summary import jsonable

    return _h(json.dumps(jsonable(obj), sort_keys=True))


def _equity_series(eq: pd.DataFrame) -> dict[str, Any]:
    # The fields the report charts, as the API's equity endpoint rounds them.
    eq = eq.reset_index()
    out: dict[str, Any] = {"dates": [d.strftime("%Y-%m-%d") for d in eq["date"]]}
    out["equity"] = [round(float(v), 6) for v in eq["equity"]]
    out["drawdown"] = [round(float(v), 6) for v in eq["equity"] / eq["equity"].cummax() - 1]
    return out


def main(inject: str | None) -> dict[str, str]:
    from quantlab.analytics.summary import analyze_backtest
    from quantlab.backtest.engine import ENGINE_VERSION, BacktestConfig, run_backtest
    from quantlab.data.synthetic import BENCHMARK_SYMBOL, SyntheticSpec, generate_market
    from quantlab.ml import pipeline
    from quantlab.reports.render import render_html, render_markdown, report_context
    from quantlab.research.integrity import build_integrity_report
    from quantlab.research.suite import run_validation_suite
    from quantlab.strategies.library import create_strategy

    if inject == "unseeded":
        original = pipeline._model
        pipeline._model = lambda name, seed: original(name, None)  # type: ignore[assignment]

    m = generate_market(SyntheticSpec(n_assets=12, start="2019-01-02", end="2020-12-31", seed=3))
    params = {"lookback": 63, "skip": 5, "top_n": 5}
    cfg = BacktestConfig(
        start="2019-07-01", benchmark=BENCHMARK_SYMBOL, rebalance="weekly", slippage_bps=5
    )
    res = run_backtest(
        m.bars, m.actions, create_strategy("momentum", params), cfg, instruments=m.master
    )
    analysis = analyze_backtest(res)
    validation = run_validation_suite(
        m.bars, m.actions, "momentum", params, cfg, instruments=m.master, seed=7
    )
    ml = pipeline.run_ml_experiment(
        m.bars,
        m.actions,
        pipeline.MLConfig(folds=2, min_train_dates=6, sample_every=10, seed=11),
        instruments=m.master,
    )
    predictions = ml.pop("predictions")
    experiment = {
        "id": "repro",
        "config": cfg.model_dump(mode="json"),
        "strategy": "momentum",
        "params": params,
        "engine_version": ENGINE_VERSION,
        "code_version": "repro",
        "input_hash": "repro",
        "fingerprint": res.fingerprint(),
    }
    dataset = {
        "version_hash": "repro",
        "provenance": m.provenance,
        "is_synthetic": True,
        "quality_status": "pass",
        "delisted_symbols": [],
        "delisted_instrument_ids": [],
    }
    evidence = {
        "experiment": experiment,
        "dataset": dataset,
        "analysis": analysis,
        "validation": validation,
        "reproductions": [],
        "trials_on_dataset": 1,
    }
    integrity = build_integrity_report(evidence)
    ctx = report_context(
        experiment={
            **experiment,
            "name": "repro",
            "strategy_key": "momentum",
            "strategy_params": params,
        },
        dataset=dataset,
        analysis=analysis,
        equity=_equity_series(res.equity),
        integrity=integrity,
        validation=validation,
    )
    ctx["generated_at"] = GENERATED_AT  # the documented timestamp, pinned
    return {
        "fingerprint": res.fingerprint(),
        "equity": _frame(res.equity),
        "trades": _frame(res.trades),
        "positions": _frame(res.positions),
        "analysis": _json(analysis),
        "validation": _json(validation),
        "ml": _json(ml),
        "ml_predictions": _frame(pd.DataFrame(predictions)),
        "integrity": _json(integrity),
        "report_markdown": _h(render_markdown(ctx)),
        "report_html": _h(render_html(ctx)),
    }


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1] if len(sys.argv) > 1 else None), sort_keys=True))
