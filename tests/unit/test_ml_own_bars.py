"""ML features over each instrument's own bars (#120). Synthetic data only."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quantlab.data.adjust import PointInTimeAdjuster, total_returns
from quantlab.data.calendar import trading_days
from quantlab.data.schema import normalize_actions, normalize_bars, wide
from quantlab.ml.pipeline import (
    FEATURES,
    ML_PIPELINE_VERSION,
    MLConfig,
    build_panel,
    compute_features,
)

SESSIONS = trading_days("2020-01-02", "2021-12-31")[:330]
HALT = (290, 291, 292)
NONE = normalize_actions(None)
KEYS = {"zero_volume_bars", "dates_below_10_with_zero_volume", "dividends_after_last_trade"}


def _bars(
    halted: str | None = "S00",
    flat: str | None = None,
    seed: int = 0,
    halt: tuple[int, ...] = HALT,
    untraded: str | None = None,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for k in range(12):
        sym = f"S{k:02d}"
        closes = 20.0 * np.cumprod(1 + rng.normal(0.0004, 0.015, len(SESSIONS)))
        for i, (d, c) in enumerate(zip(SESSIONS, closes, strict=True)):
            if sym == halted and i in halt:
                continue
            vol = 1e5 if sym == flat else float(rng.integers(50_000, 150_000))
            if sym == untraded and i >= 200:
                vol = 0.0  # a stale quote: a price, but no trades
            rows.append(
                {
                    "symbol": sym,
                    "date": d,
                    "open": c,
                    "high": c,
                    "low": c,
                    "close": c,
                    "volume": vol,
                }
            )
    return normalize_bars(pd.DataFrame(rows))


def _session_reference(bars: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """The pre-#120 formulas over calendar sessions; equal to own bars when nothing is missing."""
    close = wide(bars, "close")
    vol = wide(bars, "volume").reindex(index=close.index, columns=close.columns)
    adj = close * PointInTimeAdjuster(close, NONE).multiplier(None)
    rets = total_returns(bars, NONE).reindex(index=close.index, columns=close.columns)
    logv = np.log1p(vol)

    def mom(n: int, skip: int = 0) -> pd.DataFrame:
        return adj.shift(skip) / adj.shift(n) - 1

    return {
        "mom_21": mom(21),
        "mom_63": mom(63),
        "mom_126": mom(126),
        "mom_252_21": mom(252, 21),
        "rev_5": mom(5),
        "vol_21": rets.rolling(21).std(),
        "vol_63": rets.rolling(63).std(),
        "dist_high_252": adj / adj.rolling(252).max() - 1,
        "volume_z_21": (logv - logv.rolling(21).mean()) / logv.rolling(21).std(),
    }


def test_without_gaps_the_features_are_unchanged():
    bars = _bars(halted=None)
    feats, _ = compute_features(bars, NONE)
    ref = _session_reference(bars)
    for name in ref:  # the pre-#120 features; the staleness covariates are new
        pd.testing.assert_frame_equal(feats[name], ref[name], check_names=False, rtol=1e-12)


def test_after_a_halt_windows_count_the_instruments_own_bars():
    bars = _bars()
    feats, _ = compute_features(bars, NONE)
    s = bars[bars["symbol"] == "S00"].set_index("date")["close"]  # its own bars only
    first_after = SESSIONS[HALT[-1] + 1]
    k = s.index.get_loc(first_after)
    r = s / s.shift(1) - 1  # the first bar after the halt spans it
    # By hand, over the last 21 / 63 own bars and the 21st / 252nd bar back.
    assert feats["vol_21"].loc[first_after, "S00"] == pytest.approx(
        r.iloc[k - 20 : k + 1].std(), rel=1e-12
    )
    assert feats["vol_63"].loc[first_after, "S00"] == pytest.approx(
        r.iloc[k - 62 : k + 1].std(), rel=1e-12
    )
    assert feats["mom_21"].loc[first_after, "S00"] == pytest.approx(
        s.iloc[k] / s.iloc[k - 21] - 1, rel=1e-12
    )
    assert feats["dist_high_252"].loc[first_after, "S00"] == pytest.approx(
        s.iloc[k] / s.iloc[k - 251 : k + 1].max() - 1, rel=1e-12
    )
    for name in FEATURES:  # defined on the first bar after the halt, NaN only during it
        assert np.isfinite(feats[name].loc[first_after, "S00"]), name
        assert feats[name].loc[[SESSIONS[i] for i in HALT], "S00"].isna().all(), name


