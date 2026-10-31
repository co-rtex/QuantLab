"""Reconciliation of a dataset against a second, independent source.

One vendor's data cannot be validated by looking at it alone. A missing split and a
genuine -50% day produce the same prices (see the ``split_like_returns`` warning in
``quantlab.data.quality``). Comparing two sources tells them apart.

Both sources are keyed by permanent instrument id (``quantlab.data.identity``) before
they are compared. Vendors use different id spaces (FIGI, permaticker, ...), so the
secondary's ids can be mapped onto the primary's with ``id_map``. Ids that stay unmapped
are themselves an identity discrepancy.

Discrepancy classes:

* ``missing_bar``: a session one source has for an instrument and the other lacks,
  within the window both sources cover for that instrument.
* ``price_mismatch``: a close differing by more than ``max(close_abs, close_rel * price)``.
* ``volume_mismatch``: volume differing by more than ``volume_rel``. Vendors legitimately
  differ (consolidated vs primary-exchange volume), so this is a warning by default.
* ``action_missing``: a split or dividend in one source only.
* ``action_mismatch``: the same action (instrument, ex-date, kind) with different values.
* ``listing_mismatch``: first or last bar dates differing by more than
  ``listing_days`` calendar days.
* ``identity_mismatch``: an instrument known to only one source, or the two symbol
  histories naming different tickers for an instrument on a session.

Every class has a severity (``fail`` or ``warn``, configurable in :class:`Tolerances`).
The report's status is the worst severity found.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

from quantlab.data.identity import key_by_instrument, legacy_history, symbols_as_of

Severity = Literal["fail", "warn"]
CLASSES = (
    "missing_bar",
    "price_mismatch",
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

    * ``close_rel``/``close_abs``: closes agree if within the larger of the two.
      Defaults: 5 bp, or half a cent (a rounding difference at the vendor).
    * ``volume_rel``: relative volume difference tolerated.
    * ``dividend_abs``: per-share cash tolerance; ``split_rel``: relative ratio tolerance.
    * ``listing_days``: tolerated difference in first/last bar dates.
    """

    close_rel: float = 0.0005
    close_abs: float = 0.005
    volume_rel: float = 0.05
    dividend_abs: float = 0.0001
    split_rel: float = 1e-6
    listing_days: int = 0
    severity: Mapping[str, Severity] = field(
        default_factory=lambda: {
            "missing_bar": "fail",
            "price_mismatch": "fail",
            "volume_mismatch": "warn",
            "action_missing": "fail",
            "action_mismatch": "fail",
            "listing_mismatch": "warn",
            "identity_mismatch": "fail",
        }
    )


@dataclass(frozen=True)
class Source:
    """One side of a comparison. ``history`` None means ticker == instrument (legacy)."""

    name: str
    bars: pd.DataFrame
    actions: pd.DataFrame
    history: pd.DataFrame | None = None
    is_synthetic: bool = False

    def keyed(self) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        hist = self.history if self.history is not None else legacy_history(self.bars["symbol"])
        bars, actions = key_by_instrument(self.bars, self.actions, hist)
        return bars, actions, hist


@dataclass
class ReconciliationReport:
    primary: str
    secondary: str
    status: Literal["pass", "warn", "fail"]
    tolerances: dict[str, Any]
    counts: dict[str, int]
    samples: dict[str, list[dict[str, Any]]]
    instruments: dict[str, int]
    synthetic_inputs: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    out = frame.head(SAMPLE).copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d")
    return [
        {k: (None if (isinstance(v, float) and not np.isfinite(v)) else v) for k, v in r.items()}
        for r in out.astype(object).where(out.notna(), None).to_dict("records")
    ]


