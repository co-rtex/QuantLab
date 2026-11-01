"""Strategies for the differential tests (#180).

The engine runs the library's strategies (``quantlab.strategies.library``). The reference
runs its own implementations of the same rules, on its own view of the data, so the as-of
adjusted closes behind momentum are computed independently too. ``random_targets`` is the
exception: it is one pure function that both sides call, because its job is to exercise
the engine's handling of arbitrary targets (caps, invalid weights, non-tradable names,
"keep the portfolio"), not a signal.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from tests.oracle.reference import View


class EqualWeightRef:
    warmup = 0

    def decide(self, view: View) -> dict[str, float]:
        return {iid: 1.0 / len(view.tradable) for iid in view.tradable}


class BuyAndHoldRef:
    warmup = 0

    def decide(self, view: View) -> dict[str, float] | None:
        if any(abs(w) > 0 for w in view.holdings.values()):
            return None
        return {iid: 1.0 / len(view.tradable) for iid in view.tradable}


class MomentumRef:
    def __init__(self, lookback: int, skip: int, top_n: int) -> None:
        self.lookback, self.skip, self.top_n = lookback, skip, top_n
        self.warmup = lookback + 1

    def decide(self, view: View) -> dict[str, float]:
        score = view.trailing_return(self.lookback, self.skip)
        ranked = sorted(
            (iid for iid in view.tradable if not math.isnan(score[iid])),
            key=lambda iid: (-score[iid], iid),
        )[: self.top_n]
        return {iid: 1.0 / len(ranked) for iid in ranked}


class PriceWeightRef:
    """Weights in proportion to the as-of adjusted close, like a price-weighted index.

    It consumes price *levels*, not ratios, so it sees an as-of adjustment that uses a
    later event (a look-ahead that leaves every return unchanged). As of the decision
    session, a bar's adjusted close is its raw close: no later event is applied yet.
    """

    warmup = 0

    def decide(self, view: View) -> dict[str, float]:
        total = sum(view.close[iid] for iid in view.tradable)
        return {iid: view.close[iid] / total for iid in view.tradable}


def price_weight_engine_strategy() -> Any:
    from quantlab.strategies.base import MarketView, Strategy, StrategyParams

    class PriceWeight(Strategy):
        key = "price_weight"
        name = "Price weight (test)"
        description = "Weights in proportion to the as-of adjusted close."
        Params = StrategyParams

        def target_weights(self, view: MarketView) -> pd.Series:
            level = view.close.iloc[-1][view.universe]
            return level / level.sum()

    return PriceWeight()


def random_targets(seed: int, day: pd.Timestamp, instruments: list[str]) -> dict[str, float] | None:
    """Arbitrary targets: sometimes None (keep), sometimes over the caps or the exposure,
    with names that are not tradable today and a negative weight, which must be dropped."""
    rng = np.random.default_rng([seed, day.toordinal()])
    if rng.random() < 0.15:
        return None
    out = {}
    for iid in instruments:
        u = float(rng.random())
        if u >= 0.45:
            out[iid] = (u - 0.3) * float(rng.uniform(0.5, 1.5))
    if instruments and rng.random() < 0.2:
        out[instruments[0]] = -0.1
    return out


class RandomRef:
    warmup = 0

    def __init__(self, seed: int) -> None:
        self.seed = seed

    def decide(self, view: View) -> dict[str, float] | None:
        return random_targets(self.seed, view.date, view.instruments)


def random_engine_strategy(seed: int) -> Any:
    """The engine-side adapter for ``random_targets``."""
    from quantlab.strategies.base import MarketView, Strategy, StrategyParams

    class RandomTargets(Strategy):
        key = "random_targets"
        name = "Random targets (test)"
        description = "Arbitrary seeded targets for differential tests."
        Params = StrategyParams

        def target_weights(self, view: MarketView) -> pd.Series | None:
            out = random_targets(seed, view.date, list(view.close.columns))
            return None if out is None else pd.Series(out, dtype=float)

    return RandomTargets()
