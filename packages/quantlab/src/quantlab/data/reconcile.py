"""Reconciliation of a dataset against a second, independent source.

One vendor's data cannot be validated by looking at it alone. A missing split and a
genuine -50% day produce the same prices (see the ``split_like_returns`` warning in
``quantlab.data.quality``). Comparing two sources tells them apart.

**Identity.** Both sources are keyed by permanent instrument id
(``quantlab.data.identity``) before they are compared:

* The primary is keyed by its own symbol history (or the legacy map, ticker = id).
* A secondary with its own history is keyed by it. ``id_map`` translates its id space
  (FIGI, permaticker, ...) onto the primary's, and the two histories' tickers are
  compared session by session.
* A secondary *without* a history (a ticker-keyed CSV, say) is resolved through the
  primary's dated history, so a rename or a reused ticker maps exactly as the primary
  says. Its tickers are not compared, since it has no history of its own. Rows whose
  ticker the primary does not know on that date are identity discrepancies.

**Discrepancy classes,** inside the window both sources cover for each instrument:

* ``missing_bar``: a session one source has and the other lacks.
* ``invalid_value``: a non-finite close or volume on a compared session.
* ``price_mismatch``: closes differing by more than the larger of ``close_rel`` and a
  floor that scales with price (half a tick by default: $0.005 at or above $1, $0.00005
  below), so a $0.02 vs $0.024 penny-stock difference is caught.
* ``adjusted_series``: an instrument whose close ratio sits on two or more distinct
  levels that step at the primary's ex-dates, which is the signature of a split- or
  dividend-adjusted series compared with raw prices. It is reported once here instead of
  as many price mismatches. Reconcile raw (unadjusted) data.
* ``scale_mismatch``: an instrument whose close ratio is one constant other than 1 (pence
  vs pounds, another currency, a fixed offset). This is not an adjustment.
  For both classes only bars on the ratio level are explained. A bar off it (a bad
  print inside an adjusted series) is still a ``price_mismatch``.
* ``volume_mismatch``: volume differing by more than ``volume_rel``. Vendors legitimately
  differ (consolidated vs primary-exchange volume), so this is a warning by default.
* ``action_missing``: a split or dividend in one source only.
* ``action_mismatch``: the same action with a different value, or with ex-dates up to
  ``ex_date_sessions`` sessions apart (a late or early record is one mismatch, not two
  missing actions).
* ``listing_mismatch``: first or last bar dates differing by more than ``listing_days``.
  A secondary that stops early (stale) shows up here for every instrument, and in the
  recorded input coverage.
* ``identity_mismatch``: an instrument known to one source only, the two histories naming
  different tickers on a session, or a history-less secondary's ticker that the primary
  does not know on that date.

Every class has a configurable severity; the report's status is the worst found. The
report records its inputs (names, content hashes, provider descriptions, synthetic flags,
date coverage) and when it was made, so a stored report can be traced to exactly what
was compared.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import numpy as np
import pandas as pd

from quantlab.data.calendar import trading_days
from quantlab.data.identity import (
    IdentityError,
    is_legacy,
    key_by_instrument,
    legacy_history,
    resolve_ids,
    symbols_as_of,
)
from quantlab.data.schema import normalize_actions, normalize_bars
from quantlab.data.store import content_hash

Severity = Literal["fail", "warn"]
CLASSES = (
    "missing_bar",
    "invalid_value",
    "price_mismatch",
    "adjusted_series",
    "scale_mismatch",
    "volume_mismatch",
    "action_missing",
    "action_mismatch",
    "listing_mismatch",
    "identity_mismatch",
)
SAMPLE = 20


@dataclass(frozen=True)
class Tolerances:
    """Explicit comparison tolerances. Defaults are deliberately tight for daily closes.

    * ``close_rel``: relative close tolerance (5 bp).
    * ``close_abs``: absolute close floor; ``None`` means half a tick at that price.
    * ``volume_rel``: relative volume difference tolerated.
    * ``dividend_abs``: per-share cash tolerance; ``split_rel``: relative ratio tolerance.
    * ``ex_date_sessions``: sessions by which the same action's ex-dates may differ.
    * ``listing_days``: tolerated difference in first/last bar dates.
    * ``adjusted_min_bars``/``adjusted_share``: an instrument with at least this many price
      mismatches, at least this share of whose bars sit on a constant close ratio per
      segment between the primary's ex-dates, is explained as ``adjusted_series`` (two or
      more levels stepping at ex-dates) or ``scale_mismatch`` (one level other than 1).
    """

    close_rel: float = 0.0005
    close_abs: float | None = None
    volume_rel: float = 0.05
    dividend_abs: float = 0.0001
    split_rel: float = 1e-6
    ex_date_sessions: int = 3
    listing_days: int = 0
    adjusted_min_bars: int = 5
    adjusted_share: float = 0.9
    severity: Mapping[str, Severity] = field(
        default_factory=lambda: {
            "missing_bar": "fail",
            "invalid_value": "fail",
            "price_mismatch": "fail",
            "adjusted_series": "fail",
            "scale_mismatch": "fail",
            "volume_mismatch": "warn",
            "action_missing": "fail",
            "action_mismatch": "fail",
            "listing_mismatch": "warn",
            "identity_mismatch": "fail",
        }
    )


@dataclass(frozen=True)
class Source:
    """One side of a comparison.

    ``history`` None means: for the primary, ticker == instrument (legacy); for the
    secondary, resolve its tickers through the primary's history. ``version`` is the
    dataset version hash when the source is a stored version (computed when omitted).
    ``provider`` is the provider's ``describe()`` output, recorded in the report.
    """

    name: str
    bars: pd.DataFrame
    actions: pd.DataFrame
    history: pd.DataFrame | None = None
    is_synthetic: bool = False
    version: str | None = None
    provider: Mapping[str, Any] | None = None


@dataclass
class ReconciliationReport:
    primary: str
    secondary: str
    status: Literal["pass", "warn", "fail"]
    created_at: str
    inputs: dict[str, Any]
    tolerances: dict[str, Any]
    counts: dict[str, int]
    samples: dict[str, list[dict[str, Any]]]
    instruments: dict[str, int]
    synthetic_inputs: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _plain(v: Any) -> Any:
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and not np.isfinite(v):
        # JSON has no infinity; say so instead of showing an infinite close as missing.
        return None if np.isnan(v) else ("inf" if v > 0 else "-inf")
    return v


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    out = frame.head(SAMPLE).copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d")
    rows = out.astype(object).where(out.notna(), None).to_dict("records")
    return [{k: _plain(v) for k, v in r.items()} for r in rows]


def _half_tick(price: pd.Series) -> np.ndarray:
    """Half of the Reg NMS Rule 612 minimum increment at each price."""
    return np.where(price.abs() >= 1.0, 0.005, 0.00005)


def _describe(src: Source, bars: pd.DataFrame, actions: pd.DataFrame) -> dict[str, Any]:
    computed = content_hash(bars, actions, src.history)
    return {
        "name": src.name,
        "version": src.version or computed,
        "version_is_computed": src.version is None,
        # A supplied version is checked against the data actually compared.
        "version_matches_data": src.version is None or src.version == computed,
        "is_synthetic": src.is_synthetic,
        "provider": dict(src.provider) if src.provider else None,
        "has_symbol_history": src.history is not None,
        "bars": len(bars),
        "actions": len(actions),
        "first_date": str(bars["date"].min().date()) if len(bars) else None,
        "last_date": str(bars["date"].max().date()) if len(bars) else None,
    }


def _key_through(
    history: pd.DataFrame, bars: pd.DataFrame, actions: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Key ticker-keyed data through ``history``; unmapped rows are returned separately.

    Duplicates are rejected as in ``key_by_instrument``: two bars for one ticker and
    session, or for one instrument and session after keying, cannot be compared.
    """
    if bars.duplicated(["symbol", "date"]).any():
        raise IdentityError("duplicate bars for one ticker and session in the secondary")
    bar_ids = resolve_ids(history, bars["symbol"], bars["date"])
    act_ids = resolve_ids(history, actions["symbol"], actions["ex_date"])
    unmapped = pd.concat(
        [
            pd.DataFrame(
                {
                    "symbol": bars.loc[bar_ids.isna(), "symbol"],
                    "date": bars.loc[bar_ids.isna(), "date"],
                    "what": "bars",
                }
            ),
            pd.DataFrame(
                {
                    "symbol": actions.loc[act_ids.isna(), "symbol"],
                    "date": actions.loc[act_ids.isna(), "ex_date"],
                    "what": "actions",
                }
            ),
        ],
        ignore_index=True,
    )
    ok_b, ok_a = bar_ids.notna(), act_ids.notna()
    keyed_bars = normalize_bars(bars[ok_b].assign(symbol=bar_ids[ok_b].astype(str)))
    if keyed_bars.duplicated(["symbol", "date"]).any():
        raise IdentityError("one instrument has secondary bars under two tickers on a session")
    keyed_acts = normalize_actions(
        actions[ok_a].assign(symbol=act_ids[ok_a].astype(str)) if ok_a.any() else None
    )
    return keyed_bars, keyed_acts, unmapped


