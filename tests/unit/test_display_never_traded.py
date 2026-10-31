"""Results meta names never-traded symbols by ticker, like the universe (#150 review NB2).

Synthetic data only.
"""

from __future__ import annotations

import pandas as pd

from quantlab.backtest.engine import BacktestConfig, run_backtest
from quantlab.data.schema import normalize_actions
from quantlab.strategies.library import create_strategy
from tests.conftest import make_bars


def test_never_traded_instruments_are_shown_as_tickers():
    from quantlab_api.tasks import ExperimentInputs, _for_display

    # A keyed dataset: instruments QLA and QLZ, shown as A and Z. Z printed volume 0 only.
    bars = make_bars({"QLA": [10.0] * 6, "QLZ": [10.0] * 6})
    bars.loc[bars["symbol"] == "QLZ", "volume"] = 0.0
    first = bars["date"].min()
    history = pd.DataFrame(
        [
            {"instrument_id": i, "symbol": t, "valid_from": first, "valid_to": pd.NaT}
            for i, t in (("QLA", "A"), ("QLZ", "Z"))
        ]
    )
    cfg = BacktestConfig(rebalance="monthly", initial_capital=1000)
    actions = normalize_actions(None)
    res = run_backtest(bars, actions, create_strategy("equal_weight"), cfg)
    assert res.meta["never_traded"] == ["QLZ"]
    inputs = ExperimentInputs(bars, actions, cfg, history, master=None)
    _, shown = _for_display(res, {"meta": res.meta}, inputs)
    assert shown["meta"]["never_traded"] == ["Z"]
    assert shown["meta"]["universe"] == ["A", "Z"]