def test_a_halted_security_is_back_in_the_panel_as_soon_as_it_trades():
    panel = build_panel(_bars(), NONE, MLConfig(horizon=5, sample_every=1))
    s00 = set(panel.loc[panel["symbol"] == "S00", "date"])
    assert SESSIONS[HALT[0] - 1] in s00
    assert not {SESSIONS[i] for i in HALT} & s00  # no row while it has no bar
    assert SESSIONS[HALT[-1] + 1] in s00  # and back on its first bar after the halt


def test_own_bar_features_do_not_look_ahead():
    bars = _bars()
    feats, _ = compute_features(bars, NONE)
    for cut in (HALT[0] - 1, HALT[-1] + 1, HALT[-1] + 10):
        t = SESSIONS[cut]
        part, _ = compute_features(bars[bars["date"] <= t], NONE)
        for name in FEATURES:
            pd.testing.assert_frame_equal(
                feats[name].loc[:t], part[name].loc[:t], check_names=False, rtol=1e-12
            )


def test_flat_volume_is_a_zero_z_score_with_a_flag_not_a_dropped_row():
    panel = build_panel(_bars(halted=None, flat="S03"), NONE, MLConfig(horizon=5, sample_every=1))
    s03 = panel[panel["symbol"] == "S03"]
    assert len(s03) > 0  # before #120: 0/0 = NaN dropped every row
    assert (s03["volume_z_21"] == 0.0).all() and (s03["volume_flat_21"] == 1.0).all()
    others = panel[panel["symbol"] != "S03"]
    assert (others["volume_flat_21"] == 0.0).all()


def test_results_record_the_pipeline_version():
    from quantlab.ml.pipeline import run_ml_experiment

    out = run_ml_experiment(
        _bars(),
        NONE,
        MLConfig(horizon=5, sample_every=1, folds=2, min_train_dates=6, models=("majority",)),
    )
    assert out["pipeline_version"] == ML_PIPELINE_VERSION == 3
    assert out["panel"]["volume_flat_rows"] == 0


# ------------------------------------------------ #125 review: staleness, no trading
def _halt(length: int, at: int = 280) -> tuple[int, ...]:
    return tuple(range(at, at + length))


def test_a_long_halt_is_described_by_the_covariates_not_excluded():
    # #125 review: excluding stale windows selects against thinly traded names, so a halted
    # security is kept and its staleness is a model input instead.
    halt = _halt(40, at=260)
    bars = _bars(halt=halt)
    panel = build_panel(bars, NONE, MLConfig(horizon=5, sample_every=1))
    first_after = SESSIONS[halt[-1] + 1]
    row = panel[(panel["symbol"] == "S00") & (panel["date"] == first_after)]
    assert len(row) == 1  # back on its first bar after the halt
    assert row["max_gap_252"].iloc[0] == 40  # one 40-session gap
    assert row["log1p_max_gap_252"].iloc[0] == pytest.approx(np.log1p(40))  # the model input
    assert row["bars_span_21"].iloc[0] == pytest.approx((21 + 40) / 21)
    dense = panel[panel["symbol"] != "S00"]
    assert (dense["max_gap_252"] == 0).all() and (dense["bars_span_21"] == 1).all()
    # Nothing is excluded for staleness: only zero-volume bars are, and counted.
    assert set(panel.attrs["excluded"]) == KEYS


