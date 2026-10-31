"""Corporate-action adjustment with explicit point-in-time semantics.

For an event on ex-date ``t`` with split ratio ``s`` and cash dividend ``D`` (per pre-split
share), the backward price factor is::

    f_t = C_t / (s * C_t + D)

which makes adjusted close-to-close returns equal the exact total return
``(s * C_t + D) / C_{t-1} - 1`` (dividend reinvested at the ex-date close). Note that the
factor uses the ex-date close, so it is only knowable at the end of the ex-date.

**Placement.** Each action takes effect on the first bar *of its own symbol* dated on or
after its ex-date. For clean data that is the ex-date itself. For an ex-date on a
non-session day, or on a session where the symbol printed no bar, it is the symbol's next
bar: nothing traded in between, so the holders and the price discontinuity are the same,
and the factor uses a close that really is the first post-event price. Actions dated
before a symbol's first bar or after its last bar are outside its history and are not
applied (``quantlab.data.quality`` rejects the former and warns about the latter).

**Ordering.** Several actions placed on one bar are composed in ex-date order into one
equivalent event ``(S, D)`` per share held before the bar: a split multiplies the running
share count, and a dividend pays ``value`` per share held at that point. Two actions with
the *same* ex-date are ordered dividend first, so a same-day dividend is per pre-split
share (the schema convention).

Adjusting "as of" date ``a`` only uses events placed on rows <= ``a``. Because the factors
of later events scale every earlier price of a symbol by the same constant, adjusted
*returns* are identical across as-of dates; adjusted price *levels* are not. Strategies
that consume levels therefore must use the as-of view (see ``PointInTimeAdjuster``).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quantlab.data.schema import wide

# Same-ex-date tie-break: the dividend is paid before the split.
_KIND_ORDER = {"dividend": 0, "split": 1}
PLACEMENTS = ("ex_date", "next_bar", "before_history", "after_history", "no_bars")


def place_actions(close_wide: pd.DataFrame, actions: pd.DataFrame) -> pd.DataFrame:
    """Locate each action on ``close_wide``, in application order.

    Returns the actions sorted by (symbol, ex_date, dividend before split) with extra
    columns ``row`` (position in ``close_wide`` where the action takes effect, ``-1`` when
    it is not applied), ``col`` (column position, ``-1`` for a symbol absent from the
    frame) and ``placement`` (one of :data:`PLACEMENTS`; ``no_bars`` means the symbol has
    no bar at all).
    """
    out = actions[["symbol", "ex_date", "kind", "value"]].copy()
    out["_order"] = out["kind"].map(_KIND_ORDER)
    out = out.sort_values(["symbol", "ex_date", "_order"], kind="stable")
    out = out.drop(columns="_order").reset_index(drop=True)
    n = len(out)
    rows = np.full(n, -1, dtype=np.int64)
    cols = np.asarray(close_wide.columns.get_indexer(out["symbol"]), dtype=np.int64)
    placement = np.full(n, "no_bars", dtype=object)
    index = close_wide.index
    close = close_wide.to_numpy(dtype=float)
    ex = pd.DatetimeIndex(out["ex_date"])
    for c in np.unique(cols[cols >= 0]):
        bar_rows = np.flatnonzero(np.isfinite(close[:, c]))
        if len(bar_rows) == 0:
            continue
        sel = np.flatnonzero(cols == c)
        bar_dates = index[bar_rows]
        e = ex[sel]
        pos = bar_dates.searchsorted(e, side="left")
        inside = (e >= bar_dates[0]) & (pos < len(bar_rows))
        r = np.where(inside, bar_rows[np.minimum(pos, len(bar_rows) - 1)], -1)
        rows[sel] = r
        on_ex = inside & (index[np.maximum(r, 0)] == e)
        outside = np.where(e < bar_dates[0], "before_history", "after_history")
        placement[sel] = np.where(inside, np.where(on_ex, "ex_date", "next_bar"), outside)
    out["row"] = rows
    out["col"] = cols
    out["placement"] = placement
    return out


def event_matrices(
    close_wide: pd.DataFrame, actions: pd.DataFrame
) -> tuple[np.ndarray, np.ndarray]:
    """Return (split_ratio, dividend) matrices aligned with ``close_wide``.

    Entry ``[t, j]`` is the equivalent event of every action placed on that bar (see the
    module docstring): ``S`` new shares and ``D`` cash per share held before the bar.
    """
    return _matrices(close_wide.shape, place_actions(close_wide, actions))


def _matrices(shape: tuple[int, int], placed: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    splits = np.ones(shape)
    divs = np.zeros(shape)
    applied = placed[placed["row"] >= 0]
    for r, c, kind, v in zip(
        applied["row"].to_numpy(),
        applied["col"].to_numpy(),
        applied["kind"].to_numpy(),
        applied["value"].to_numpy(),
        strict=True,
    ):
        if kind == "split":
            splits[r, c] *= v
        else:
            # ``v`` is paid per share held now, i.e. after the splits already composed.
            divs[r, c] += splits[r, c] * v
    return splits, divs


class PointInTimeAdjuster:
    """Precomputes reverse-cumulative factors so as-of adjustment is O(1) per date."""

    def __init__(self, close_wide: pd.DataFrame, actions: pd.DataFrame) -> None:
        self.index = close_wide.index
        self.columns = close_wide.columns
        self.placed = place_actions(close_wide, actions)
        self.splits, self.dividends = _matrices(close_wide.shape, self.placed)
        # Every event sits on a row where its symbol has a bar, so ``close`` there is the
        # real first post-event price; rows without a bar carry no event and get f = 1.
        close = close_wide.to_numpy(dtype=float)
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
    """Date x symbol matrix of adjusted closes using only events placed on or before as_of."""
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
    """Exact total returns ``(S_t*C_t + D_t)/C_prev - 1`` between consecutive bars.

    ``C_prev`` is the symbol's previous bar. After a gap that is several sessions back, so
    the first bar after a gap carries the return over the whole gap, including any action
    placed on it. This keeps the compounded returns equal to the ratio of adjusted closes.
    Rows where the symbol has no bar, and its first bar, are NaN.
    """
    close = wide(bars, "close")
    splits, divs = event_matrices(close, actions)
    c = close.to_numpy(dtype=float)
    prev = close.ffill().shift(1).to_numpy(dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = (splits * c + divs) / prev - 1.0
    return pd.DataFrame(r, index=close.index, columns=close.columns)