def reconcile(
    primary: Source,
    secondary: Source,
    *,
    tolerances: Tolerances | None = None,
    id_map: Mapping[str, str] | None = None,
) -> ReconciliationReport:
    """Compare ``secondary`` against ``primary`` and classify every discrepancy."""
    tol = tolerances or Tolerances()
    pb, pa, ph = primary.keyed()
    sb, sa, sh = secondary.keyed()
    if id_map:
        mapping = {str(k).upper(): str(v).upper() for k, v in id_map.items()}
        sb = sb.assign(symbol=sb["symbol"].map(lambda s: mapping.get(s, s)))
        sa = sa.assign(symbol=sa["symbol"].map(lambda s: mapping.get(s, s)))
        sh = sh.assign(instrument_id=sh["instrument_id"].map(lambda s: mapping.get(s, s)))

    found: dict[str, pd.DataFrame] = {}
    p_ids, s_ids = set(pb["symbol"]), set(sb["symbol"])
    only = sorted(p_ids ^ s_ids)
    identity = [
        pd.DataFrame(
            {
                "instrument_id": only,
                "issue": [
                    f"only in {primary.name if i in p_ids else secondary.name}" for i in only
                ],
            }
        )
    ]
    common = sorted(p_ids & s_ids)
    pb, sb = pb[pb["symbol"].isin(common)], sb[sb["symbol"].isin(common)]

    # --- bars, inside the window both sources cover for each instrument
    span = (
        pd.concat(
            [
                pb.groupby("symbol")["date"].agg(["min", "max"]),
                sb.groupby("symbol")["date"].agg(["min", "max"]),
            ],
            axis=1,
            keys=["p", "s"],
        )
        if common
        else pd.DataFrame()
    )
    m = pb.merge(
        sb, on=["symbol", "date"], how="outer", suffixes=("_p", "_s"), indicator=True
    ).rename(columns={"symbol": "instrument_id"})
    if len(span):
        lo = span[[("p", "min"), ("s", "min")]].max(axis=1)
        hi = span[[("p", "max"), ("s", "max")]].min(axis=1)
        inside = (m["date"] >= m["instrument_id"].map(lo)) & (
            m["date"] <= m["instrument_id"].map(hi)
        )
    else:
        inside = pd.Series(False, index=m.index)
    missing = m[inside & (m["_merge"] != "both")]
    found["missing_bar"] = missing.assign(
        missing_in=np.where(missing["_merge"] == "left_only", secondary.name, primary.name)
    )[["instrument_id", "date", "missing_in"]]

    both = m[m["_merge"] == "both"]
    diff = (both["close_p"] - both["close_s"]).abs()
    allowed = np.maximum(tol.close_abs, tol.close_rel * both["close_p"].abs())
    found["price_mismatch"] = both.loc[
        diff > allowed, ["instrument_id", "date", "close_p", "close_s"]
    ]
    vol_base = both[["volume_p", "volume_s"]].max(axis=1)
    vdiff = (both["volume_p"] - both["volume_s"]).abs()
    found["volume_mismatch"] = both.loc[
        vdiff > tol.volume_rel * vol_base, ["instrument_id", "date", "volume_p", "volume_s"]
    ]

    # --- listing windows
    if len(span):
        days = pd.Timedelta(days=tol.listing_days)
        off = ((span[("p", "min")] - span[("s", "min")]).abs() > days) | (
            (span[("p", "max")] - span[("s", "max")]).abs() > days
        )
        lst = span[off]
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
    key = ["symbol", "ex_date", "kind"]

    def window(a: pd.DataFrame) -> pd.DataFrame:
        a = a[a["symbol"].isin(common)]
        if not len(span):
            return a
        lo_a = a["symbol"].map(span[[("p", "min"), ("s", "min")]].max(axis=1))
        hi_a = a["symbol"].map(span[[("p", "max"), ("s", "max")]].min(axis=1))
        return a[(a["ex_date"] >= lo_a) & (a["ex_date"] <= hi_a)]

    # Same-day, same-kind actions are combined before comparing, so a vendor that splits
    # a dividend into regular + special rows still matches one that reports the total.
    pa_w = _compose(window(pa))
    sa_w = _compose(window(sa))
    am = pa_w.merge(sa_w, on=key, how="outer", suffixes=("_p", "_s"), indicator=True).rename(
        columns={"symbol": "instrument_id"}
    )
    only_a = am[am["_merge"] != "both"]
    found["action_missing"] = only_a.assign(
        missing_in=np.where(only_a["_merge"] == "left_only", secondary.name, primary.name)
    )[["instrument_id", "ex_date", "kind", "value_p", "value_s", "missing_in"]]
    ab = am[am["_merge"] == "both"]
    is_split = ab["kind"] == "split"
    bad = np.where(
        is_split,
        (ab["value_p"] / ab["value_s"] - 1).abs() > tol.split_rel,
        (ab["value_p"] - ab["value_s"]).abs() > tol.dividend_abs,
    )
    found["action_mismatch"] = ab.loc[
        bad, ["instrument_id", "ex_date", "kind", "value_p", "value_s"]
    ]

    # --- symbol histories: the ticker each source names for an instrument on a session
    sessions = both[["instrument_id", "date"]]
    if len(sessions):
        tp = symbols_as_of(ph, sessions["instrument_id"], sessions["date"])
        ts = symbols_as_of(sh, sessions["instrument_id"], sessions["date"])
        differ = tp.ne(ts)
        if differ.any():
            d = sessions[differ.to_numpy()].assign(
                ticker_p=tp[differ].to_numpy(), ticker_s=ts[differ].to_numpy()
            )
            # One row per (instrument, ticker pair) with the first and last such session.
            g = d.groupby(["instrument_id", "ticker_p", "ticker_s"], dropna=False)["date"]
            runs = g.agg(["min", "max"]).reset_index()
            identity.append(
                runs.assign(
                    issue="tickers differ",
                    sessions=g.size().to_numpy(),
                ).rename(columns={"min": "from", "max": "to"})
            )
    found["identity_mismatch"] = pd.concat(identity, ignore_index=True)

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


def _compose(actions: pd.DataFrame) -> pd.DataFrame:
    """One row per (instrument, ex-date, kind): splits multiply, dividends add."""
    key = ["symbol", "ex_date", "kind"]
    if not len(actions):
        return pd.DataFrame(columns=[*key, "value"])
    split = actions[actions["kind"] == "split"].groupby(key, as_index=False)["value"].prod()
    div = actions[actions["kind"] == "dividend"].groupby(key, as_index=False)["value"].sum()
    return pd.concat([split, div], ignore_index=True)
