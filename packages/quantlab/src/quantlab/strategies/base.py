"""Strategy interface and point-in-time market view.

A strategy is a pure function of a ``MarketView``: it receives only information that was
available at the close of the decision date and returns target portfolio weights (or
``None`` to keep the current portfolio). Strategies never see execution prices, fills or
future bars; the backtest engine owns all of that.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict


class StrategyParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True)
class MarketView:
    """Data visible at the close of ``date``.

    ``close`` holds total-return-adjusted closes as of ``date`` (only corporate actions with
    ex-date <= ``date`` are applied). ``returns`` are exact daily total returns.
    ``tradable`` marks symbols with a bar on ``date``, including one printed with zero
    volume; nothing fills on such a bar (#149). ``holdings`` are current portfolio weights
    at the close of ``date`` before any rebalance.
    """

    date: pd.Timestamp
    close: pd.DataFrame
    returns: pd.DataFrame
    volume: pd.DataFrame
    tradable: pd.Series
    holdings: pd.Series
    raw_close: pd.DataFrame | None = None  # as traded; for dollar volume and liquidity

    @property
    def universe(self) -> list[str]:
        return [s for s, ok in self.tradable.items() if ok]

    def history(self, n: int) -> pd.DataFrame:
        return self.close.iloc[-n:]

    def trailing_return(self, lookback: int, skip: int = 0) -> pd.Series:
        """Total return from close[t-lookback] to close[t-skip]; NaN without full history."""
        if len(self.close) <= lookback:
            return pd.Series(np.nan, index=self.close.columns)
        end = self.close.iloc[-1 - skip]
        start = self.close.iloc[-1 - lookback]
        return end / start - 1.0

    def trailing_vol(self, lookback: int) -> pd.Series:
        r = self.returns.iloc[-lookback:]
        vol = r.std(ddof=1)
        vol[r.count() < lookback] = np.nan
        return vol


class Strategy(ABC):
    key: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str]
    Params: ClassVar[type[StrategyParams]]

    def __init__(self, params: StrategyParams | dict[str, Any] | None = None) -> None:
        if params is None:
            params = {}
        self.params = params if isinstance(params, StrategyParams) else self.Params(**params)

    @property
    def warmup(self) -> int:
        """Number of sessions of history needed before the first signal."""
        return 0

    @abstractmethod
    def target_weights(self, view: MarketView) -> pd.Series | None: ...

    def describe(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "params": self.params.model_dump()}


def equal_weights(symbols: list[str]) -> pd.Series:
    if not symbols:
        return pd.Series(dtype=float)
    return pd.Series(1.0 / len(symbols), index=symbols)


def top_n(scores: pd.Series, n: int, ascending: bool = False) -> list[str]:
    s = scores.dropna()
    # Deterministic tie-break on symbol name.
    df = pd.DataFrame({"score": s, "sym": s.index})
    df = df.sort_values(["score", "sym"], ascending=[ascending, True])
    return df["sym"].head(n).tolist()