def test_thin_trading_is_kept_and_told_apart_from_a_halt():
    # An irregular thin pattern (random 50% of sessions from session 270, after 270 bars of
    # history): its gaps sum to more than the retracted 2*L cap allowed, which dropped such
    # names. Every bar row with complete features must now be kept.
    bars = _bars(halted=None)
    late = bars.index[(bars["symbol"] == "S01") & (bars["date"] >= SESSIONS[270])]
    rng = np.random.default_rng(7)
    thin = bars.drop(index=late[rng.random(len(late)) >= 0.5])
    cfg = MLConfig(horizon=5, sample_every=1)
    panel = build_panel(thin, NONE, cfg)
    sampled = set(panel["date"])
    # Expected independently of the pipeline: every sampled bar from the name's 253rd own
    # bar on (the warm-up of the 252-bar features), with a defined label.
    own = sorted(thin.loc[thin["symbol"] == "S01", "date"])
    expected = {d for d in own[252:] if d in sampled and d <= SESSIONS[-1 - cfg.horizon]}
    rows = panel[panel["symbol"] == "S01"]
    assert len(expected) > 20 and set(rows["date"]) == expected
    # Under the retracted cap some of these rows were stale (a window over 2x its length):
    pos = pd.Series(SESSIONS.get_indexer(sorted(thin.loc[thin["symbol"] == "S01", "date"])))
    assert ((pos - pos.shift(5)) > 10).any()  # e.g. rev_5's 5 bars spanning > 10 sessions
    # Many short gaps, unlike a halt's one long gap.
    thin_rows = rows[rows["date"] >= SESSIONS[315]]  # 21-bar windows inside the thin period
    assert len(thin_rows) and rows["max_gap_252"].max() < 15
    assert thin_rows["bars_span_21"].median() > 1.5


def test_zero_volume_bars_are_excluded_and_counted_not_treated_as_flat_volume():
    cfg = MLConfig(horizon=5, sample_every=1)
    panel = build_panel(_bars(halted=None, flat="S03", untraded="S05"), NONE, cfg)
    # S05 prints only zero-volume closes from session 200: no bar, so no rows (and no
    # history to come back with). Before #142 its rows stayed until 21 bars had no volume.
    assert not (panel["symbol"] == "S05").any()  # every panel date is past session 252
    sampled = panel["date"].nunique()
    assert panel.attrs["excluded"]["zero_volume_bars"] == sampled  # one per panel date
    assert (panel.loc[panel["symbol"] == "S03", "volume_flat_21"] == 1.0).all()  # still kept


def test_results_report_the_excluded_rows():
    from quantlab.ml.pipeline import run_ml_experiment

    out = run_ml_experiment(
        _bars(halt=_halt(40, at=260), untraded="S05"),
        NONE,
        MLConfig(horizon=5, sample_every=1, folds=2, min_train_dates=6, models=("majority",)),
    )
    ex = out["panel"]["excluded_rows"]
    assert set(ex) == KEYS
    assert ex["zero_volume_bars"] > 0


# ------------------------------------------- #142: zero-volume bars are missing trades
def _zero_volume_halt(bars: pd.DataFrame, halt: tuple[int, ...]) -> pd.DataFrame:
    """The same halt printed as zero-volume bars repeating the last close (a stale quote)."""
    out = bars.copy()
    sym = out["symbol"] == "S00"
    last = out[sym & (out["date"] < SESSIONS[halt[0]])].iloc[-1]
    rows = pd.DataFrame(
        [
            {
                "symbol": "S00",
                "date": SESSIONS[i],
                "open": last["close"],
                "high": last["close"],
                "low": last["close"],
                "close": last["close"],
                "volume": 0.0,
            }
            for i in halt
        ]
    )
    return normalize_bars(pd.concat([out, rows], ignore_index=True))