def reconcile(
    primary: Source,
    secondary: Source,
    *,
    tolerances: Tolerances | None = None,
    id_map: Mapping[str, str] | None = None,
) -> ReconciliationReport:
    """Compare ``secondary`` against ``primary`` and classify every discrepancy."""
    tol = tolerances or Tolerances()
    p_bars, p_acts = normalize_bars(primary.bars), normalize_actions(primary.actions)
    s_bars, s_acts = normalize_bars(secondary.bars), normalize_actions(secondary.actions)
    inputs = {
        "primary": _describe(primary, p_bars, p_acts),
        "secondary": _describe(secondary, s_bars, s_acts),
    }

    ph = primary.history if primary.history is not None else legacy_history(p_bars["symbol"])
    pb, pa = key_by_instrument(p_bars, p_acts, ph)
    identity: list[pd.DataFrame] = []
    compare_tickers = False
    if secondary.history is None:
        if id_map:
            raise ValueError("id_map applies only to a secondary with its own symbol history")
        sb, sa, unmapped = _key_through(ph, s_bars, s_acts)
        if len(unmapped):
            g = unmapped.groupby(["symbol", "what"])["date"]
            identity.append(
                g.agg(["min", "max", "size"])
                .reset_index()
                .rename(columns={"symbol": "ticker", "min": "from", "max": "to", "size": "rows"})
                .assign(issue=f"ticker unknown to {primary.name} on these dates")
            )
        sh = ph
    else:
        sb, sa = key_by_instrument(s_bars, s_acts, secondary.history)
        sh = secondary.history
        if id_map:
            mapping = {str(k).upper(): str(v).upper() for k, v in id_map.items()}
            if len(set(mapping.values())) != len(mapping):
                raise ValueError("id_map must be one-to-one (several ids map to one)")
            native = set(sb["symbol"]) - set(mapping)
            taken = native & set(mapping.values())
            if taken:
                raise ValueError(
                    f"id_map targets already used by unmapped ids: {sorted(taken)[:5]}"
                )
            sb = sb.assign(symbol=sb["symbol"].replace(mapping))
            sa = sa.assign(symbol=sa["symbol"].replace(mapping))
            sh = sh.assign(instrument_id=sh["instrument_id"].replace(mapping))
        compare_tickers = not (is_legacy(ph) or is_legacy(secondary.history))

    found: dict[str, pd.DataFrame] = {}
    p_ids, s_ids = set(pb["symbol"]), set(sb["symbol"])
    only = sorted(p_ids ^ s_ids)
    identity.append(
        pd.DataFrame(
            {
                "instrument_id": only,
                "issue": [
                    f"only in {primary.name if i in p_ids else secondary.name}" for i in only
                ],
            }
        )
    )
    common = sorted(p_ids & s_ids)
    pb, sb = pb[pb["symbol"].isin(common)], sb[sb["symbol"].isin(common)]

    # --- bars, inside the window both sources cover for each instrument
    if common:
        span = pd.concat(
            [
                pb.groupby("symbol")["date"].agg(["min", "max"]),
                sb.groupby("symbol")["date"].agg(["min", "max"]),
            ],
            axis=1,
            keys=["p", "s"],
        )
        lo = span[[("p", "min"), ("s", "min")]].max(axis=1)
        hi = span[[("p", "max"), ("s", "max")]].min(axis=1)
    else:
        span = pd.DataFrame()
        lo = hi = pd.Series(dtype="datetime64[ns]")
    m = pb.merge(
        sb, on=["symbol", "date"], how="outer", suffixes=("_p", "_s"), indicator=True
    ).rename(columns={"symbol": "instrument_id"})
    if common:
        inside = (m["date"] >= m["instrument_id"].map(lo)) & (
            m["date"] <= m["instrument_id"].map(hi)
        )
    else:  # nothing in common: every instrument is an identity discrepancy
        inside = pd.Series(False, index=m.index)
    missing = m[inside & (m["_merge"] != "both")]
    found["missing_bar"] = missing.assign(
        missing_in=np.where(missing["_merge"] == "left_only", secondary.name, primary.name)
    )[["instrument_id", "date", "missing_in"]]

    both = m[m["_merge"] == "both"]
    value_cols = ["close_p", "close_s", "volume_p", "volume_s"]
    finite = np.isfinite(both[value_cols].to_numpy(dtype=float)).all(axis=1)
    found["invalid_value"] = both.loc[~finite, ["instrument_id", "date", *value_cols]]
    valid = both[finite]
    floor = (
        _half_tick(valid["close_p"])
        if tol.close_abs is None
        else np.full(len(valid), tol.close_abs)
    )
    allowed = np.maximum(floor, tol.close_rel * valid["close_p"].abs())
    off = valid[(valid["close_p"] - valid["close_s"]).abs() > allowed]
    adjusted, scale, explained = _ratio_classes(valid, off, pa, tol)
    found["adjusted_series"] = adjusted
    found["scale_mismatch"] = scale
    found["price_mismatch"] = off.loc[
        ~off.index.isin(explained), ["instrument_id", "date", "close_p", "close_s"]
    ]
    vol_base = valid[["volume_p", "volume_s"]].max(axis=1)
    vdiff = (valid["volume_p"] - valid["volume_s"]).abs()
    found["volume_mismatch"] = valid.loc[
        vdiff > tol.volume_rel * vol_base, ["instrument_id", "date", "volume_p", "volume_s"]
    ]

    # --- listing windows
    if len(span):
        days = pd.Timedelta(days=tol.listing_days)
        moved = ((span[("p", "min")] - span[("s", "min")]).abs() > days) | (
            (span[("p", "max")] - span[("s", "max")]).abs() > days
        )
        lst = span[moved]
        found["listing_mismatch"] = pd.DataFrame(
            {
                "instrument_id": lst.index,
                "first_p": lst[("p", "min")].to_numpy(),
                "first_s": lst[("s", "min")].to_numpy(),
                "last_p": lst[("p", "max")].to_numpy(),
                "last_s": lst[("s", "max")].to_numpy(),
            }
        )
    else:
        found["listing_mismatch"] = pd.DataFrame(columns=["instrument_id"])

    # --- corporate actions, within the common bar window of each instrument
    def window(a: pd.DataFrame) -> pd.DataFrame:
        a = a[a["symbol"].isin(common)]
        if a.empty:
            return a
        return a[(a["ex_date"] >= a["symbol"].map(lo)) & (a["ex_date"] <= a["symbol"].map(hi))]

    found["action_missing"], found["action_mismatch"] = _compare_actions(
        _compose(window(pa)), _compose(window(sa)), tol, primary.name, secondary.name
    )

    # --- symbol histories: the ticker each source names for an instrument on a session
    if compare_tickers and len(both):
        sessions = both[["instrument_id", "date"]]
        tp = symbols_as_of(ph, sessions["instrument_id"], sessions["date"])
        ts = symbols_as_of(sh, sessions["instrument_id"], sessions["date"])
        differ = tp.ne(ts)
        if differ.any():
            d = sessions[differ.to_numpy()].assign(
                ticker_p=tp[differ].to_numpy(), ticker_s=ts[differ].to_numpy()
            )
            g = d.groupby(["instrument_id", "ticker_p", "ticker_s"], dropna=False)["date"]
            identity.append(
                g.agg(["min", "max"])
                .reset_index()
                .assign(issue="tickers differ", sessions=g.size().to_numpy())
                .rename(columns={"min": "from", "max": "to"})
            )
    ident = pd.concat(identity, ignore_index=True)
    for col in ("rows", "sessions"):  # counts stay integers despite NaN-padding in concat
        if col in ident.columns:
            ident[col] = ident[col].astype("Int64")
    found["identity_mismatch"] = ident

    counts = {c: len(found[c]) for c in CLASSES}
    severities = {tol.severity[c] for c in CLASSES if counts[c]}
    status: Literal["pass", "warn", "fail"] = (
        "fail" if "fail" in severities else ("warn" if severities else "pass")
    )
    tol_dict = {k: v for k, v in asdict(tol).items() if k != "severity"}
    tol_dict["severity"] = dict(tol.severity)
    return ReconciliationReport(
        primary=primary.name,
        secondary=secondary.name,
        status=status,
        created_at=datetime.now(UTC).isoformat(),
        inputs=inputs,
        tolerances=tol_dict,
        counts=counts,
        samples={c: _records(found[c]) for c in CLASSES if counts[c]},
        instruments={
            "primary": len(p_ids),
            "secondary": len(s_ids),
            "common": len(common),
            "bars_compared": len(both),
        },
        synthetic_inputs=primary.is_synthetic or secondary.is_synthetic,
    )


