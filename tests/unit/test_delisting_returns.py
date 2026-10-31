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
    assert res.meta["delistings"] == {
        "with_return": 0,
        "with_fallback_return": 0,
        "return_unknown": 0,
        "by_missing_bars": 1,
        "after_window": 0,
    }


def test_master_counts_are_reported():
    bars, d, cfg = _case()
    res = run_backtest(
        bars, NONE, create_strategy("equal_weight"), cfg, instruments=_master(d, -0.3)
    )
    assert res.meta["delistings"] == {
        "with_return": 1,
        "with_fallback_return": 0,
        "return_unknown": 0,
        "by_missing_bars": 0,
        "after_window": 0,
    }


def test_decisions_before_the_delisting_do_not_depend_on_the_master():
    bars, d, cfg = _case()
    daily = cfg.model_copy(update={"rebalance": "daily"})
    a = run_backtest(bars, NONE, create_strategy("equal_weight"), daily)
    b = run_backtest(
        bars, NONE, create_strategy("equal_weight"), daily, instruments=_master(d, -0.9)
    )
    # Strictly before the delisting date nothing differs. On the date itself the delisting
    # is public, so the security is no longer offered to the strategy.
    wa, wb = (r.weights_target[r.weights_target["date"] < d[4]] for r in (a, b))
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
    # From its delisting date it is not offered to the strategy, so it is never targeted.
    late = res.weights_target[res.weights_target["date"] >= d[4]]
    assert "B" not in set(late["symbol"])


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


# ----------------------------------------------- independent review of b3809b4
def test_a_halt_before_the_delisting_keeps_the_delisting_return():
    # B stops printing after d[4] (a halt) and is delisted at d[10] with -90%. The
    # missing-bars rule must not liquidate it at its last close first.
    bars, d, cfg = _case()
    master = _master(d, -0.9).assign(
        delisting_date=lambda m: m["delisting_date"].where(m["instrument_id"] != "B", d[10])
    )
    res = run_backtest(bars, NONE, create_strategy("equal_weight"), cfg, instruments=master)
    assert res.equity["equity"].iloc[-1] == pytest.approx(500 + 50 * 5.0 * 0.1)
    liq = res.trades[res.trades["note"].fillna("").str.startswith("delisting")].iloc[0]
    assert liq["date"] == d[11]
    assert res.meta["delistings"] == {
        "with_return": 1,
        "with_fallback_return": 0,
        "return_unknown": 0,
        "by_missing_bars": 0,
        "after_window": 0,
    }


def test_a_delisted_security_is_not_offered_to_the_strategy():
    # B delists at d[3] (return 0) but keeps printing bars; equal weight must spread over
    # A and C only from then on, with no orders wasted on B.
    bars = make_bars({"A": [10.0] * 10, "B": [10.0] * 10, "C": [10.0] * 10})
    d = pd.DatetimeIndex(sorted(bars["date"].unique()))
    master = normalize_master(
        pd.DataFrame([{"instrument_id": "B", "delisting_date": d[3], "delisting_return": 0.0}])
    )
    cfg = BacktestConfig(rebalance="daily", initial_capital=900, **FREE)
    res = run_backtest(bars, NONE, create_strategy("equal_weight"), cfg, instruments=master)
    late = res.weights_target[res.weights_target["date"] >= d[3]]
    assert "B" not in set(late["symbol"])
    assert res.equity["cash"].iloc[-1] == pytest.approx(0.0, abs=1e-6)  # fully reinvested
    assert (res.rejections["reason"] != "delisted").all()


