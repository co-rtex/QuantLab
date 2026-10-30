"""Corporate-action adjustment with explicit point-in-time semantics.

For an event on ex-date ``t`` with split ratio ``s`` and cash dividend ``D`` (per pre-split
share), the backward price factor is::

    f_t = C_t / (s * C_t + D)

which makes adjusted close-to-close returns equal the exact total return
``(s * C_t + D) / C_{t-1} - 1`` (dividend reinvested at the ex-date close). Note that the
factor uses the ex-date close, so it is only knowable at the end of the ex-date.

Adjusting "as of" date ``a`` only uses events with ex-date <= ``a``. Because the factors of
later events scale every earlier price of a symbol by the same constant, adjusted
*returns* are identical across as-of dates; adjusted price *levels* are not. Strategies
that consume levels therefore must use the as-of view (see ``PointInTimeAdjuster``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quantlab.data.schema import wide


def event_matrices(
    close_wide: pd.DataFrame, actions: pd.DataFrame
) -> tuple[np.ndarray, np.ndarray]:
    """Return (split_ratio, dividend) matrices aligned with ``close_wide``.

    An action whose ex-date is not a row of ``close_wide`` (a non-session date, or a date
    outside a filtered universe's sessions) takes effect at the first row on or after its
    ex-date: nothing trades in between, so the holders and the price discontinuity are the
    same. Dropping it instead would book a split as a loss. Actions before the first row or
    after the last one are outside the history and ignored.
    """
    T, n = close_wide.shape
    splits = np.ones((T, n))
    divs = np.zeros((T, n))
    if len(actions) == 0 or T == 0:
        return splits, divs
    ex = pd.DatetimeIndex(actions["ex_date"])
    row = close_wide.index.searchsorted(ex, side="left")
    row = np.where((ex < close_wide.index[0]) | (row >= T), -1, row)
    col = close_wide.columns.get_indexer(actions["symbol"])
    ok = (row >= 0) & (col >= 0)
    for r, c, kind, v in zip(
        row[ok],
        col[ok],
        actions["kind"].to_numpy()[ok],
        actions["value"].to_numpy()[ok],
        strict=True,
    ):
        if kind == "split":
            splits[r, c] *= v
        else:
            divs[r, c] += v
    return splits, divs


class PointInTimeAdjuster:
    """Precomputes reverse-cumulative factors so as-of adjustment is O(1) per date."""

    def __init__(self, close_wide: pd.DataFrame, actions: pd.DataFrame) -> None:
        self.index = close_wide.index
        self.columns = close_wide.columns
        # On an event row where the symbol printed no bar, the last known close stands in for
        # the ex-date close (still point-in-time); without it the factor would be lost.
        close = close_wide.ffill().to_numpy(dtype=float)
        self.splits, self.dividends = event_matrices(close_wide, actions)
        with np.errstate(invalid="ignore", divide="ignore"):
            f = close / (self.splits * close + self.dividends)
        f = np.where(np.isfinite(f) & (f > 0), f, 1.0)
        # rev[t] = prod_{u >= t} f[u]; rev[T] = 1
        T = len(close)
        rev = np.ones((T + 1, close.shape[1]))
        rev[:T] = np.cumprod(f[::-1], axis=0)[::-1]
        self._rev = rev

    def multiplier(self, as_of_pos: int | None = None) -> np.ndarray:
        """Per-date multiplier matrix (T x n) for adjustment as of row ``as_of_pos``."""
        full = self._rev[1:]  # date d uses events strictly after d
        if as_of_pos is None:
            return full
        return full / self._rev[as_of_pos + 1]

    def scale_as_of(self, as_of_pos: int) -> np.ndarray:
        """Vector dividing fully-adjusted prices into as-of-adjusted prices."""
        return self._rev[as_of_pos + 1]


def adjusted_close(
    bars: pd.DataFrame, actions: pd.DataFrame, as_of: pd.Timestamp | None = None
) -> pd.DataFrame:
    """Date x symbol matrix of adjusted closes using only events with ex-date <= as_of."""
    close = wide(bars, "close")
    adj = PointInTimeAdjuster(close, actions)
    if as_of is None:
        pos = None
    else:
        pos = int(close.index.searchsorted(pd.Timestamp(as_of), side="right")) - 1
        close = close.iloc[: pos + 1]
    mult = adj.multiplier(pos)[: len(close)]
    return pd.DataFrame(close.to_numpy() * mult, index=close.index, columns=close.columns)


def total_returns(bars: pd.DataFrame, actions: pd.DataFrame) -> pd.DataFrame:
    """Exact daily total returns ``(s*C_t + D_t)/C_{t-1} - 1`` (NaN where undefined)."""
    close = wide(bars, "close")
    splits, divs = event_matrices(close, actions)
    c = close.to_numpy()
    prev = np.vstack([np.full((1, c.shape[1]), np.nan), c[:-1]])
    with np.errstate(invalid="ignore", divide="ignore"):
        r = (splits * c + divs) / prev - 1.0
    return pd.DataFrame(r, index=close.index, columns=close.columns)
