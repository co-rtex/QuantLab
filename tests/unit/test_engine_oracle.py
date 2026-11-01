"""The engine against an independent reference backtester (#180). Synthetic data only.

``tests/oracle/reference.py`` is written from the documented rules and imports nothing
from ``quantlab``. The hand-computed cases pin both implementations to arithmetic done by
hand. The differential tests then compare them on generated adversarial universes
(``tests/oracle/universes.py``): the equity record, trades, rejections, positions,
decisions, the final ledger's cash and positions, and a list of meta counts, to 1e-9
relative (see ``tests/oracle/differential.py``).

Independence is not complete: the reference's fills, pro-rata and affordability follow
the engine's code closely, so a defect shared there would not show (#190 review). #195
rewrites them from METHODOLOGY's text.
"""

from __future__ import annotations

import os

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.oracle import strategies as S
from tests.oracle.differential import STRATEGIES, compare, diff, run_engine
from tests.oracle.reference import run_reference
from tests.oracle.universes import Scenario

# 50 seeds by default (CI runs the suite under coverage, which slows the engine about 3x);
# QUANTLAB_ORACLE_SEEDS=200, or QUANTLAB_ORACLE_SLOW=1 for the rest, runs the full set.
SEEDS = range(int(os.environ.get("QUANTLAB_ORACLE_SEEDS", "50")))
ALL_SEEDS = 200
DAYS = pd.bdate_range("2024-01-02", periods=6)


def _scenario(bars: list[tuple], actions: list[tuple], cfg: dict) -> Scenario:
    frame = pd.DataFrame(bars, columns=["symbol", "date", "open", "close", "volume"])
    frame["high"] = frame[["open", "close"]].max(axis=1)
    frame["low"] = frame[["open", "close"]].min(axis=1)
    tickers = sorted(frame["symbol"].unique())
    history = pd.DataFrame(
        {"instrument_id": tickers, "symbol": tickers, "valid_from": pd.Timestamp("2023-01-01")}
    ).assign(valid_to=pd.NaT)
    base = {
        "start": None,
        "end": None,
        "initial_capital": 1000.0,
        "rebalance": "daily",
        "execution": "next_open",
        "commission_bps": 0.0,
        "commission_min": 0.0,
        "slippage_bps": 0.0,
        "fractional_shares": False,
        "max_weight": 1.0,
        "max_gross_exposure": 1.0,
        "min_trade_value": 0.0,
        "delist_after_missing": 5,
        "performance_delisting_return": None,
    }
    acts = pd.DataFrame(actions, columns=["symbol", "ex_date", "kind", "value"])
    return Scenario(-1, frame, acts, history, None, {**base, **cfg})


def _both(sc: Scenario, engine_strategy, ref_strategy):
    eng = run_engine(sc, sc.cfg, engine_strategy)
    ref = run_reference(sc.bars, sc.actions, sc.history, sc.master, sc.cfg, ref_strategy)
    return eng, ref


# A held position through a same-day dividend and 2-for-1 split: buy at 11 with 1000 cash.
SPLIT_BARS = [
    ("AAA", DAYS[0], 10.0, 10.0, 1e4),
    ("AAA", DAYS[1], 11.0, 12.0, 1e4),
    ("AAA", DAYS[2], 6.0, 6.5, 1e4),
    ("AAA", DAYS[3], 7.0, 7.0, 1e4),
]
SPLIT_ACTIONS = [("AAA", DAYS[2], "dividend", 1.0), ("AAA", DAYS[2], "split", 2.0)]


def test_hand_computed_split_and_dividend_next_open():
    from quantlab.strategies.library import create_strategy

    sc = _scenario(SPLIT_BARS, SPLIT_ACTIONS, {})
    eng, ref = _both(sc, create_strategy("buy_and_hold"), S.BuyAndHoldRef())
    # Day 1: 1000 / 10 = 100 shares wanted. Day 2: they cost 1100 at the open, so the buy is
    # scaled to 1000/1100 x 100 = 90.9 and floored to 90 (990 spent, 10 left).
    # Day 2 mark 90 x 12 + 10 = 1090. Day 3 open: dividend 90 x 1 (on pre-split shares),
    # then 180 shares; mark 180 x 6.5 + 100 = 1270. Day 4: 180 x 7 + 100 = 1360.
    expected = [1000.0, 1090.0, 1270.0, 1360.0]
    assert [r["equity"] for r in ref.equity] == pytest.approx(expected, rel=1e-12)
    assert eng.equity["equity"].tolist() == pytest.approx(expected, rel=1e-12)
    assert ref.trades[0]["quantity"] == 90 and ref.trades[0]["partial"]
    assert eng.ledger.positions == {"AAA": 180.0} and ref.meta["final_positions"] == {"AAA": 180.0}
    assert diff(eng, ref, 1000.0) == []


