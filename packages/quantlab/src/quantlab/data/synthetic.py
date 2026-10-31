"""Deterministic synthetic market generator.

All output is SYNTHETIC and must be labelled as such wherever it is displayed. The
generator exists to provide reproducible fixtures with realistic structure:

* a regime-switching market factor (calm / stressed volatility states),
* sector factors and per-asset betas (so correlations and factor exposures exist),
* raw (unadjusted) OHLCV prices with splits and cash dividends,
* staggered listings and delistings (so survivorship bias can be demonstrated),
* a cap-weight-like benchmark asset that tracks the market factor.

Raw prices obey the total-return identity used everywhere in QuantLab:

    (split_ratio * close_t + dividend_t) / close_{t-1} = 1 + r_t

where ``dividend_t`` is per pre-split share and is paid at the ex-date open.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from quantlab.data.calendar import trading_days
from quantlab.data.identity import normalize_history, normalize_master
from quantlab.data.schema import normalize_actions, normalize_bars

SECTORS = ("Technology", "Financials", "Healthcare", "Energy", "Industrials", "Consumer")
BENCHMARK_SYMBOL = "SYNIDX"


@dataclass(frozen=True)
class SyntheticSpec:
    n_assets: int = 50
    start: str = "2018-01-02"
    end: str = "2023-12-29"
    seed: int = 7
    annual_market_drift: float = 0.07
    calm_daily_vol: float = 0.008
    stressed_daily_vol: float = 0.022
    p_calm_to_stress: float = 0.01
    p_stress_to_calm: float = 0.05
    split_probability: float = 0.0006  # per asset-day
    quarterly_dividend_yield_range: tuple[float, float] = (0.0, 0.009)
    late_listing_fraction: float = 0.1
    delisting_fraction: float = 0.08
    include_benchmark: bool = True


@dataclass
class SyntheticMarket:
    spec: SyntheticSpec
    bars: pd.DataFrame
    actions: pd.DataFrame
    instruments: pd.DataFrame
    market_regime: pd.Series
    provenance: dict[str, object] = field(default_factory=dict)

    @property
    def symbol_history(self) -> pd.DataFrame:
        """Point-in-time ticker history: one ticker per synthetic instrument, valid from
        its listing until the day after its last bar (open-ended while it trades)."""
        i = self.instruments
        return normalize_history(
            pd.DataFrame(
                {
                    "instrument_id": i["instrument_id"],
                    "symbol": i["symbol"],
                    "valid_from": i["listed"],
                    "valid_to": i["delisted"] + pd.Timedelta(days=1),
                }
            )
        )

    @property
    def master(self) -> pd.DataFrame:
        """Instrument master (listing and delisting facts); no delisting returns."""
        i = self.instruments
        return normalize_master(
            i.rename(columns={"listed": "listing_date", "delisted": "delisting_date"})
        )


def _symbols(n: int) -> list[str]:
    width = max(3, len(str(n)))
    return [f"SYN{i:0{width}d}" for i in range(1, n + 1)]


def generate_market(spec: SyntheticSpec | None = None) -> SyntheticMarket:
    spec = spec or SyntheticSpec()
    rng = np.random.default_rng(spec.seed)
    dates = trading_days(spec.start, spec.end)
    T = len(dates)
    if T < 30:
        raise ValueError("synthetic market needs at least 30 trading days")
    symbols = _symbols(spec.n_assets)
    n = len(symbols)

    # --- market regime and factors -------------------------------------------------
    regime = np.zeros(T, dtype=np.int8)
    for t in range(1, T):
        u = rng.random()
        if regime[t - 1] == 0:
            regime[t] = 1 if u < spec.p_calm_to_stress else 0
        else:
            regime[t] = 0 if u < spec.p_stress_to_calm else 1
    mkt_vol = np.where(regime == 1, spec.stressed_daily_vol, spec.calm_daily_vol)
    mkt_mu = spec.annual_market_drift / 252 - np.where(regime == 1, 0.0015, 0.0)
    market = mkt_mu + mkt_vol * rng.standard_normal(T)
    n_sectors = len(SECTORS)
    sector_ret = 0.006 * rng.standard_normal((T, n_sectors))

    sector_of = rng.integers(0, n_sectors, size=n)
    beta = np.clip(rng.normal(1.0, 0.3, size=n), 0.2, 2.2)
    sector_load = rng.uniform(0.3, 1.0, size=n)
    idio_vol = rng.uniform(0.008, 0.025, size=n)
    alpha = rng.normal(0.0, 0.00015, size=n)
    eps = rng.standard_normal((T, n)) * idio_vol
    total_ret = alpha + market[:, None] * beta + sector_ret[:, sector_of] * sector_load + eps
    total_ret = np.clip(total_ret, -0.6, 1.5)

    # --- listing windows ------------------------------------------------------------
    first = np.zeros(n, dtype=int)
    last = np.full(n, T - 1, dtype=int)
    late = rng.random(n) < spec.late_listing_fraction
    first[late] = rng.integers(20, max(21, T // 2), size=late.sum())
    delist = rng.random(n) < spec.delisting_fraction
    for i in map(int, np.flatnonzero(delist)):
        lo = first[i] + 60
        if lo < T - 5:
            last[i] = int(rng.integers(lo, T - 5))
    # A delisted name drifts down hard into its final bar (distressed exit).
    for i in map(int, np.flatnonzero(last < T - 1)):
        k0 = max(first[i] + 1, last[i] - 20)
        total_ret[k0 : last[i] + 1, i] -= 0.01

    # --- corporate actions ------------------------------------------------------------
    div_yield = rng.uniform(*spec.quarterly_dividend_yield_range, size=n)
    div_yield[rng.random(n) < 0.35] = 0.0  # non-payers
    quarter_offset = rng.integers(0, 63, size=n)
    split_mask = rng.random((T, n)) < spec.split_probability
    split_ratio = np.where(split_mask, rng.choice([2.0, 3.0, 1.5], size=(T, n)), 1.0)

    # --- raw price paths ----------------------------------------------------------
    close = np.full((T, n), np.nan)
    open_ = np.full((T, n), np.nan)
    dividends = np.zeros((T, n))
    close[first, np.arange(n)] = rng.uniform(20, 200, size=n)
    open_[first, np.arange(n)] = close[first, np.arange(n)] * np.exp(rng.normal(0, 0.003, size=n))
    gap_noise = rng.standard_normal((T, n))
    for t in range(1, T):
        alive = (first < t) & (last >= t)
        if not alive.any():
            continue
        prev = close[t - 1]
        is_div_day = alive & (div_yield > 0) & (((t + quarter_offset) % 63) == 0)
        div = np.where(is_div_day, np.round(prev * div_yield, 2), 0.0)
        s = np.where(alive, split_ratio[t], 1.0)
        # Only allow splits when the post-split price stays above $5.
        s = np.where(prev / s > 5.0, s, 1.0)
        new_close = (prev * (1 + total_ret[t]) - div) / s
        bad = alive & ~(new_close > 0.01)
        new_close = np.where(bad, 0.01, new_close)
        close[t] = np.where(alive, new_close, close[t])
        dividends[t] = np.where(alive, div, 0.0)
        split_ratio[t] = s
        ex_prev = (prev - div) / s
        sigma = np.where(regime[t] == 1, 0.5, 0.3) * idio_vol
        open_[t] = np.where(alive, ex_prev * np.exp(sigma * gap_noise[t]), open_[t])

    hi_noise = np.abs(rng.standard_normal((T, n))) * idio_vol * 0.6
    lo_noise = np.abs(rng.standard_normal((T, n))) * idio_vol * 0.6
    high = np.fmax(open_, close) * np.exp(hi_noise)
    low = np.fmin(open_, close) * np.exp(-lo_noise)
    volume = np.round(np.exp(rng.normal(13.0, 0.6, size=(T, n))) * (1 + 2 * regime[:, None]))

    frames = []
    action_rows: list[dict[str, object]] = []
    for i, sym in enumerate(symbols):
        sl = slice(int(first[i]), int(last[i]) + 1)
        frames.append(
            pd.DataFrame(
                {
                    "symbol": sym,
                    "date": dates[sl],
                    "open": np.round(open_[sl, i], 4),
                    "high": np.round(high[sl, i], 4),
                    "low": np.round(low[sl, i], 4),
                    "close": np.round(close[sl, i], 4),
                    "volume": volume[sl, i],
                }
            )
        )
        for t in range(first[i] + 1, last[i] + 1):
            if dividends[t, i] > 0:
                action_rows.append(
                    {
                        "symbol": sym,
                        "ex_date": dates[t],
                        "kind": "dividend",
                        "value": float(dividends[t, i]),
                    }
                )
            if split_ratio[t, i] != 1.0:
                action_rows.append(
                    {
                        "symbol": sym,
                        "ex_date": dates[t],
                        "kind": "split",
                        "value": float(split_ratio[t, i]),
                    }
                )

    delisted = [dates[last[i]] if last[i] < T - 1 else pd.NaT for i in range(n)]
    instruments = pd.DataFrame(
        {
            "instrument_id": [f"QLSYN{s[3:]}" for s in symbols],
            "symbol": symbols,
            "name": [f"Synthetic Asset {s[3:]}" for s in symbols],
            "sector": [SECTORS[k] for k in sector_of],
            "asset_class": "equity",
            "listed": dates[first],
            "delisted": delisted,
            "delisting_reason": [None if pd.isna(d) else "other" for d in delisted],
            "delisting_return": np.nan,
            "beta_true": np.round(beta, 4),
            "is_synthetic": True,
        }
    )

    if spec.include_benchmark:
        bench_ret = market + 0.0008 * rng.standard_normal(T)
        bclose = np.empty(T)
        bclose[0] = 100.0
        bdiv = np.zeros(T)
        for t in range(1, T):
            bdiv[t] = round(bclose[t - 1] * 0.004, 2) if t % 63 == 31 else 0.0
            bclose[t] = bclose[t - 1] * (1 + bench_ret[t]) - bdiv[t]
        bopen = np.r_[bclose[0], bclose[:-1] - bdiv[1:]] * np.exp(0.002 * rng.standard_normal(T))
        frames.append(
            pd.DataFrame(
                {
                    "symbol": BENCHMARK_SYMBOL,
                    "date": dates,
                    "open": np.round(bopen, 4),
                    "high": np.round(np.fmax(bopen, bclose) * 1.003, 4),
                    "low": np.round(np.fmin(bopen, bclose) * 0.997, 4),
                    "close": np.round(bclose, 4),
                    "volume": np.round(np.exp(rng.normal(16.0, 0.3, size=T))),
                }
            )
        )
        action_rows += [
            {
                "symbol": BENCHMARK_SYMBOL,
                "ex_date": dates[t],
                "kind": "dividend",
                "value": float(bdiv[t]),
            }
            for t in range(1, T)
            if bdiv[t] > 0
        ]
        instruments = pd.concat(
            [
                instruments,
                pd.DataFrame(
                    [
                        {
                            "instrument_id": f"QL{BENCHMARK_SYMBOL}",
                            "symbol": BENCHMARK_SYMBOL,
                            "name": "Synthetic Market Index",
                            "sector": "Index",
                            "asset_class": "index_fund",
                            "listed": dates[0],
                            "delisted": pd.NaT,
                            "delisting_reason": None,
                            "delisting_return": np.nan,
                            "beta_true": 1.0,
                            "is_synthetic": True,
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )

    bars = normalize_bars(pd.concat(frames, ignore_index=True))
    actions = normalize_actions(pd.DataFrame(action_rows) if action_rows else None)
    provenance = {
        "provider": "synthetic",
        "is_synthetic": True,
        "generator": "quantlab.data.synthetic.generate_market",
        "spec": {k: (list(v) if isinstance(v, tuple) else v) for k, v in vars(spec).items()},
        "disclaimer": "Synthetic data generated for testing and demonstration. "
        "Not real market observations.",
    }
    return SyntheticMarket(
        spec=spec,
        bars=bars,
        actions=actions,
        instruments=instruments,
        market_regime=pd.Series(regime, index=dates, name="stressed"),
        provenance=provenance,
    )
