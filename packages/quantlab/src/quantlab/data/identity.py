"""Permanent instrument identity and point-in-time symbol history.

A ticker is not an identity. Issuers change tickers (a rename keeps the security), and
exchanges hand retired tickers to unrelated issuers (reuse). Keying research data by
ticker therefore splits one security into two series at a rename, which the engine sees as
a delisting followed by a new listing, and glues two securities into one series at a
reuse.

Research data is keyed by a permanent ``instrument_id`` instead. A **symbol history** maps
tickers to instruments with half-open validity intervals ``[valid_from, valid_to)``
(``valid_to`` NaT = still valid):

    instrument_id  symbol  valid_from   valid_to
    I1             FB      2012-05-18   2022-06-09
    I1             META    2022-06-09   NaT

Rules, enforced by :func:`normalize_history`: at any instant a ticker names at most one
instrument, and an instrument trades under at most one ticker.

Resolution is point-in-time: the ticker for an instrument (or the instrument for a ticker)
as of date ``d`` uses only an interval with ``valid_from <= d < valid_to``, never one that
starts after ``d``.

:func:`key_by_instrument` re-keys ticker-keyed vendor bars and actions so that the
``symbol`` column holds the instrument id. The engine, strategies, ML and analytics treat
``symbol`` as an opaque series key, so they run unchanged on the re-keyed frames, and
tickers become display labels (:func:`symbols_as_of`).

The **instrument master** carries listing and delisting facts per instrument:
``listing_date``, ``delisting_date``, ``delisting_reason`` and ``delisting_return`` (the
return from the last trade to the final distribution or the first post-delisting quote,
when the vendor supplies it; NaN = unknown).

Datasets without identity data use :func:`legacy_history`, under which every ticker is its
own permanent instrument (``instrument_id == symbol``), so re-keying is the identity map.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

from quantlab.data.schema import (
    EXCHANGE_TZ,
    SYMBOL_PATTERN,
    SchemaError,
    normalize_actions,
    normalize_bars,
)

HISTORY_COLUMNS = ["instrument_id", "symbol", "valid_from", "valid_to"]
MASTER_COLUMNS = [
    "instrument_id",
    "name",
    "listing_date",
    "delisting_date",
    "delisting_reason",
    "delisting_return",
]
DELISTING_REASONS = (
    "merger",
    "exchange_change",
    "liquidation",
    "bankruptcy",
    "noncompliance",
    "voluntary",
    "other",
)
# Delisting reasons that indicate failure rather than a corporate transaction. Returns for
# these are typically large losses (Shumway 1997 estimates about -30% on NYSE/AMEX).
PERFORMANCE_REASONS = ("bankruptcy", "liquidation", "noncompliance")
# Start of the open-ended interval used for datasets without identity data.
LEGACY_FROM = pd.Timestamp("1900-01-01")
SAMPLE = 5


class IdentityError(SchemaError):
    """Raised when identity data is inconsistent or data cannot be keyed by instrument."""


def _ids(series: pd.Series, what: str) -> pd.Series:
    out = series.astype(str).str.upper().str.strip()
    bad = ~out.str.fullmatch(SYMBOL_PATTERN)
    if bad.any():
        raise IdentityError(f"invalid {what}: {sorted(out[bad].unique())[:SAMPLE]}")
    return out


def _dates(series: pd.Series, what: str) -> pd.Series:
    try:
        parsed = pd.to_datetime(series)
        if parsed.dt.tz is not None:  # same rule as bars: the New York calendar date
            parsed = parsed.dt.tz_convert(EXCHANGE_TZ).dt.tz_localize(None)
        return parsed.dt.normalize().astype("datetime64[ns]")
    except (ValueError, TypeError) as exc:
        raise IdentityError(f"{what} is not a date: {exc}") from exc


def _merge_adjacent(h: pd.DataFrame) -> pd.DataFrame:
    """Join back-to-back intervals of one (instrument, ticker) pair.

    Vendor name files often split a row at an exchange or name change that keeps the
    ticker; the identity is the same, so it must canonicalize (and hash) the same way.
    """
    h = h.sort_values(["instrument_id", "symbol", "valid_from"], kind="stable")
    key = ["instrument_id", "symbol"]
    prev_end = h.groupby(key)["valid_to"].shift(1)
    starts = ~(h.groupby(key).cumcount() > 0) | (prev_end != h["valid_from"])
    run = starts.cumsum()
    return (
        h.groupby(run, sort=False)
        .agg(
            instrument_id=("instrument_id", "first"),
            symbol=("symbol", "first"),
            valid_from=("valid_from", "first"),
            # ``last`` would skip NaT, which here means "still valid".
            valid_to=("valid_to", lambda s: s.iloc[-1]),
        )
        .reset_index(drop=True)
    )


def _overlaps(history: pd.DataFrame, key: str) -> pd.DataFrame:
    """Rows whose interval starts before the previous interval of the same ``key`` ends."""
    h = history.sort_values([key, "valid_from"], kind="stable")
    prev_end = h.groupby(key)["valid_to"].shift(1)
    has_prev = h.groupby(key).cumcount() > 0
    clash = has_prev & (prev_end.isna() | (prev_end > h["valid_from"]))
    return h[clash]


def normalize_history(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate and canonicalize a symbol history (sorted by instrument, then start)."""
    missing = {"instrument_id", "symbol", "valid_from"} - set(frame.columns)
    if missing:
        raise IdentityError(f"symbol history missing columns: {sorted(missing)}")
    h = frame.copy()
    h["instrument_id"] = _ids(h["instrument_id"], "instrument ids")
    h["symbol"] = _ids(h["symbol"], "symbols")
    h["valid_from"] = _dates(h["valid_from"], "valid_from")
    if h["valid_from"].isna().any():
        raise IdentityError("valid_from is required")
    h["valid_to"] = _dates(
        h["valid_to"] if "valid_to" in h else pd.Series(pd.NaT, index=h.index), "valid_to"
    )
    empty = h["valid_to"].notna() & (h["valid_to"] <= h["valid_from"])
    if empty.any():
        raise IdentityError(f"empty validity intervals: {_rows(h[empty])}")
    repeated = h.duplicated(HISTORY_COLUMNS, keep=False)
    if repeated.any():
        raise IdentityError(f"duplicate symbol history rows: {_rows(h[repeated])}")
    h = _merge_adjacent(h[HISTORY_COLUMNS])
    for key, rule in (
        ("symbol", "a ticker maps to two instruments at once"),
        ("instrument_id", "an instrument has two tickers at once"),
    ):
        clash = _overlaps(h, key)
        if len(clash):
            raise IdentityError(f"overlapping intervals ({rule}): {_rows(clash)}")
    h = h[HISTORY_COLUMNS].sort_values(["instrument_id", "valid_from"], kind="stable")
    return h.reset_index(drop=True)