def test_distributions_after_the_delisting_date_are_in_the_delisting_return():
    bars = make_bars({"A": [10.0] * 8, "B": [10.0] * 8})
    d = pd.DatetimeIndex(sorted(bars["date"].unique()))
    acts = normalize_actions(
        pd.DataFrame([{"symbol": "B", "ex_date": d[5], "kind": "dividend", "value": 1.0}])
    )
    master = normalize_master(
        pd.DataFrame([{"instrument_id": "B", "delisting_date": d[3], "delisting_return": -1.0}])
    )
    cfg = BacktestConfig(rebalance="quarterly", initial_capital=1000, **FREE)
    res = run_backtest(bars, acts, create_strategy("equal_weight"), cfg, instruments=master)
    # Hand: 50 shares of B, written off at d[4]; the dividend dated after is not paid.
    assert res.equity["equity"].iloc[-1] == pytest.approx(500)
    assert res.equity["cum_dividends"].iloc[-1] == 0


def test_master_is_normalized_and_unmatched_ids_are_reported():
    bars, d, cfg = _case()
    lower = _master(d, -0.3).assign(instrument_id=lambda m: m["instrument_id"].str.lower())
    extra = pd.concat(
        [lower, pd.DataFrame([{"instrument_id": "nobody", "delisting_date": d[2]}])],
        ignore_index=True,
    )
    res = run_backtest(bars, NONE, create_strategy("equal_weight"), cfg, instruments=extra)
    assert res.meta["delistings"]["with_return"] == 1  # "b" matched B
    assert res.meta["instrument_master_unmatched"] == 1
    from quantlab.data.identity import IdentityError

    with pytest.raises(IdentityError):  # duplicate ids are rejected, not crashed on
        run_backtest(
            bars,
            NONE,
            create_strategy("equal_weight"),
            cfg,
            instruments=pd.concat([lower, lower], ignore_index=True),
        )


def test_ml_label_books_the_return_only_once_it_is_public():
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
    sessions = pd.DatetimeIndex(sorted(m.bars["date"].unique()))
    master = m.master
    sym_of = m.instruments.set_index("instrument_id")["symbol"]
    gone = master[master["delisting_date"].notna()].copy()
    gone["instrument_id"] = gone["instrument_id"].map(sym_of)
    # Delisted 60 sessions after the last bar (a long halt), with a -50% return.
    gone["delisting_date"] = gone["delisting_date"].map(
        lambda t: sessions[min(sessions.get_loc(t) + 60, len(sessions) - 1)]
    )
    gone["delisting_return"] = -0.5
    cfg = MLConfig(horizon=21, sample_every=5)
    base = build_panel(m.bars, m.actions, cfg)
    with_r = build_panel(m.bars, m.actions, cfg, instruments=gone)
    merged = base.merge(with_r, on=["date", "symbol"], suffixes=("_0", "_r"))
    changed = ~np.isclose(merged["fwd_return_0"], merged["fwd_return_r"])
    when = merged["symbol"].map(gone.set_index("instrument_id")["delisting_date"])
    # Every label that includes the return becomes available after the delisting date.
    assert (merged.loc[changed, "label_available_at_r"] > when[changed]).all()


# --------------------------------------------------------- follow-ups from #49 (#59)
def test_a_halt_that_ends_in_delisting_is_held_not_sold():
    """CRSP convention: a halted security that the master later delists is held until the
    delisting (a halted stock cannot be sold). The no-master run instead liquidates it at
    the stale last close after `delist_after_missing` sessions, so positions and cash
    differ during the halt; decisions before the halt begins are identical."""
    bars, d, cfg = _case()  # B's last bar is d[4]
    master = _master(d, -0.9).assign(
        delisting_date=lambda m: m["delisting_date"].where(m["instrument_id"] != "B", d[10])
    )
    held = run_backtest(bars, NONE, create_strategy("equal_weight"), cfg, instruments=master)
    sold = run_backtest(bars, NONE, create_strategy("equal_weight"), cfg)
    pos = lambda r, day: r.positions[(r.positions["date"] == day) & (r.positions["symbol"] == "B")]  # noqa: E731
    assert len(pos(sold, d[7])) == 0  # sold at the stale last close after 3 missing sessions
    assert pos(held, d[7])["quantity"].iloc[0] == pytest.approx(50)  # still held in the halt
    assert held.equity["cash"].loc[d[7]] < sold.equity["cash"].loc[d[7]]
    early = [r.weights_target[r.weights_target["date"] <= d[4]] for r in (held, sold)]
    pd.testing.assert_frame_equal(early[0].reset_index(drop=True), early[1].reset_index(drop=True))