def test_hand_computed_costs_next_close():
    from quantlab.strategies.library import create_strategy

    sc = _scenario(
        SPLIT_BARS,
        SPLIT_ACTIONS,
        {"execution": "next_close", "slippage_bps": 10.0, "commission_bps": 10.0},
    )
    eng, ref = _both(sc, create_strategy("buy_and_hold"), S.BuyAndHoldRef())
    # Fill at the day-2 close 12 x 1.001 = 12.012. 100 shares need 100 x 12 x 1.001 x 1.001
    # = 1202.4012 > 1000, so they are scaled to 83.17 and floored to 83: notional 996.996,
    # commission 0.996996, cash 2.007004. Marks: 83 x 12 = 996; after the dividend (83) and
    # split (166 shares) 166 x 6.5 = 1079 and 166 x 7 = 1162, plus 85.007004 cash.
    expected = [1000.0, 998.007004, 1164.007004, 1247.007004]
    assert [r["equity"] for r in ref.equity] == pytest.approx(expected, rel=1e-12)
    assert eng.equity["equity"].tolist() == pytest.approx(expected, rel=1e-12)
    t = eng.trades.iloc[0]
    assert (t["quantity"], t["price"]) == (83, pytest.approx(12.012, rel=1e-15))
    assert t["commission"] == pytest.approx(0.996996, rel=1e-12)
    assert t["slippage_cost"] == pytest.approx(0.996, rel=1e-12)
    assert diff(eng, ref, 1000.0) == []


def test_hand_computed_zero_volume_suspension_and_missing_bars_exit():
    from quantlab.strategies.library import create_strategy

    x = [("XXX", d, 10.0, 10.0, 1e4) for d in DAYS]
    y = [
        ("YYY", DAYS[0], 20.0, 20.0, 1e4),
        ("YYY", DAYS[1], 20.0, 20.0, 0.0),  # a stale repeat with zero volume
        ("YYY", DAYS[2], 21.0, 22.0, 1e4),  # then no bars: suspended, never back
    ]
    sc = _scenario(
        x + y,
        [],
        {"initial_capital": 10_000.0, "fractional_shares": True, "delist_after_missing": 2},
    )
    eng, ref = _both(sc, create_strategy("equal_weight"), S.EqualWeightRef())
    # Day 1: 500 X and 250 Y wanted. Day 2: X fills at 10 (5000); Y's bar has zero volume,
    # so its buy is rejected. Day 3: Y's 250 at 21 need 5250 > 5000 cash, so the buy is
    # scaled to 238.095238 (cash 0); mark 5000 + 238.095238 x 22 = 10238.095238.
    # Day 4: Y has no bar: its sell is rejected (no price) and X's top-up finds no cash.
    # Day 5: Y's second missing session: sold at its last close 22 (5238.095238), then
    # X's buy of 523.809524 at 10 spends it. Equity stays 10238.095238.
    q = 250 * 5000 / 5250
    after = 5000 + q * 22
    expected = [10_000.0, 10_000.0, after, after, after, after]
    assert [r["equity"] for r in ref.equity] == pytest.approx(expected, rel=1e-12)
    assert eng.equity["equity"].tolist() == pytest.approx(expected, rel=1e-12)
    reasons = [(r["date"], r["symbol"], r["reason"]) for r in ref.rejections]
    assert reasons == [
        (DAYS[1], "YYY", "no_volume"),
        (DAYS[3], "YYY", "no_price"),
        (DAYS[3], "XXX", "insufficient_cash"),
        (DAYS[4], "YYY", "no_price"),
    ]
    exit_ = eng.trades[eng.trades["note"].notna()].iloc[0]
    assert (exit_["date"], exit_["symbol"], exit_["price"]) == (DAYS[4], "YYY", 22.0)
    assert exit_["quantity"] == pytest.approx(-q, rel=1e-12)
    assert eng.meta["delistings"]["by_missing_bars"] == 1
    assert eng.meta["stale_marks"]["position_days"] == 1  # Y on day 4
    assert diff(eng, ref, 10_000.0) == []


def test_cash_dust_leaves_a_shortfall_recorded_as_insufficient_cash():
    # #184 F1. Day 2: both buys of 166.67 shares need 1023.33 at 3.07 and are scaled to
    # 162.866450, leaving cash +1.1369e-13 (rounding dust). Day 3: A's sell is rejected on
    # its zero-volume bar, so B's buy of 0.5 x 977.198697 / 2.5 - 162.866450 = 32.573290
    # meets only the dust. It used to be scaled to 4.5e-14 shares and logged InvalidOrder.
    from quantlab.strategies.library import create_strategy

    bars = [
        ("AAA", DAYS[0], 3.0, 3.0, 1e4),
        ("AAA", DAYS[1], 3.07, 3.5, 1e4),
        ("AAA", DAYS[2], 3.5, 3.5, 0.0),
        ("AAA", DAYS[3], 3.6, 3.6, 1e4),
        ("BBB", DAYS[0], 3.0, 3.0, 1e4),
        ("BBB", DAYS[1], 3.07, 2.5, 1e4),
        ("BBB", DAYS[2], 2.5, 2.6, 1e4),
        ("BBB", DAYS[3], 2.6, 2.6, 1e4),
    ]
    sc = _scenario(bars, [], {"fractional_shares": True})
    eng, ref = _both(sc, create_strategy("equal_weight"), S.EqualWeightRef())
    assert 0 < eng.equity["cash"].iloc[1] < 1e-12  # the dust this case is about
    held = 0.5 * 1000 / 3 * (1000 / (1000 / 3 * 3.07))
    want = 0.5 * held * 6.0 / 2.5
    rej = eng.rejections[eng.rejections["date"] == DAYS[2]].set_index("symbol")
    assert rej.loc["AAA", "reason"] == "no_volume"
    assert rej.loc["BBB", "reason"] == "insufficient_cash"
    assert rej.loc["BBB", "quantity"] == pytest.approx(want - held, rel=1e-9)
    assert want - held == pytest.approx(32.573290, rel=1e-7)
    assert diff(eng, ref, 1000.0) == []