def _ratio_classes(
    valid: pd.DataFrame, off: pd.DataFrame, primary_actions: pd.DataFrame, tol: Tolerances
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Index]:
    """Explain systematic close-ratio differences per instrument.

    For an instrument with at least ``adjusted_min_bars`` price mismatches, the close
    ratio (secondary / primary) is taken per segment between the primary's ex-dates. A bar
    is *on level* when its ratio is within the ordinary close tolerance of its segment's
    median. If at least ``adjusted_share`` of the bars are on level:

    * two or more distinct levels that change at the ex-dates are an **adjusted series**
      (one source split/dividend-adjusted, the other raw);
    * a single level other than 1 is a **scale mismatch** (units, currency, or a constant
      offset), not an adjustment.

    On-level bars of an explained instrument are no longer reported one by one; off-level
    bars stay price mismatches, so a bad print inside an adjusted series is never absorbed.
    Returns (adjusted rows, scale rows, index of the explained ``off`` rows).
    """
    adjusted, scale, explained = [], [], []
    counts = off["instrument_id"].value_counts()
    for iid in counts[counts >= tol.adjusted_min_bars].index:
        v = valid[valid["instrument_id"] == iid].sort_values("date")
        ratio = (v["close_s"] / v["close_p"]).to_numpy()
        price = v["close_p"].abs()
        floor = _half_tick(price) if tol.close_abs is None else np.full(len(v), tol.close_abs)
        band = np.maximum(tol.close_rel, floor / price.to_numpy())
        ex = np.sort(primary_actions.loc[primary_actions["symbol"] == iid, "ex_date"].unique())
        segment = np.searchsorted(ex, v["date"].to_numpy(), side="right")
        levels = pd.Series(ratio).groupby(segment).median()
        level_of = levels.reindex(segment).to_numpy()
        on_level = np.abs(ratio / level_of - 1) <= band
        if on_level.mean() < tol.adjusted_share:
            continue
        distinct = [float(levels.iloc[0])]
        for x in levels.iloc[1:]:
            if abs(x / distinct[-1] - 1) > tol.close_rel:
                distinct.append(float(x))
        row = {
            "instrument_id": iid,
            "price_mismatches": int(counts[iid]),
            "ratios": [round(x, 8) for x in distinct],
        }
        if len(distinct) >= 2:
            adjusted.append({**row, "segments": int(levels.size)})
        elif abs(distinct[0] - 1) > tol.close_rel:
            scale.append(row)
        else:
            continue
        on = v.index[on_level]
        explained.extend(off.index[off.index.isin(on)])
    cols = ["instrument_id", "price_mismatches", "ratios"]
    return (
        pd.DataFrame(adjusted, columns=[*cols, "segments"]),
        pd.DataFrame(scale, columns=cols),
        pd.Index(explained),
    )