def normalize_master(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate and canonicalize an instrument master (one row per instrument)."""
    if "instrument_id" not in frame.columns:
        raise IdentityError("instrument master missing column: instrument_id")
    m = frame.copy()
    m["instrument_id"] = _ids(m["instrument_id"], "instrument ids")
    if m["instrument_id"].duplicated().any():
        raise IdentityError("instrument master has duplicate instrument ids")
    for col in ("listing_date", "delisting_date"):
        m[col] = _dates(m[col] if col in m else pd.Series(pd.NaT, index=m.index), col)
    m["name"] = m["name"].astype(str) if "name" in m else m["instrument_id"]
    reason = m["delisting_reason"] if "delisting_reason" in m else pd.Series(None, index=m.index)
    m["delisting_reason"] = reason.where(reason.notna(), None)
    known = m["delisting_reason"].notna()
    bad = known & ~m["delisting_reason"].isin(DELISTING_REASONS)
    if bad.any():
        raise IdentityError(
            f"unknown delisting reasons {sorted(m.loc[bad, 'delisting_reason'].unique())}; "
            f"expected one of {DELISTING_REASONS}"
        )
    ret = m["delisting_return"] if "delisting_return" in m else pd.Series(np.nan, index=m.index)
    try:
        # A malformed value (e.g. "-30%") must not silently become "unknown", which would
        # take the optimistic last-close exit.
        # Strip strings element by element; .str would turn non-string values into NaN.
        cleaned = ret.map(lambda v: (v.strip() or None) if isinstance(v, str) else v)
        m["delisting_return"] = pd.to_numeric(cleaned).astype("float64")
    except (ValueError, TypeError) as exc:
        raise IdentityError(f"delisting_return is not a number: {exc}") from exc
    r = m["delisting_return"]
    if (r.notna() & ~((r >= -1.0) & np.isfinite(r))).any():
        raise IdentityError("delisting_return must be a finite return >= -1")
    after = m["delisting_date"].notna() & m["listing_date"].notna()
    if (after & (m["delisting_date"] < m["listing_date"])).any():
        raise IdentityError("delisting_date before listing_date")
    orphan = m["delisting_date"].isna() & (known | r.notna())
    if orphan.any():
        raise IdentityError("delisting reason or return given without a delisting_date")
    return m[MASTER_COLUMNS].sort_values("instrument_id").reset_index(drop=True)


def master_as_of(
    master: pd.DataFrame, when: pd.Timestamp | str, keep: Iterable[str] = ()
) -> pd.DataFrame:
    """The instrument master as it was knowable at the close of ``when`` (#247).

    A delisting is public on its date, so one dated on or before ``when`` is kept, with its
    reason and return. A later one is not known yet: its date, reason and return are
    blanked, as for a security still listed. Listing dates and names are kept. The engine
    does not read them, and a listing shows in the bars anyway.

    An instrument in ``keep`` whose delisting is not public yet keeps only the fact that it
    delists later: its date becomes the day after ``when``, so nothing can act on it at or
    before ``when``, and its reason and return are blanked like any other.
    """
    m = normalize_master(master)
    when = pd.Timestamp(when)
    later = m["delisting_date"] > when
    pending = later & m["instrument_id"].isin(set(keep))
    m.loc[later & ~pending, "delisting_date"] = pd.NaT
    m.loc[pending, "delisting_date"] = when + pd.Timedelta(days=1)
    m.loc[later, "delisting_reason"] = None
    m.loc[later, "delisting_return"] = np.nan
    return m


def _rows(frame: pd.DataFrame) -> list[dict[str, object]]:
    out = frame.head(SAMPLE).astype(object).where(frame.head(SAMPLE).notna(), None)
    return [
        {k: (str(v) if isinstance(v, pd.Timestamp) else v) for k, v in r.items()}
        for r in out.to_dict("records")
    ]


def legacy_history(symbols: pd.Series | list[str]) -> pd.DataFrame:
    """Compatibility history: every ticker is its own instrument, valid from 1900 onward."""
    syms = sorted(set(pd.Series(symbols, dtype=str)))
    return normalize_history(
        pd.DataFrame(
            {"instrument_id": syms, "symbol": syms, "valid_from": LEGACY_FROM, "valid_to": pd.NaT}
        )
    )


def is_legacy(history: pd.DataFrame) -> bool:
    """True when ``history`` is the identity map that :func:`legacy_history` produces."""
    return bool(
        (history["instrument_id"] == history["symbol"]).all()
        and not history["symbol"].duplicated().any()
        and (history["valid_from"] == LEGACY_FROM).all()
        and history["valid_to"].isna().all()
    )


def _resolve(
    history: pd.DataFrame,
    by: str,
    target: str,
    keys: pd.Series,
    dates: pd.Series,
    *,
    open_only: bool = True,
) -> pd.Series:
    """For each (key, date), the ``target`` of the interval of ``by`` = key covering date.

    With ``open_only=False`` the latest interval that *started* on or before the date is
    used even if it has ended (the last ticker a delisted security had), for display only.
    """
    # merge_asof needs identical key dtypes on both sides.
    left = (
        pd.DataFrame(
            {
                by: keys.astype(str).str.strip().str.upper().to_numpy(),
                "_d": pd.to_datetime(dates).astype("datetime64[ns]").to_numpy(),
                "_pos": np.arange(len(keys)),
            }
        )
        .astype({by: str})
        .sort_values("_d", kind="stable")
    )
    right = history[[by, target, "valid_from", "valid_to"]].astype(
        {by: str, "valid_from": "datetime64[ns]"}
    )
    right = right.sort_values("valid_from", kind="stable")
    # Latest interval of this key starting on or before the date; intervals of one key
    # never overlap, so it is the only candidate, and it must still be open on the date.
    m = pd.merge_asof(left, right, left_on="_d", right_on="valid_from", by=by, direction="backward")
    live = m["valid_from"].notna()
    if open_only:
        live &= m["valid_to"].isna() | (m["_d"] < m["valid_to"])
    out = pd.Series(np.where(live, m[target], None), index=m["_pos"].to_numpy(), dtype=object)
    return out.sort_index().set_axis(keys.index)


def resolve_ids(history: pd.DataFrame, symbols: pd.Series, dates: pd.Series) -> pd.Series:
    """Instrument id of each (ticker, date), or None when no interval covers the date."""
    return _resolve(history, "symbol", "instrument_id", symbols, dates)


def symbols_as_of(history: pd.DataFrame, ids: pd.Series, dates: pd.Series) -> pd.Series:
    """Ticker of each (instrument, date), or None when it has no ticker on that date."""
    return _resolve(history, "instrument_id", "symbol", ids, dates)


def resolve(history: pd.DataFrame, symbol: str, as_of: pd.Timestamp | str) -> str | None:
    """Instrument named by ``symbol`` as of ``as_of`` (point-in-time)."""
    out = resolve_ids(history, pd.Series([symbol]), pd.Series([pd.Timestamp(as_of)]))
    v = out.iloc[0]
    return None if v is None else str(v)


def key_by_instrument(
    bars: pd.DataFrame, actions: pd.DataFrame, history: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Re-key ticker-keyed bars and actions by instrument id (in the ``symbol`` column).

    Each bar is resolved as of its session date and each action as of its ex-date. Data
    that no interval covers, or two tickers of one instrument printing on the same
    session, raise :class:`IdentityError` with a sample: guessing would silently merge or
    split histories. Inputs are normalized first, so raw vendor frames (time-zone-aware
    timestamps included) resolve on their session dates.
    """
    bars, actions = normalize_bars(bars), normalize_actions(actions)
    if actions["ex_date"].isna().any():
        raise IdentityError("corporate actions without an ex_date cannot be keyed")
    same_ticker = bars.duplicated(["symbol", "date"], keep=False)
    if same_ticker.any():
        raise IdentityError(
            f"duplicate bars for one ticker and session: "
            f"{_rows(bars.loc[same_ticker, ['symbol', 'date']])}"
        )
    bar_ids = resolve_ids(history, bars["symbol"], bars["date"])
    unmapped = bar_ids.isna()
    if unmapped.any():
        raise IdentityError(
            f"{int(unmapped.sum())} bars have no instrument for their ticker and date: "
            f"{_rows(bars.loc[unmapped, ['symbol', 'date']])}"
        )
    keyed = bars.assign(symbol=bar_ids.astype(str))
    dup = keyed.duplicated(["symbol", "date"], keep=False)
    if dup.any():
        raise IdentityError(
            "one instrument has bars under two tickers on the same session: "
            f"{_rows(bars.loc[dup, ['symbol', 'date']])}"
        )
    act_ids = resolve_ids(history, actions["symbol"], actions["ex_date"])
    unmapped = act_ids.isna()
    if unmapped.any():
        raise IdentityError(
            f"{int(unmapped.sum())} corporate actions have no instrument for their ticker "
            f"and ex-date: {_rows(actions.loc[unmapped, ['symbol', 'ex_date', 'kind']])}"
        )
    return (
        normalize_bars(keyed),
        normalize_actions(actions.assign(symbol=act_ids.astype(str))),
    )


def resolve_universe(
    history: pd.DataFrame,
    tickers: list[str],
    as_of: pd.Timestamp | str,
    until: pd.Timestamp | str | None = None,
) -> tuple[list[str], list[str]]:
    """Instrument ids of ``tickers`` for a backtest starting ``as_of``, and the tickers that
    name no instrument in the window.

    A ticker means the instrument it names **on the start date**, never "today": a
    security renamed later stays in under its later ticker, and an issuer that takes over
    the ticker later is not pulled in. A ticker that has never named anything by the
    start (a later listing) means the first instrument to take it within the window (up to
    ``until``), which then simply joins when it lists, as in an unkeyed dataset, but only
    if that is the instrument's first-ever ticker: a security renamed into the ticker later
    was trading under another name at the start and is not selected by its later name. A
    ticker that named another security before the start but nothing on it (retired, or in
    a gap before a reuse) is unresolved: its later holder would be a reuse.
    """
    when = pd.Timestamp(as_of)
    syms = pd.Series([str(t).upper().strip() for t in tickers])
    ids = resolve_ids(history, syms, pd.Series([when] * len(syms)))
    for k in np.flatnonzero(ids.isna().to_numpy()):
        uses = history[history["symbol"] == syms.iloc[k]]
        if (uses["valid_from"] <= when).any():
            continue  # it named another security before the start: a later holder is a reuse
        later = uses[uses["valid_from"] > when]
        if until is not None:
            later = later[later["valid_from"] <= pd.Timestamp(until)]
        if not len(later):
            continue
        first = later.sort_values("valid_from").iloc[0]
        # Only a genuine new listing: the ticker must be the instrument's first-ever ticker.
        # A security renamed *into* it was trading under another ticker at the start, and
        # taking it from the start would select it by its later name (today's-ticker bias).
        own = history[history["instrument_id"] == first["instrument_id"]]
        if own["valid_from"].min() == first["valid_from"]:
            ids.iloc[k] = first["instrument_id"]
    resolved = sorted({str(i) for i in ids.dropna()})
    unresolved = sorted(syms[ids.isna().to_numpy()].tolist())
    return resolved, unresolved


def label_ids(history: pd.DataFrame, ids: list[str], when: pd.Timestamp | str) -> dict[str, str]:
    """A display ticker for each instrument as of ``when``, never a later one if avoidable.

    The ticker valid on ``when``; else the last one it had before (it has delisted); else
    its first ticker (it lists later); else the id itself.
    """
    if not ids:
        return {}
    keys = pd.Series([str(i) for i in ids])
    dates = pd.Series([pd.Timestamp(when)] * len(keys))
    valid = symbols_as_of(history, keys, dates)
    last = _resolve(history, "instrument_id", "symbol", keys, dates, open_only=False)
    first = history.sort_values("valid_from").groupby("instrument_id")["symbol"].first()
    out = {}
    for k, v, lt in zip(keys, valid, last, strict=True):
        out[k] = str(v if v is not None else lt if lt is not None else first.get(k, k))
    return out


def tickers_of(history: pd.DataFrame, ids: list[str]) -> list[str]:
    """Every ticker the instruments ever traded under (to load their full history)."""
    wanted = {str(i).upper() for i in ids}
    return sorted(set(history.loc[history["instrument_id"].isin(wanted), "symbol"]))


def display_frame(
    frame: pd.DataFrame, history: pd.DataFrame, date_col: str = "date"
) -> pd.DataFrame:
    """Turn an id-keyed result frame back into tickers for display.

    ``symbol`` becomes the ticker valid on each row's date and the permanent id moves to
    an ``instrument_id`` column. Under the legacy map both are the same value. A row dated
    after the instrument's last ticker ended (a delisting fill, say) shows that last ticker;
    one with no ticker at all keeps the id.
    """
    if "symbol" not in frame.columns:
        return frame
    out = frame.copy()
    ids = out["symbol"].astype(str)
    if len(out) and date_col in out.columns:
        when = pd.to_datetime(out[date_col])
        shown = symbols_as_of(history, ids, when)
        # After a delisting no ticker is valid any more: show the last one it had.
        last = _resolve(history, "instrument_id", "symbol", ids, when, open_only=False)
        shown = shown.where(shown.notna(), last)
        out["symbol"] = shown.where(shown.notna(), ids).astype(str).to_numpy()
    out.insert(out.columns.get_loc("symbol") + 1, "instrument_id", ids.to_numpy())
    return out