def test_dust_cash_in_a_large_account_buys_nothing():
    # #184 review (B1). The same case with capital 3e9: the dust after day 2 is 4.8e-7,
    # under a billionth of B's need, so B's buy is rejected insufficient_cash at its
    # decided size. Without the rule it was neither scaled nor refused: the first buy by
    # sort order spent the dust on 1.9e-7 shares.
    from quantlab.strategies.library import create_strategy

    bars = [
        ("AAA", DAYS[0], 3.0, 3.0, 1e4),
        ("AAA", DAYS[1], 3.07, 3.5, 1e4),
        ("AAA", DAYS[2], 3.5, 3.5, 0.0),
        ("AAA", DAYS[3], 3.6, 3.6, 1e4),
        ("BBB", DAYS[0], 3.0, 3.0, 1e4),
        ("BBB", DAYS[1], 3.07, 2.5, 1e4),
        ("BBB", DAYS[2], 2.5, 2.6, 1e4),
        ("BBB", DAYS[3], 2.6, 2.6, 1e4),
    ]
    capital = 3e9
    sc = _scenario(bars, [], {"fractional_shares": True, "initial_capital": capital})
    eng, ref = _both(sc, create_strategy("equal_weight"), S.EqualWeightRef())
    dust = eng.equity["cash"].iloc[1]
    assert 0 < dust < 1e-6
    assert len(eng.trades[eng.trades["date"] == DAYS[2]]) == 0  # nothing bought with it
    rej = eng.rejections[eng.rejections["date"] == DAYS[2]].set_index("symbol")
    assert rej.loc["BBB", "reason"] == "insufficient_cash"
    held = 0.5 * capital / 3 * (capital / (capital / 3 * 3.07))
    assert rej.loc["BBB", "quantity"] == pytest.approx(0.5 * held * 6.0 / 2.5 - held, rel=1e-9)
    assert diff(eng, ref, capital) == []


def test_whole_share_buys_scaled_below_one_share_are_rejected_not_dropped():
    # #184 F2. 50 shares of each at 10 are wanted; at the next open (1100) they need
    # 110,000 against 1000 cash, so each is scaled to 50 x 1000/110000 = 0.454545 shares,
    # under one whole share. They used to vanish: no trade and no rejection.
    from quantlab.strategies.library import create_strategy

    bars = [
        (s, d, px, px, 1e4)
        for s in ("AAA", "BBB")
        for d, px in zip(DAYS[:3], (10.0, 1100.0, 1100.0), strict=True)
    ]
    sc = _scenario(bars, [], {"rebalance": "monthly"})
    eng, ref = _both(sc, create_strategy("equal_weight"), S.EqualWeightRef())
    assert len(eng.trades) == 0
    rej = eng.rejections
    assert rej["symbol"].tolist() == ["AAA", "BBB"]
    assert set(rej["reason"]) == {"insufficient_cash"}
    assert rej["quantity"].tolist() == pytest.approx([50 * 1000 / 110_000] * 2, rel=1e-12)
    assert eng.equity["equity"].tolist() == [1000.0] * 3
    assert diff(eng, ref, 1000.0) == []


@pytest.mark.parametrize("seed", SEEDS)
def test_engine_matches_reference(seed):
    assert compare(seed) == []


@pytest.mark.slow
@pytest.mark.skipif(
    not os.environ.get("QUANTLAB_ORACLE_SLOW"),
    reason="slow: set QUANTLAB_ORACLE_SLOW=1 for the seeds beyond the default set",
)
@pytest.mark.parametrize("seed", range(len(SEEDS), ALL_SEEDS))
def test_engine_matches_reference_on_every_seed(seed):
    assert compare(seed) == []


@pytest.mark.slow
@pytest.mark.skipif(
    not os.environ.get("QUANTLAB_ORACLE_PROPERTY"),
    reason="slow: set QUANTLAB_ORACLE_PROPERTY=1 to search random seeds (about 30 s)",
)
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    seed=st.integers(10_000, 2**31 - 1),
    execution=st.sampled_from(["next_open", "next_close"]),
    strategy=st.sampled_from(STRATEGIES),
)
def test_engine_matches_reference_property(seed, execution, strategy):
    assert compare(seed, [(execution, strategy)]) == []