def test_a_delisting_after_the_window_is_counted_not_silent():
    bars, d, cfg = _case()
    master = _master(d, -0.5).assign(
        delisting_date=lambda m: m["delisting_date"].where(
            m["instrument_id"] != "B", d[-1] + pd.Timedelta(days=30)
        )
    )
    long_halt = cfg.model_copy(update={"delist_after_missing": 63})
    res = run_backtest(bars, NONE, create_strategy("equal_weight"), long_halt, instruments=master)
    assert res.meta["delistings"]["after_window"] == 1
    assert res.equity["equity"].iloc[-1] == pytest.approx(500 + 50 * 5.0)  # frozen, unapplied


def test_malformed_delisting_returns_raise():
    from quantlab.data.identity import IdentityError

    with pytest.raises(IdentityError, match="not a number"):
        normalize_master(
            pd.DataFrame(
                [{"instrument_id": "B", "delisting_date": "2024-01-08", "delisting_return": "-30%"}]
            )
        )


@pytest.mark.parametrize(
    ("reason", "proceeds", "count"),
    [
        ("bankruptcy", 50 * 5.0 * 0.7, "with_fallback_return"),  # assumed -30%
        ("merger", 50 * 5.0, "return_unknown"),  # not a performance delisting: unchanged
    ],
)
def test_performance_delisting_fallback(reason, proceeds, count):
    bars, d, cfg = _case()
    master = _master(d, np.nan).assign(
        delisting_reason=lambda m: m["delisting_reason"].where(m["instrument_id"] != "B", reason)
    )
    cfg = cfg.model_copy(update={"performance_delisting_return": -0.3})
    res = run_backtest(bars, NONE, create_strategy("equal_weight"), cfg, instruments=master)
    assert res.equity["equity"].iloc[-1] == pytest.approx(500 + proceeds)
    assert res.meta["delistings"][count] == 1
    if reason == "bankruptcy":
        note = res.trades[res.trades["note"].fillna("").str.startswith("delisting")].iloc[0]["note"]
        assert "assumed" in note


def test_integrity_report_surfaces_how_delistings_were_priced():
    from quantlab.research.integrity import build_integrity_report

    def check(delistings):
        ev = {
            "experiment": {"config": {"commission_bps": 1, "slippage_bps": 5}},
            "dataset": {"provenance": {"provider": "csv"}, "quality_status": "pass"},
            "analysis": {"meta": {"universe": ["A"], "delistings": delistings}},
        }
        rep = build_integrity_report(ev)
        return {c["key"]: c for c in rep["checks"]}.get("delisting_exits")

    base = {
        "with_return": 0,
        "with_fallback_return": 0,
        "return_unknown": 0,
        "by_missing_bars": 0,
        "after_window": 0,
    }
    assert check({**base, "return_unknown": 2})["status"] == "warning"
    assert check({**base, "by_missing_bars": 1})["status"] == "warning"
    assert "after the backtest window" in check({**base, "after_window": 1})["finding"]
    assert check({**base, "with_return": 3})["status"] == "verified"
    assert "assumed" in check({**base, "with_fallback_return": 1})["finding"]
    assert check(base)["status"] == "verified"
    # Analyses written before delisting counts existed get no such check.
    ev = {"experiment": {"config": {}}, "dataset": {}, "analysis": {"meta": {"universe": ["A"]}}}
    assert "delisting_exits" not in {c["key"] for c in build_integrity_report(ev)["checks"]}