def _reopen_lower(bars: pd.DataFrame, after: int, factor: float = 0.7) -> pd.DataFrame:
    """S00 reopens ``factor`` lower after session ``after`` (prices scaled from then on)."""
    out = bars.copy()
    later = (out["symbol"] == "S00") & (out["date"] > SESSIONS[after])
    for col in ("open", "high", "low", "close"):
        out.loc[later, col] *= factor
    return out


def test_a_halt_printed_as_zero_volume_bars_is_a_halt_without_bars():
    # The #147 review: a 40-session halt printed as zero-volume stale closes, reopening 30%
    # lower. Its zero-volume bars were decision rows that read as fresh (span 1, gap 0),
    # and labels started at the stale close (fwd_return -0.40 with h = 21).
    halt = _halt(40, at=260)
    no_bars = _reopen_lower(_bars(halt=halt), halt[-1])
    stale = _zero_volume_halt(no_bars, halt)
    f_none, _ = compute_features(no_bars, NONE)
    f_stale, _ = compute_features(stale, NONE)
    for name in [*FEATURES, "max_gap_252"]:  # every feature, every name, every session
        pd.testing.assert_frame_equal(f_none[name], f_stale[name], check_names=False)
    assert f_stale["max_gap_252"].loc[SESSIONS[halt[-1] + 1], "S00"] == 40
    assert f_stale["bars_span_21"].loc[SESSIONS[halt[-1] + 1], "S00"] == pytest.approx(61 / 21)
    cfg = MLConfig(horizon=21, sample_every=1)
    p_none, p_stale = build_panel(no_bars, NONE, cfg), build_panel(stale, NONE, cfg)
    pd.testing.assert_frame_equal(p_none, p_stale)  # no row and no label from a stale close
    halt_dates = {SESSIONS[i] for i in halt}
    assert not halt_dates & set(p_stale.loc[p_stale["symbol"] == "S00", "date"])
    # Counted: one zero-volume bar per halt session that made the panel.
    assert p_stale.attrs["excluded"]["zero_volume_bars"] == len(halt_dates & set(p_stale["date"]))
    assert p_none.attrs["excluded"]["zero_volume_bars"] == 0


def test_zero_volume_bars_do_not_look_ahead():
    halt = _halt(40, at=260)
    stale = _zero_volume_halt(_reopen_lower(_bars(halt=halt), halt[-1]), halt)
    feats, _ = compute_features(stale, NONE)
    for cut in (halt[0] - 1, halt[10], halt[-1], halt[-1] + 1, halt[-1] + 15):
        t = SESSIONS[cut]
        part, _ = compute_features(stale[stale["date"] <= t], NONE)
        for name in FEATURES:
            pd.testing.assert_frame_equal(
                feats[name].loc[:t], part[name].loc[:t], check_names=False, rtol=1e-12
            )


def test_a_thin_name_printing_zero_volume_closes_reads_like_one_without_bars():
    # N4: a name that trades on a random half of the sessions from session 270 and prints
    # a zero-volume stale close on the others. It never had 21 bars without volume, so
    # the retracted untraded_21 rule never removed its stale rows.
    bars = _bars(halted=None)
    s01 = bars["symbol"] == "S01"
    late = bars.index[s01 & (bars["date"] >= SESSIONS[270])]
    idle = late[np.random.default_rng(7).random(len(late)) >= 0.5]
    no_bars = bars.drop(index=idle)
    stale = bars.copy()  # a stale quote on the idle sessions: the last traded close, volume 0
    stale.loc[idle, "close"] = np.nan
    stale.loc[s01, "close"] = stale.loc[s01, "close"].ffill()
    stale.loc[idle, ["open", "high", "low"]] = stale.loc[idle, ["close"] * 3].to_numpy()
    stale.loc[idle, "volume"] = 0.0
    cfg = MLConfig(horizon=5, sample_every=1)
    p_none, p_stale = build_panel(no_bars, NONE, cfg), build_panel(stale, NONE, cfg)
    pd.testing.assert_frame_equal(p_none, p_stale)
    idle_dates = set(bars.loc[idle, "date"])
    assert len(idle_dates & set(p_stale["date"])) > 20
    assert p_stale.attrs["excluded"]["zero_volume_bars"] == len(idle_dates & set(p_stale["date"]))


