"""Point-in-time correctness where features meet, on one instrument (synthetic data).

Each feature has its own tests; this file combines them on a single security, I1:
* a rename (A -> B);
* a 2:1 split and a cash dividend whose ex-dates both fall on a weekend, so both land on
  the same (merged) Monday session;
* a three-session halt;
* a delisting with a known return (-40%), after which an unrelated security, I2, takes
  over the ticker B.

A universe is named by ticker at the start; the benchmark pays a dividend; both
execution modes run; and an ML label horizon spans the delisting. Expected values are
computed by hand from the raw prices, never by the code under test.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quantlab.backtest.engine import BacktestConfig, run_backtest
from quantlab.data.calendar import trading_days
from quantlab.data.identity import (
    key_by_instrument,
    normalize_history,
    normalize_master,
    resolve_universe,
)
from quantlab.data.schema import normalize_actions, normalize_bars
from quantlab.ml.pipeline import MLConfig, build_panel
from quantlab.strategies.library import create_strategy

SESSIONS = trading_days("2020-01-02", "2021-12-31")[:400]
RENAME, HALT, LAST_BAR, DELIST, REUSE = 280, (320, 321, 322), 339, 340, 350
# The first Monday at or after session 300: its Saturday and Sunday are not sessions.
EVENT = next(i for i in range(300, 320) if SESSIONS[i].day_of_week == 0)
SPLIT_EX = SESSIONS[EVENT] - pd.Timedelta(days=2)  # Saturday
DIV_EX = SESSIONS[EVENT] - pd.Timedelta(days=1)  # Sunday
SPLIT, DIV, DELIST_RET = 2.0, 0.5, -0.4
IDX_DIV_AT, IDX_DIV = 290, 1.0
START = 260


def _walk(rng: np.random.Generator, n: int, start: float) -> np.ndarray:
    return start * np.cumprod(1 + rng.normal(0.0003, 0.012, n))


def _market(seed: int = 0, halt: tuple[int, ...] = HALT) -> dict:
    rng = np.random.default_rng(seed)
    rows: list[dict] = []

    def add(symbol: str, idx: list[int], closes: np.ndarray) -> None:
        for i, c in zip(idx, closes, strict=True):
            o = c * (1 + rng.normal(0, 0.003))
            rows.append(
                {
                    "symbol": symbol,
                    "date": SESSIONS[i],
                    "open": o,
                    "high": max(o, c) * 1.002,
                    "low": min(o, c) * 0.998,
                    "close": c,
                    "volume": float(rng.integers(50_000, 150_000)),
                }
            )

    # I1: one raw price path; the split halves it from EVENT on (the dividend is small).
    i1_idx = [i for i in range(LAST_BAR + 1) if i not in halt]
    raw = _walk(rng, LAST_BAR + 1, 50.0)
    raw[EVENT:] = raw[EVENT:] / SPLIT
    i1 = raw[i1_idx]
    add("A", [i for i in i1_idx if i < RENAME], i1[: sum(i < RENAME for i in i1_idx)])
    add("B", [i for i in i1_idx if i >= RENAME], i1[sum(i < RENAME for i in i1_idx) :])
    add("B", list(range(REUSE, 400)), _walk(rng, 400 - REUSE, 400.0))  # I2, a new company
    idx = _walk(rng, 400, 100.0)
    idx[IDX_DIV_AT:] -= IDX_DIV  # the index drops by its dividend
    add("IDX", list(range(400)), idx)
    for k in range(12):
        add(f"F{k:02d}", list(range(400)), _walk(rng, 400, 30.0 + 5 * k))
    bars = normalize_bars(pd.DataFrame(rows))
    actions = normalize_actions(
        pd.DataFrame(
            [
                {"symbol": "B", "ex_date": SPLIT_EX, "kind": "split", "value": SPLIT},
                {"symbol": "B", "ex_date": DIV_EX, "kind": "dividend", "value": DIV},
                {
                    "symbol": "IDX",
                    "ex_date": SESSIONS[IDX_DIV_AT],
                    "kind": "dividend",
                    "value": IDX_DIV,
                },
            ]
        )
    )
    end = pd.NaT
    hist = [
        {
            "instrument_id": "I1",
            "symbol": "A",
            "valid_from": SESSIONS[0],
            "valid_to": SESSIONS[RENAME],
        },
        {
            "instrument_id": "I1",
            "symbol": "B",
            "valid_from": SESSIONS[RENAME],
            "valid_to": SESSIONS[DELIST],
        },
        {"instrument_id": "I2", "symbol": "B", "valid_from": SESSIONS[REUSE], "valid_to": end},
        {"instrument_id": "QLIDX", "symbol": "IDX", "valid_from": SESSIONS[0], "valid_to": end},
    ] + [
        {
            "instrument_id": f"QLF{k:02d}",
            "symbol": f"F{k:02d}",
            "valid_from": SESSIONS[0],
            "valid_to": end,
        }
        for k in range(12)
    ]
    history = normalize_history(pd.DataFrame(hist))
    master = normalize_master(
        pd.DataFrame(
            {
                "instrument_id": ["I1"],
                "delisting_date": [SESSIONS[DELIST]],
                "delisting_reason": ["other"],
                "delisting_return": [DELIST_RET],
            }
        )
    )
    kb, ka = key_by_instrument(bars, actions, history)
    return {
        "bars": bars,
        "actions": actions,
        "history": history,
        "master": master,
        "kb": kb,
        "ka": ka,
        "raw": raw,
    }


@pytest.fixture(scope="module")
def m():
    return _market()


def _close(m: dict, iid: str) -> pd.Series:
    b = m["kb"][m["kb"]["symbol"] == iid]
    return b.set_index("date")["close"]


def _run(m: dict, execution: str, kb=None, ka=None, master=None):
    ids, unresolved = resolve_universe(m["history"], ["A"], SESSIONS[START])
    assert ids == ["I1"] and not unresolved  # the ticker means its holder at the start
    cfg = BacktestConfig(
        start=SESSIONS[START].date(),
        universe=ids,
        benchmark="QLIDX",
        execution=execution,
        commission_bps=0,
        slippage_bps=0,
        fractional_shares=True,
    )
    return run_backtest(
        m["kb"] if kb is None else kb,
        m["ka"] if ka is None else ka,
        create_strategy("buy_and_hold"),
        cfg,
        instruments=m["master"] if master is None else master,
    )


# --------------------------------------------------------------------- identity
def test_a_reused_ticker_never_reaches_the_old_instrument(m):
    kb = m["kb"]
    i1 = kb[kb["symbol"] == "I1"]
    assert i1["date"].max() == SESSIONS[LAST_BAR]  # nothing of I2's printed as I1
    assert kb[kb["symbol"] == "I2"]["date"].min() == SESSIONS[REUSE]
    # "B" at the start names nothing yet: its first holder took it by rename and the next
    # by reuse, so a universe of "B" must not pull in either (and certainly not I2).
    ids, unresolved = resolve_universe(m["history"], ["B"], SESSIONS[START], SESSIONS[-1])
    assert ids == [] and unresolved == ["B"]  # neither I1 (renamed into B) nor I2 (reuse)


# --------------------------------------------------------------------- backtest
@pytest.mark.parametrize("execution", ["next_open", "next_close"])
def test_value_is_conserved_through_split_dividend_halt_and_delisting(m, execution):
    res = _run(m, execution)
    t = res.trades
    buys = t[(t["symbol"] == "I1") & (t["side"] == "buy")]
    assert len(buys) == 1  # bought once
    q0, p0 = float(buys["quantity"].iloc[0]), float(buys["price"].iloc[0])
    eq = res.equity
    cash0 = float(res.config.initial_capital - q0 * p0)
    close = _close(m, "I1")

    # The merged Monday: in calendar order the split (Saturday) comes before the dividend
    # (Sunday), so the dividend is paid on the post-split share count.
    e, prev = SESSIONS[EVENT], SESSIONS[EVENT - 1]
    paid = SPLIT * q0 * DIV
    expected = (cash0 + paid + SPLIT * q0 * close[e]) / (cash0 + q0 * close[prev])
    assert eq.loc[e, "equity"] / eq.loc[prev, "equity"] == pytest.approx(expected, rel=1e-12)

    # The halt: no bars, so nothing moves (marks stay at the last close).
    halt = [SESSIONS[i] for i in (HALT[0] - 1, *HALT)]
    assert eq.loc[halt, "equity"].nunique() == 1

    # Delisting: booked exactly once, on the first session after the delisting date, at
    # the last close x (1 + return), on the post-split share count.
    delist = t[
        t.get("note", pd.Series(index=t.index, dtype=object)).fillna("").str.startswith("delisting")
    ]
    assert len(delist) == 1
    row = delist.iloc[0]
    assert row["date"] == SESSIONS[DELIST + 1]
    assert row["quantity"] == pytest.approx(-SPLIT * q0, rel=1e-12)
    assert row["price"] == pytest.approx(close[SESSIONS[LAST_BAR]] * (1 + DELIST_RET), rel=1e-12)
    final = cash0 + paid + SPLIT * q0 * close[SESSIONS[LAST_BAR]] * (1 + DELIST_RET)
    assert eq["equity"].iloc[-1] == pytest.approx(final, rel=1e-12)
    assert res.meta["delistings"]["with_return"] == 1


@pytest.mark.parametrize("execution", ["next_open", "next_close"])
def test_the_benchmark_compounds_its_own_total_return(m, execution):
    res = _run(m, execution)
    b = res.benchmark.dropna()
    idx = _close(m, "QLIDX").reindex(b.index)
    ratio = (b / b.shift(1)).iloc[1:]
    hand = idx / idx.shift(1)
    hand.loc[SESSIONS[IDX_DIV_AT]] = (idx[SESSIONS[IDX_DIV_AT]] + IDX_DIV) / idx[
        SESSIONS[IDX_DIV_AT - 1]
    ]
    np.testing.assert_allclose(ratio.to_numpy(), hand.iloc[1:].to_numpy(), rtol=1e-12)


@pytest.mark.parametrize("execution", ["next_open", "next_close"])
@pytest.mark.parametrize("cut", [EVENT - 3, HALT[1], LAST_BAR - 2])
def test_nothing_after_a_cutoff_changes_anything_decided_before_it(m, execution, cut):
    rng = np.random.default_rng(cut)
    cutoff = SESSIONS[cut]
    kb = m["kb"].copy()
    after = kb["date"] > cutoff
    noise = rng.uniform(0.5, 2.0, after.sum())
    for c in ("open", "high", "low", "close"):
        kb.loc[after, c] = kb.loc[after, c] * noise
    ka = pd.concat(
        [
            m["ka"],
            normalize_actions(
                pd.DataFrame(
                    [{"symbol": "I1", "ex_date": SESSIONS[cut + 1], "kind": "split", "value": 3.0}]
                )
            ),
        ],
        ignore_index=True,
    )
    master = m["master"].assign(delisting_return=[-0.95])
    base, moved = _run(m, execution), _run(m, execution, kb, ka, master)
    early = base.equity.index <= cutoff
    pd.testing.assert_frame_equal(base.equity.loc[early], moved.equity.loc[early])
    # The perturbation is real: results after the cutoff do change.
    assert not np.allclose(base.equity.loc[~early, "equity"], moved.equity.loc[~early, "equity"])
    decided = base.trades["decision_date"] < cutoff
    decided_m = moved.trades["decision_date"] < cutoff
    pd.testing.assert_frame_equal(
        base.trades.loc[decided].reset_index(drop=True),
        moved.trades.loc[decided_m].reset_index(drop=True),
    )


# --------------------------------------------------------------------------- ML
H = 21


def _panel(m: dict, kb=None, ka=None) -> pd.DataFrame:
    cfg = MLConfig(horizon=H, sample_every=1)
    return build_panel(
        m["kb"] if kb is None else kb, m["ka"] if ka is None else ka, cfg, instruments=m["master"]
    )


def _hand_tr(m: dict) -> pd.Series:
    """I1's total return per bar, from raw prices: S * (C_t + d) / C_prev, the dividend being
    paid per post-split share (the split's ex-date comes first in calendar order)."""
    c = _close(m, "I1")
    tr = c / c.shift(1)
    e = SESSIONS[EVENT]
    tr[e] = SPLIT * (c[e] + DIV) / c.shift(1)[e]
    return tr


def test_labels_that_span_the_delisting_include_its_return_and_no_others_do(m):
    panel = _panel(m)
    i1 = panel[panel["symbol"] == "I1"].set_index("date")
    assert len(i1) and i1.index.max() <= SESSIONS[LAST_BAR]  # never sampled after it
    tr = _hand_tr(m)
    first_after = DELIST + 1  # the delisting return is public on this session
    checked = {"spans": 0, "within": 0}
    for d, row in i1.iterrows():
        k = SESSIONS.get_loc(d)
        end = SESSIONS[min(k + H, LAST_BAR)]  # the last close at or before t+h
        growth = tr[(tr.index > d) & (tr.index <= end)].prod()
        if k + H >= first_after:
            expected = growth * (1 + DELIST_RET) - 1
            checked["spans"] += 1
        else:
            expected = growth - 1
            checked["within"] += 1
        assert row["fwd_return"] == pytest.approx(expected, rel=1e-9), d
        assert row["label_available_at"] == SESSIONS[k + H]
    assert checked["spans"] > 0 and checked["within"] > 0


def test_the_reused_ticker_leaves_the_old_instruments_rows_untouched(m):
    with_i2 = _panel(m)
    kb = m["kb"][m["kb"]["symbol"] != "I2"]
    without = _panel(m, kb=kb)
    a = with_i2[with_i2["symbol"] == "I1"].reset_index(drop=True)
    b = without[without["symbol"] == "I1"].reset_index(drop=True)
    assert len(a) > 0
    pd.testing.assert_frame_equal(a, b)


def test_ml_features_and_known_labels_do_not_look_ahead(m):
    cut = SESSIONS[HALT[1]]
    kb = m["kb"].copy()
    after = kb["date"] > cut
    for c in ("open", "high", "low", "close"):
        kb.loc[after, c] = kb.loc[after, c] * 1.7
    ka = pd.concat(
        [
            m["ka"],
            normalize_actions(
                pd.DataFrame(
                    [
                        {
                            "symbol": "QLF03",
                            "ex_date": SESSIONS[HALT[1] + 5],
                            "kind": "split",
                            "value": 4.0,
                        }
                    ]
                )
            ),
        ],
        ignore_index=True,
    )
    base, moved = _panel(m), _panel(m, kb=kb, ka=ka)
    later = base["label_available_at"] > cut
    assert not np.allclose(  # the perturbation is real: later labels change
        base.loc[later, "fwd_return"].to_numpy(),
        moved.loc[moved["label_available_at"] > cut, "fwd_return"].to_numpy(),
    )
    known = base["label_available_at"] <= cut
    known_m = moved["label_available_at"] <= cut
    pd.testing.assert_frame_equal(
        base.loc[known].reset_index(drop=True),
        moved.loc[known_m].reset_index(drop=True),
        rtol=1e-12,
    )
    feats = [c for c in base.columns if c not in ("fwd_return", "label", "label_available_at")]
    early, early_m = base["date"] <= cut, moved["date"] <= cut
    pd.testing.assert_frame_equal(
        base.loc[early, feats].reset_index(drop=True),
        moved.loc[early_m, feats].reset_index(drop=True),
        rtol=1e-12,
    )


def test_a_short_halt_does_not_remove_a_security_from_the_ml_panel_for_a_year(m):
    panel = _panel(m)
    i1 = panel[panel["symbol"] == "I1"]
    dates = set(i1["date"])
    # Fixed by #120: absent only on the halt's sessions, back on its first bar after.
    assert SESSIONS[HALT[0] - 1] in dates
    assert not {SESSIONS[i] for i in HALT} & dates
    assert SESSIONS[HALT[-1] + 1] in dates