def _compose(actions: pd.DataFrame) -> pd.DataFrame:
    """One row per (instrument, ex-date, kind): splits multiply, dividends add."""
    key = ["symbol", "ex_date", "kind"]
    if not len(actions):
        return pd.DataFrame(columns=[*key, "value"])
    split = actions[actions["kind"] == "split"].groupby(key, as_index=False)["value"].prod()
    div = actions[actions["kind"] == "dividend"].groupby(key, as_index=False)["value"].sum()
    return pd.concat([split, div], ignore_index=True)


def _compare_actions(
    pa: pd.DataFrame, sa: pd.DataFrame, tol: Tolerances, p_name: str, s_name: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pair each source's actions per (instrument, kind) and classify every pair.

    All candidate pairs within ``ex_date_sessions`` are ranked by (value disagrees,
    session distance) and taken greedily, so an action pairs with an agreeing amount
    before a merely nearer date: two dividends recorded a session late pair with their own
    amounts, not crosswise. A pair on one date with agreeing values is a match; otherwise
    it is an ``action_mismatch``. Unpaired actions are ``action_missing``.
    """

    def value_differs(kind: str, a: float, b: float) -> bool:
        if kind == "split":
            return bool(abs(a / b - 1) > tol.split_rel)
        return bool(abs(a - b) > tol.dividend_abs)

    dates = pd.concat([pa["ex_date"], sa["ex_date"]])
    sessions = (
        trading_days(dates.min() - pd.Timedelta(days=10), dates.max() + pd.Timedelta(days=10))
        if len(dates)
        else pd.DatetimeIndex([])
    )
    mismatch: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    keys = sorted(
        set(map(tuple, pa[["symbol", "kind"]].to_numpy()))
        | set(map(tuple, sa[["symbol", "kind"]].to_numpy()))
    )
    for iid, kind in keys:
        lp = pa[(pa["symbol"] == iid) & (pa["kind"] == kind)].reset_index(drop=True)
        rs = sa[(sa["symbol"] == iid) & (sa["kind"] == kind)].reset_index(drop=True)
        pos_p = sessions.searchsorted(lp["ex_date"].to_numpy())
        pos_s = sessions.searchsorted(rs["ex_date"].to_numpy())
        candidates = []
        for i in range(len(lp)):
            for j in range(len(rs)):
                dist = abs(int(pos_p[i]) - int(pos_s[j]))
                if dist <= tol.ex_date_sessions:
                    bad = value_differs(kind, lp.at[i, "value"], rs.at[j, "value"])
                    candidates.append((bad, dist, i, j))
        used_p: set[int] = set()
        used_s: set[int] = set()
        for bad, dist, i, j in sorted(candidates):
            if i in used_p or j in used_s:
                continue
            used_p.add(i)
            used_s.add(j)
            if dist == 0 and not bad:
                continue  # a match
            issue = "value differs" if dist == 0 else "ex_date differs"
            if dist and bad:
                issue += " and value differs"
            mismatch.append(
                {
                    "instrument_id": iid,
                    "kind": kind,
                    "ex_date_p": lp.at[i, "ex_date"],
                    "ex_date_s": rs.at[j, "ex_date"],
                    "value_p": lp.at[i, "value"],
                    "value_s": rs.at[j, "value"],
                    "issue": issue,
                }
            )
        for i in sorted(set(range(len(lp))) - used_p):
            missing.append(
                {
                    "instrument_id": iid,
                    "ex_date": lp.at[i, "ex_date"],
                    "kind": kind,
                    "value_p": lp.at[i, "value"],
                    "value_s": None,
                    "missing_in": s_name,
                }
            )
        for j in sorted(set(range(len(rs))) - used_s):
            missing.append(
                {
                    "instrument_id": iid,
                    "ex_date": rs.at[j, "ex_date"],
                    "kind": kind,
                    "value_p": None,
                    "value_s": rs.at[j, "value"],
                    "missing_in": p_name,
                }
            )
    cols_x = ["instrument_id", "ex_date", "kind", "value_p", "value_s", "missing_in"]
    cols_m = ["instrument_id", "kind", "ex_date_p", "ex_date_s", "value_p", "value_s", "issue"]
    return pd.DataFrame(missing, columns=cols_x), pd.DataFrame(mismatch, columns=cols_m)
