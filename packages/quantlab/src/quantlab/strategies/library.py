"""Built-in strategies. All are long-only and deterministic given the same view."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from quantlab.optimize.allocators import Bounds, OptimizationError, min_variance, risk_parity
from quantlab.strategies.base import (
    MarketView,
    Strategy,
    StrategyParams,
    equal_weights,
    top_n,
)


class BuyAndHoldParams(StrategyParams):
    weights: dict[str, float] | None = Field(
        default=None, description="Initial weights; equal weight across the universe if omitted"
    )

    @model_validator(mode="after")
    def _check(self) -> BuyAndHoldParams:
        if self.weights is not None:
            if any(w < 0 for w in self.weights.values()):
                raise ValueError("weights must be non-negative")
            if sum(self.weights.values()) > 1 + 1e-9:
                raise ValueError("weights must sum to at most 1")
        return self


class BuyAndHold(Strategy):
    key = "buy_and_hold"
    name = "Buy and Hold"
    description = "Invest once at the first decision date and never rebalance."
    Params = BuyAndHoldParams

    def target_weights(self, view: MarketView) -> pd.Series | None:
        if (view.holdings.abs() > 0).any():
            return None
        p: BuyAndHoldParams = self.params  # type: ignore[assignment]
        if p.weights:
            w = pd.Series({k.upper(): v for k, v in p.weights.items()})
            return w[w.index.isin(view.universe)]
        return equal_weights(view.universe)


class EqualWeightParams(StrategyParams):
    pass


class EqualWeight(Strategy):
    key = "equal_weight"
    name = "Equal Weight"
    description = "Hold every tradable asset at 1/N, restored on each rebalance date."
    Params = EqualWeightParams

    def target_weights(self, view: MarketView) -> pd.Series | None:
        return equal_weights(view.universe)


class SMACrossoverParams(StrategyParams):
    fast: int = Field(50, ge=2, le=400)
    slow: int = Field(200, ge=3, le=600)

    @model_validator(mode="after")
    def _order(self) -> SMACrossoverParams:
        if self.fast >= self.slow:
            raise ValueError("fast window must be shorter than slow window")
        return self


class SMACrossover(Strategy):
    key = "sma_crossover"
    name = "Moving-Average Crossover"
    description = (
        "Equal-weight the assets whose fast SMA is above their slow SMA; remaining capital "
        "stays in cash."
    )
    Params = SMACrossoverParams

    @property
    def warmup(self) -> int:
        return int(self.params.slow)  # type: ignore[attr-defined]

    def target_weights(self, view: MarketView) -> pd.Series | None:
        p: SMACrossoverParams = self.params  # type: ignore[assignment]
        hist = view.history(p.slow)
        if len(hist) < p.slow:
            return pd.Series(dtype=float)
        full = hist.notna().all()
        fast = hist.iloc[-p.fast :].mean()
        slow = hist.mean()
        selected = [s for s in view.universe if full.get(s, False) and fast[s] > slow[s]]
        n = len(view.universe)
        if not selected or n == 0:
            return pd.Series(dtype=float)
        # Each asset gets a fixed 1/N sleeve; inactive sleeves are held in cash.
        return pd.Series(1.0 / n, index=selected)


class MomentumParams(StrategyParams):
    lookback: int = Field(252, ge=20, le=756)
    skip: int = Field(21, ge=0, le=63)
    top_n: int = Field(10, ge=1, le=500)

    @model_validator(mode="after")
    def _order(self) -> MomentumParams:
        if self.skip >= self.lookback:
            raise ValueError("skip must be shorter than lookback")
        return self


class CrossSectionalMomentum(Strategy):
    key = "momentum"
    name = "Cross-Sectional Momentum"
    description = (
        "Rank assets by total return over [t-lookback, t-skip] and equal-weight the top N "
        "(Jegadeesh-Titman style, skipping the most recent month)."
    )
    Params = MomentumParams

    @property
    def warmup(self) -> int:
        return int(self.params.lookback) + 1  # type: ignore[attr-defined]

    def target_weights(self, view: MarketView) -> pd.Series | None:
        p: MomentumParams = self.params  # type: ignore[assignment]
        score = view.trailing_return(p.lookback, p.skip)
        score = score[score.index.isin(view.universe)]
        return equal_weights(top_n(score, p.top_n))


class MeanReversionParams(StrategyParams):
    lookback: int = Field(5, ge=1, le=63)
    bottom_n: int = Field(10, ge=1, le=500)


class MeanReversion(Strategy):
    key = "mean_reversion"
    name = "Short-Term Mean Reversion"
    description = "Equal-weight the N worst performers over the lookback window (reversal)."
    Params = MeanReversionParams

    @property
    def warmup(self) -> int:
        return int(self.params.lookback) + 1  # type: ignore[attr-defined]

    def target_weights(self, view: MarketView) -> pd.Series | None:
        p: MeanReversionParams = self.params  # type: ignore[assignment]
        score = view.trailing_return(p.lookback)
        score = score[score.index.isin(view.universe)]
        return equal_weights(top_n(score, p.bottom_n, ascending=True))


class LowVolParams(StrategyParams):
    lookback: int = Field(63, ge=10, le=504)
    top_n: int = Field(10, ge=1, le=500)
    weighting: Literal["equal", "inverse_vol"] = "inverse_vol"


class LowVolatility(Strategy):
    key = "low_volatility"
    name = "Low Volatility"
    description = "Hold the N assets with the lowest trailing volatility."
    Params = LowVolParams

    @property
    def warmup(self) -> int:
        return int(self.params.lookback) + 1  # type: ignore[attr-defined]

    def target_weights(self, view: MarketView) -> pd.Series | None:
        p: LowVolParams = self.params  # type: ignore[assignment]
        vol = view.trailing_vol(p.lookback)
        vol = vol[vol.index.isin(view.universe) & (vol > 0)]
        chosen = top_n(vol, p.top_n, ascending=True)
        if not chosen:
            return pd.Series(dtype=float)
        if p.weighting == "equal":
            return equal_weights(chosen)
        inv = 1.0 / vol[chosen]
        return inv / inv.sum()


class CovarianceParams(StrategyParams):
    lookback: int = Field(126, ge=20, le=756)
    max_assets: int = Field(30, ge=2, le=200)
    max_weight: float = Field(0.25, gt=0, le=1)


def _cov_universe(view: MarketView, lookback: int, max_assets: int) -> tuple[list[str], np.ndarray]:
    r = view.returns.iloc[-lookback:]
    ok = [s for s in view.universe if r[s].notna().sum() == lookback] if len(r) == lookback else []
    if not ok:
        return [], np.empty((0, 0))
    # Keep the cross-section manageable: most liquid names by trailing dollar volume.
    if len(ok) > max_assets:
        # Dollar volume needs raw (as traded) prices: volume is in shares as traded, so
        # pairing it with split-adjusted closes would understate pre-split liquidity.
        px = view.raw_close if view.raw_close is not None else view.close
        dv = (view.volume.iloc[-lookback:][ok] * px.iloc[-lookback:][ok]).mean()
        ok = top_n(dv, max_assets)
        ok.sort()
    cov = r[ok].cov(ddof=1).to_numpy() * 252
    return ok, cov


class MinimumVariance(Strategy):
    key = "min_variance"
    name = "Minimum Variance"
    description = "Long-only minimum-variance portfolio on trailing sample covariance."
    Params = CovarianceParams

    @property
    def warmup(self) -> int:
        return int(self.params.lookback) + 1  # type: ignore[attr-defined]

    def target_weights(self, view: MarketView) -> pd.Series | None:
        p: CovarianceParams = self.params  # type: ignore[assignment]
        syms, cov = _cov_universe(view, p.lookback, p.max_assets)
        if not syms:
            return pd.Series(dtype=float)
        cap = max(p.max_weight, 1.0 / len(syms))
        try:
            w = min_variance(cov, Bounds(0.0, cap))
        except OptimizationError:
            return None  # keep current holdings rather than trade on a failed solve
        return pd.Series(w, index=syms)


class RiskParity(Strategy):
    key = "risk_parity"
    name = "Risk Parity"
    description = "Equal risk contribution weights from trailing sample covariance."
    Params = CovarianceParams

    @property
    def warmup(self) -> int:
        return int(self.params.lookback) + 1  # type: ignore[attr-defined]

    def target_weights(self, view: MarketView) -> pd.Series | None:
        p: CovarianceParams = self.params  # type: ignore[assignment]
        syms, cov = _cov_universe(view, p.lookback, p.max_assets)
        if not syms:
            return pd.Series(dtype=float)
        try:
            w = risk_parity(cov)
        except OptimizationError:
            return None
        return pd.Series(w, index=syms)


class VolTargetParams(StrategyParams):
    target_vol: float = Field(0.10, gt=0.0, le=1.0)
    lookback: int = Field(63, ge=10, le=504)
    max_exposure: float = Field(1.0, gt=0.0, le=1.0)


class VolatilityTarget(Strategy):
    key = "vol_target"
    name = "Volatility-Targeted Equal Weight"
    description = (
        "Equal-weight portfolio scaled so its trailing realized volatility matches the "
        "target; the remainder is held in cash (no leverage)."
    )
    Params = VolTargetParams

    @property
    def warmup(self) -> int:
        return int(self.params.lookback) + 1  # type: ignore[attr-defined]

    def target_weights(self, view: MarketView) -> pd.Series | None:
        p: VolTargetParams = self.params  # type: ignore[assignment]
        uni = view.universe
        r = view.returns.iloc[-p.lookback :][uni]
        if len(r) < p.lookback or not uni:
            return pd.Series(dtype=float)
        port = r.mean(axis=1, skipna=True)
        realized = float(port.std(ddof=1) * np.sqrt(252))
        scale = p.max_exposure if realized <= 0 else min(p.max_exposure, p.target_vol / realized)
        return equal_weights(uni) * scale


REGISTRY: dict[str, type[Strategy]] = {
    cls.key: cls
    for cls in (
        BuyAndHold,
        EqualWeight,
        SMACrossover,
        CrossSectionalMomentum,
        MeanReversion,
        LowVolatility,
        MinimumVariance,
        RiskParity,
        VolatilityTarget,
    )
}


class UnknownStrategy(KeyError):
    pass


def create_strategy(key: str, params: dict[str, Any] | None = None) -> Strategy:
    cls = REGISTRY.get(key)
    if cls is None:
        raise UnknownStrategy(key)
    return cls(params or {})


def catalog() -> list[dict[str, Any]]:
    return [
        {
            "key": cls.key,
            "name": cls.name,
            "description": cls.description,
            "params_schema": cls.Params.model_json_schema(),
        }
        for cls in REGISTRY.values()
    ]
