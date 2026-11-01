"""The look-ahead check gives the truncated run the instrument master as knowable at the
cut (#247): a delisting dated after the kept session is not known yet, except for a name
held and halted at the cut, whose delisting the engine's halt rule waits for.

Synthetic data only. Before #247 both runs got the full master, so an engine path that
acted on a delisting before it was public behaved the same in both and went unseen.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from quantlab.backtest import engine
from quantlab.data import identity
from quantlab.data.identity import key_by_instrument, master_as_of
from quantlab.data.synthetic import SyntheticSpec, generate_market
from quantlab.research import validation
from quantlab.research.validation import leakage_check

CFG = engine.BacktestConfig(start="2019-07-01", rebalance="weekly")
EARLY = pd.Timedelta(days=10)


@dataclass
class Data:
    bars: pd.DataFrame
    actions: pd.DataFrame
    master: pd.DataFrame


def _market() -> Data:
    m = generate_market(
        SyntheticSpec(
            n_assets=20, start="2019-01-02", end="2021-12-31", seed=4, delisting_fraction=0.5
        )
    )
    # Keyed by instrument, as the platform runs it: the master's ids are QLSYN..., the
    # generator's bars are by ticker. Unkeyed, the engine would match no delisting.
    bars, actions = key_by_instrument(m.bars, m.actions, m.symbol_history)
    master = m.master
    assert master["delisting_date"].notna().sum() >= 5
    assert set(master["instrument_id"]) <= set(bars["symbol"])
    return Data(bars, actions, master)


def test_master_as_of_blanks_only_delistings_not_yet_public():
    master = pd.DataFrame(
        {
            "instrument_id": ["A", "B", "C", "D"],
            "listing_date": ["2018-01-02"] * 4,
            "delisting_date": ["2020-03-02", "2020-03-03", None, "2020-06-01"],
            "delisting_reason": ["merger", "bankruptcy", None, "bankruptcy"],
            "delisting_return": [0.1, -0.9, None, -0.5],
        }
    )
    m = master_as_of(master, "2020-03-02", keep=["D"]).set_index("instrument_id")
    assert m.at["A", "delisting_date"] == pd.Timestamp("2020-03-02")  # public on its date
    assert m.at["A", "delisting_reason"] == "merger" and m.at["A", "delisting_return"] == 0.1
    assert pd.isna(m.at["B", "delisting_date"])  # the next day: not known yet
    assert m.at["B", "delisting_reason"] is None and pd.isna(m.at["B", "delisting_return"])
    assert m.at["D", "delisting_date"] == pd.Timestamp("2020-06-01")  # kept as asked
    assert m.at["D", "delisting_return"] == -0.5
    assert (m["listing_date"] == pd.Timestamp("2018-01-02")).all()
    identity.normalize_master(m.reset_index())  # still a valid master


def test_honest_strategies_stay_verified_with_delistings():
    d = _market()
    for key in ("equal_weight", "momentum", "min_variance"):
        res = leakage_check(d.bars, d.actions, key, {}, CFG, instruments=d.master, samples=40)
        assert res["status"] == "verified", (key, res)


def _acting_early(monkeypatch):
    """An engine that acts on each delisting EARLY before it is public: the leak to catch."""
    real = engine.normalize_master

    def early(frame):
        out = real(frame)
        out["delisting_date"] = out["delisting_date"] - EARLY
        return out

    monkeypatch.setattr(engine, "normalize_master", early)


def test_a_leak_through_the_master_is_caught(monkeypatch):
    d = _market()
    _acting_early(monkeypatch)
    res = leakage_check(
        d.bars, d.actions, "equal_weight", {}, CFG, instruments=d.master, samples=40
    )
    assert res["status"] == "failed", res


def test_the_same_leak_was_invisible_with_the_full_master(monkeypatch):
    # Documents the blind spot #247 closes: both runs reading the full master agree.
    d = _market()
    _acting_early(monkeypatch)
    monkeypatch.setattr(validation, "master_as_of", lambda master, when, keep=(): master)
    res = leakage_check(
        d.bars, d.actions, "equal_weight", {}, CFG, instruments=d.master, samples=40
    )
    assert res["status"] == "verified", res


HALT = 15  # sessions without a bar before the master's delisting


def _halt_then_delist() -> tuple[Data, str, pd.Timestamp, pd.Timestamp, engine.BacktestConfig]:
    """One delisted name stops printing HALT sessions before its master delisting, which
    then books -90% (the typical bankruptcy path). The engine holds it through the halt."""
    d = _market()
    sessions = pd.DatetimeIndex(sorted(d.bars["date"].unique()))
    m = d.master.set_index("instrument_id")
    later = m[m["delisting_date"] > pd.Timestamp("2020-06-01")].sort_values("delisting_date")
    iid, when = str(later.index[0]), later["delisting_date"].iloc[0]
    halt_from = sessions[sessions.searchsorted(when) - HALT]
    bars = d.bars[~((d.bars["symbol"] == iid) & (d.bars["date"] >= halt_from))]
    master = d.master.copy()
    row = master["instrument_id"] == iid
    master.loc[row, "delisting_reason"] = "bankruptcy"
    master.loc[row, "delisting_return"] = -0.9
    # A short window around the halt, so most sampled cuts fall in or near it.
    start = sessions[sessions.searchsorted(halt_from) - 40]
    end = sessions[min(sessions.searchsorted(when) + 15, len(sessions) - 1)]
    cfg = CFG.model_copy(update={"start": start.date(), "end": end.date()})
    return Data(bars, d.actions, master), iid, halt_from, when, cfg


def test_an_honest_strategy_holding_through_a_halt_to_its_delisting_is_verified():
    d, iid, halt_from, when, cfg = _halt_then_delist()
    from quantlab.strategies.library import create_strategy

    full = engine.run_backtest(
        d.bars, d.actions, create_strategy("equal_weight"), cfg, instruments=d.master
    )
    # The engine holds it through the halt and books the -90% (CRSP's convention).
    exits = full.trades[full.trades["symbol"] == iid]["note"].dropna().tolist()
    assert any("delisting return applied (-90.00%)" in n for n in exits), exits
    assert full.meta["delistings"]["by_missing_bars"] == 0
    res = leakage_check(
        d.bars, d.actions, "equal_weight", {}, cfg, instruments=d.master, samples=100
    )
    assert res["status"] == "verified", res
    # Not vacuous: some tested cut lies deep enough in the halt for the missing-bars rule
    # (5 sessions) to have fired in the truncated run without the exemption.
    sessions = pd.DatetimeIndex(sorted(d.bars["date"].unique()))
    deep = [
        c
        for c in map(pd.Timestamp, res["dates_tested"])
        if c < when and sessions.searchsorted(c) - sessions.searchsorted(halt_from) >= 5
    ]
    assert deep, (res["dates_tested"], halt_from, when)


def test_without_the_halt_exemption_the_same_case_fails(monkeypatch):
    # The exemption is load-bearing: the as-of master alone would make the truncated run
    # sell the halted name at its last close, which the full run (rightly) never does.
    d, *_, cfg = _halt_then_delist()
    monkeypatch.setattr(validation, "_held_in_a_halt", lambda *a: set())
    res = leakage_check(
        d.bars, d.actions, "equal_weight", {}, cfg, instruments=d.master, samples=100
    )
    assert res["status"] == "failed", res