def test_an_unknown_volume_is_still_a_bar():
    # N3: NaN volume is unknown, not zero. The bar has a close, so it is kept: prices,
    # returns and the staleness inputs count it as a bar. Only the volume features of the
    # 21 windows that contain it are undefined, and those rows are incomplete.
    bars = _bars(halted=None)
    at = SESSIONS[280]
    unknown = bars.copy()
    unknown.loc[(unknown["symbol"] == "S02") & (unknown["date"] == at), "volume"] = np.nan
    f_known, _ = compute_features(bars, NONE)
    f_unknown, _ = compute_features(unknown, NONE)
    for name in [*FEATURES, "max_gap_252"]:
        if name in ("volume_z_21", "volume_flat_21"):
            continue
        pd.testing.assert_frame_equal(f_known[name], f_unknown[name], check_names=False)
    window = [SESSIONS[i] for i in range(280, 301)]
    assert f_unknown["volume_z_21"].loc[window, "S02"].isna().all()
    assert np.isfinite(f_unknown["volume_z_21"].loc[SESSIONS[301], "S02"])
    panel = build_panel(unknown, NONE, MLConfig(horizon=5, sample_every=1))
    rows = set(panel.loc[panel["symbol"] == "S02", "date"])
    assert not rows & set(window) and SESSIONS[279] in rows and SESSIONS[301] in rows
    assert panel.attrs["excluded"]["zero_volume_bars"] == 0  # unknown is not zero


def test_a_longer_warm_up_is_not_counted_as_a_zero_volume_date():
    # #147 re-review: 3 of 12 names print zero volume on sessions 0-29, so their 253rd own
    # bar (mom_252_21's warm-up) comes 30 sessions later. Dates 252-281 have 9 names and
    # are skipped, but no zero-volume bar is on them, so the counter reads 0 (documented).
    bars = _bars(halted=None)
    early = bars["symbol"].isin(["S00", "S01", "S02"]) & (bars["date"] < SESSIONS[30])
    bars.loc[early, "volume"] = 0.0
    panel = build_panel(bars, NONE, MLConfig(horizon=5, sample_every=1))
    assert panel["date"].min() == SESSIONS[282]
    assert panel.attrs["excluded"]["dates_below_10_with_zero_volume"] == 0


def _dividend(sym: str, at: int, value: float = 0.5) -> pd.DataFrame:
    row = {"symbol": sym, "ex_date": SESSIONS[at], "kind": "dividend", "value": value}
    return normalize_actions(pd.DataFrame([row]))


@pytest.mark.parametrize(
    ("sym", "ex", "lost"),
    [
        ("S00", 310, 1),  # inside S00's zero-volume tail from session 300: never applied
        ("S00", 250, 0),  # on a traded bar
        ("S01", 310, 0),  # S01 trades to the end
    ],
)
def test_dividends_in_a_zero_volume_tail_are_counted(sym, ex, lost):
    # #147 re-review NB2: ML cannot apply such a dividend; the engine pays it (#149).
    bars = _bars(halted=None)
    bars.loc[(bars["symbol"] == "S00") & (bars["date"] >= SESSIONS[300]), "volume"] = 0.0
    panel = build_panel(bars, _dividend(sym, ex), MLConfig(horizon=5, sample_every=1))
    assert panel.attrs["excluded"]["dividends_after_last_trade"] == lost


def test_a_dividend_inside_a_zero_volume_halt_is_applied_on_the_reopen_not_counted():
    halt = _halt(40, at=260)
    stale = _zero_volume_halt(_bars(halt=halt), halt)
    panel = build_panel(stale, _dividend("S00", halt[5]), MLConfig(horizon=5, sample_every=1))
    assert panel.attrs["excluded"]["dividends_after_last_trade"] == 0
