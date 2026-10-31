"""Delisting returns from the instrument master (issue #9, part B).

Hand-computed references. A delisting return r is the return from the last close to the
final value of the security (a cash distribution, or the first post-delisting quote), so
a holder of q shares ends with q x last_close x (1 + r). Synthetic data only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quantlab.backtest.engine import BacktestConfig, run_backtest
from quantlab.data.identity import normalize_master
from quantlab.data.schema import normalize_actions
from quantlab.portfolio.ledger import Ledger, LedgerError, replay
from quantlab.strategies.library import create_strategy
from tests.conftest import make_bars

FREE = dict(commission_bps=0, slippage_bps=0, fractional_shares=True)
NONE = normalize_actions(None)


def _case():
    a = [10.0] * 12
    b = [10.0, 10.0, 8.0, 6.0, 5.0, None, None, None, None, None, None, None]
    bars = make_bars({"A": a, "B": b})
    d = pd.DatetimeIndex(sorted(bars["date"].unique()))
    cfg = BacktestConfig(
        rebalance="quarterly", initial_capital=1000, delist_after_missing=3, **FREE
    )
    return bars, d, cfg


def _master(d, ret):
    return normalize_master(
        pd.DataFrame(
            [
                {"instrument_id": "A", "name": "A", "listing_date": d[0]},
                {
                    "instrument_id": "B",
                    "name": "B",
                    "listing_date": d[0],
                    "delisting_date": d[4],
                    "delisting_reason": "bankruptcy",
                    "delisting_return": ret,
                },
            ]
        )
    )


@pytest.mark.parametrize(
    ("ret", "proceeds", "note"),
    [
        (-0.3, 50 * 5.0 * 0.7, "delisting return applied"),
        (-1.0, 0.0, "delisting return applied"),  # a worthless bankruptcy
        (np.nan, 50 * 5.0, "delisting return unknown"),
    ],
)
def test_delisting_return_is_applied_on_the_session_after_delisting(ret, proceeds, note):
    bars, d, cfg = _case()
    res = run_backtest(
        bars, NONE, create_strategy("equal_weight"), cfg, instruments=_master(d, ret)
    )
    # Hand: $500 in each at the 2nd open (10): 50 shares of B. B's last close is 5 (d[4]).
    assert res.equity["equity"].iloc[-1] == pytest.approx(500 + proceeds)
    liq = res.trades[res.trades["note"].fillna("").str.startswith("delisting")]
    assert len(liq) == 1 and liq.iloc[0]["symbol"] == "B"
    assert liq.iloc[0]["date"] == d[5]  # the first session after the delisting date
    assert note in liq.iloc[0]["note"]
    res.ledger.check_invariants()
    kinds = [t.kind for t in res.ledger.transactions if t.symbol == "B"]
    assert kinds[-1] == "delisting"


def test_without_a_master_the_last_close_rule_is_unchanged():
    bars, d, cfg = _case()
    res = run_backtest(bars, NONE, create_strategy("equal_weight"), cfg)
    liq = res.trades[res.trades["note"] == "delisting"].iloc[0]
    assert liq["date"] == d[7] and liq["price"] == 5.0  # after 3 missing sessions
    assert res.equity["equity"].iloc[-1] == pytest.approx(750)
    assert res.meta["delistings"] == {"with_return": 0, "return_unknown": 0, "by_missing_bars": 1}


def test_master_counts_are_reported():
    bars, d, cfg = _case()
    res = run_backtest(
        bars, NONE, create_strategy("equal_weight"), cfg, instruments=_master(d, -0.3)
    )
    assert res.meta["delistings"] == {"with_return": 1, "return_unknown": 0, "by_missing_bars": 0}


def test_decisions_before_the_delisting_do_not_depend_on_the_master():
    bars, d, cfg = _case()
    daily = cfg.model_copy(update={"rebalance": "daily"})
    a = run_backtest(bars, NONE, create_strategy("equal_weight"), daily)
    b = run_backtest(
        bars, NONE, create_strategy("equal_weight"), daily, instruments=_master(d, -0.9)
    )
    wa, wb = (r.weights_target[r.weights_target["date"] <= d[4]] for r in (a, b))
    pd.testing.assert_frame_equal(wa.reset_index(drop=True), wb.reset_index(drop=True))
    assert (a.equity["equity"][: d[4]] == b.equity["equity"][: d[4]]).all()


def test_a_delisted_security_is_never_bought_again():
    # The data keeps printing bars after the master's delisting date (an OTC tail, say).
    bars = make_bars({"A": [10.0] * 10, "B": [10.0] * 10})
    d = pd.DatetimeIndex(sorted(bars["date"].unique()))
    master = _master(d, -0.5)
    cfg = BacktestConfig(rebalance="daily", initial_capital=1000, **FREE)
    res = run_backtest(bars, NONE, create_strategy("equal_weight"), cfg, instruments=master)
    buys = res.trades[(res.trades["symbol"] == "B") & (res.trades["side"] == "buy")]
    assert (buys["date"] <= d[4]).all()
    assert (res.rejections.loc[res.rejections["symbol"] == "B", "reason"] == "delisted").any()


def test_ledger_delisting_transaction_replays_and_is_verified():
    led = Ledger()
    t0 = pd.Timestamp("2024-01-02")
    led.deposit(t0, 1000)
    led.trade(t0, "B", 10, 50.0)
    led.delist(t0, "B", 0.0)
    assert led.quantity("B") == 0 and led.cash == pytest.approx(500)
    led.check_invariants()
    tampered = list(led.transactions)
    bad = tampered[-1].__class__(**{**tampered[-1].__dict__, "cash_delta": 100.0})
    with pytest.raises(LedgerError):
        replay([*tampered[:-1], bad])
    with pytest.raises(LedgerError):
        led.delist(t0, "B", 1.0)  # nothing held any more


def test_ml_labels_include_the_delisting_return():
    from quantlab.data.synthetic import SyntheticSpec, generate_market
    from quantlab.ml.pipeline import MLConfig, build_panel

    m = generate_market(
        SyntheticSpec(
            n_assets=16,
            start="2019-01-02",
            end="2021-12-31",
            seed=2,
            delisting_fraction=0.4,
            late_listing_fraction=0.0,
        )
    )
    master = m.master
    gone = master[master["delisting_date"].notna()].copy()
    assert len(gone)
    sym_of = m.instruments.set_index("instrument_id")["symbol"]
    # The synthetic master is keyed by QLSYN ids; this panel is keyed by ticker.
    gone["instrument_id"] = gone["instrument_id"].map(sym_of)
    gone["delisting_return"] = -0.5
    cfg = MLConfig(horizon=21, sample_every=5)
    base = build_panel(m.bars, m.actions, cfg)
    with_r = build_panel(m.bars, m.actions, cfg, instruments=gone)
    merged = base.merge(with_r, on=["date", "symbol"], suffixes=("_0", "_r"))
    last = m.bars.groupby("symbol")["date"].max()
    sessions = pd.DatetimeIndex(sorted(m.bars["date"].unique()))
    pos = {s: sessions.get_loc(t) for s, t in last.items()}
    k = merged["date"].map(sessions.get_loc)
    hit = merged["symbol"].isin(gone["instrument_id"]) & (
        k + cfg.horizon > merged["symbol"].map(pos)
    )
    assert hit.any()
    np.testing.assert_allclose(
        1 + merged.loc[hit, "fwd_return_r"], (1 + merged.loc[hit, "fwd_return_0"]) * 0.5
    )
    np.testing.assert_allclose(merged.loc[~hit, "fwd_return_r"], merged.loc[~hit, "fwd_return_0"])


def test_master_is_stored_hashed_and_verified(tmp_path):
    from quantlab.data.store import DatasetStore, content_hash

    bars, d, _ = _case()
    store = DatasetStore(tmp_path)
    plain = store.write("plain", bars, NONE, {"provider": "test"})
    assert store.load_master(plain) is None
    assert plain.version == content_hash(bars, NONE)  # unchanged without a master
    dv = store.write("with-master", bars, NONE, {"provider": "test"}, master=_master(d, -0.3))
    assert dv.version != plain.version and dv.manifest["instrument_master"] is True
    pd.testing.assert_frame_equal(store.load_master(dv), _master(d, -0.3))
    assert store.verify(dv)
    other = store.write("other-return", bars, NONE, {"provider": "test"}, master=_master(d, -0.6))
    assert other.version != dv.version  # a different delisting return is different data
