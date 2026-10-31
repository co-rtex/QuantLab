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
    # Nothing is excluded for staleness: only no-trading rows are, and counted.
    assert set(panel.attrs["excluded"]) == {"untraded_21", "dates_below_10_after_untraded"}


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


def test_no_trading_is_excluded_and_counted_not_treated_as_flat_volume():
    panel = build_panel(
        _bars(halted=None, flat="S03", untraded="S05"), NONE, MLConfig(horizon=5, sample_every=1)
    )
    s05 = panel[panel["symbol"] == "S05"]
    assert (s05["date"] < SESSIONS[200 + 20]).all()  # gone once 21 bars have no volume
    assert panel.attrs["excluded"]["untraded_21"] > 0
    assert (panel.loc[panel["symbol"] == "S03", "volume_flat_21"] == 1.0).all()  # still kept


def test_results_report_the_excluded_rows():
    from quantlab.ml.pipeline import run_ml_experiment

    out = run_ml_experiment(
        _bars(halt=_halt(40, at=260), untraded="S05"),
        NONE,
        MLConfig(horizon=5, sample_every=1, folds=2, min_train_dates=6, models=("majority",)),
    )
    ex = out["panel"]["excluded_rows"]
    assert set(ex) == {"untraded_21", "dates_below_10_after_untraded"}
    assert ex["untraded_21"] > 0


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


def test_a_halt_printed_as_zero_volume_bars_reads_like_a_halt_without_bars():
    halt = _halt(40, at=260)
    no_bars = _bars(halt=halt)  # S00 has no bars during the halt
    stale = _zero_volume_halt(no_bars, halt)  # S00 prints its last close, volume 0
    f_none, _ = compute_features(no_bars, NONE)
    f_stale, _ = compute_features(stale, NONE)
    after = [SESSIONS[i] for i in range(halt[-1] + 1, len(SESSIONS))]
    for name in ("bars_span_21", "max_gap_252", "log1p_max_gap_252"):
        pd.testing.assert_series_equal(
            f_none[name].loc[after, "S00"], f_stale[name].loc[after, "S00"], check_names=False
        )
    assert f_stale["max_gap_252"].loc[after[0], "S00"] == 40
    # Before #142 a zero-volume bar was a fresh observation: no gap at all.
    assert f_stale["bars_span_21"].loc[after[0], "S00"] == pytest.approx((21 + 40) / 21)


def test_the_gap_grows_through_a_zero_volume_halt_and_dense_names_are_unchanged():
    halt = _halt(40, at=260)
    stale = _zero_volume_halt(_bars(halt=halt), halt)
    feats, _ = compute_features(stale, NONE)
    # On the k-th zero-volume bar, k - 1 sessions have passed without a trade before it.
    for k in (1, 10, 25):
        assert feats["max_gap_252"].loc[SESSIONS[halt[0] + k - 1], "S00"] == k - 1
    dense = [c for c in feats["max_gap_252"].columns if c != "S00"]
    assert (feats["max_gap_252"][dense].dropna() == 0).all().all()
    assert (feats["bars_span_21"][dense].dropna() == 1).all().all()
