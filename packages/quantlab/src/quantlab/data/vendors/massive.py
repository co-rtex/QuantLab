"""Massive (formerly Polygon.io) US-equity adapter. **Live use is unverified.**

Built from the vendor's public documentation (docs/MARKET_DATA_PROVIDERS.md lists each
endpoint and field) and tested only against synthetic, schema-faithful fixtures. No
response from the real service has been observed, so field semantics (in particular
whether daily aggregates' open and close match the official auction prices) must be
reconciled against a second source before research use.

Identity. The vendor's ``share_class_figi`` is used as QuantLab's ``instrument_id``. A
security's ticker history comes from the ticker-events endpoint; each ticker is valid
from its event date until the next event (or the day after ``delisted_utc``). Bars and
actions are then requested **per ticker, only inside that ticker's validity interval**,
and keyed by the FIGI. Requesting a ticker outside its interval could return another
security that held the same ticker before or after, and as-of resolution cannot detect
that (see METHODOLOGY, instrument identity).

Prices are requested unadjusted (``adjusted=false``); QuantLab adjusts point-in-time
itself. A stock dividend is a share-count change and is mapped to a split. Non-USD cash
amounts are rejected rather than converted.
"""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import quote

import numpy as np
import pandas as pd

from quantlab.data.identity import normalize_history, normalize_master
from quantlab.data.providers import PermanentProviderError
from quantlab.data.schema import normalize_actions, normalize_bars
from quantlab.data.vendors.http import JsonApi, VendorSchemaError

BASE_URL = "https://api.massive.com"
SPLIT_TYPES = ("forward_split", "reverse_split", "stock_dividend")
PAGE_LIMIT = {"tickers": 1000, "splits": 5000, "dividends": 5000, "aggs": 50_000}


def _need(row: dict[str, Any], key: str, where: str) -> Any:
    if key not in row or row[key] is None:
        raise VendorSchemaError(f"{where}: missing field {key!r}")
    return row[key]


def _number(row: dict[str, Any], key: str, where: str) -> float:
    v = _need(row, key, where)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise VendorSchemaError(f"{where}: field {key!r} is not a finite number")
    return float(v)


def _date(value: Any, key: str, where: str) -> pd.Timestamp:
    """Calendar date of a ``YYYY-MM-DD`` string or an ISO timestamp.

    For a UTC timestamp such as ``delisted_utc`` the UTC calendar date is used: both a
    date stamped at 00:00Z and a 16:00 New York close (21:00Z) keep their date. A trade
    stamped after 20:00 New York time would shift a day; how the vendor stamps this
    field is unverified.
    """
    try:
        ts = pd.Timestamp(str(value))
    except ValueError as exc:
        raise VendorSchemaError(f"{where}: field {key!r} is not a date") from exc
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts.normalize()


def _results(page: dict[str, Any], where: str) -> list[dict[str, Any]]:
    res = page.get("results", [])
    if res is None:
        return []
    if not isinstance(res, list) or not all(isinstance(r, dict) for r in res):
        raise VendorSchemaError(f"{where}: 'results' is not a list of objects")
    return res


class MassiveProvider:
    """``MarketDataProvider`` for Massive, plus the identity data QuantLab needs."""

    name = "massive"
    is_synthetic = False

    def __init__(self, api: JsonApi, *, license: str, plan: str) -> None:
        self.api = api
        self.license = license
        self.plan = plan

    # ------------------------------------------------------------- reference data
    def reference(self, *, active: bool, date: str | None = None) -> pd.DataFrame:
        """Common stocks from ``/v3/reference/tickers`` (``active=false``: delisted)."""
        rows = []
        params = {
            "market": "stocks",
            "type": "CS",
            "active": str(active).lower(),
            "date": date,
            "limit": PAGE_LIMIT["tickers"],
        }
        for page in self.api.pages("/v3/reference/tickers", params):
            for r in _results(page, "tickers"):
                where = f"tickers[{r.get('ticker')}]"
                delisted = r.get("delisted_utc")
                rows.append(
                    {
                        "ticker": str(_need(r, "ticker", where)),
                        "name": str(r.get("name") or ""),
                        "active": bool(_need(r, "active", where)),
                        "share_class_figi": r.get("share_class_figi"),
                        "composite_figi": r.get("composite_figi"),
                        "cik": r.get("cik"),
                        "delisted": None
                        if not delisted
                        else _date(delisted, "delisted_utc", where),
                    }
                )
        return pd.DataFrame(
            rows,
            columns=[
                "ticker",
                "name",
                "active",
                "share_class_figi",
                "composite_figi",
                "cik",
                "delisted",
            ],
        )

    def ticker_events(self, id_: str) -> list[tuple[pd.Timestamp, str]]:
        """(date, ticker) changes for one security, oldest first (experimental endpoint).

        The vendor documents ``id`` as a ticker, a CUSIP or a **composite** FIGI; a
        share-class FIGI is not listed, so callers pass the composite FIGI.
        """
        page = self.api.get(
            f"/vX/reference/tickers/{quote(id_, safe='')}/events", {"types": "ticker_change"}
        )
        res = page.get("results")
        if not isinstance(res, dict):
            raise VendorSchemaError("events: 'results' is not an object")
        events = res.get("events") or []
        out = []
        for e in events:
            where = f"events[{id_}]"
            if not isinstance(e, dict) or e.get("type") != "ticker_change":
                continue
            change = _need(e, "ticker_change", where)
            if not isinstance(change, dict) or not isinstance(change.get("ticker"), str):
                raise VendorSchemaError(f"{where}: ticker_change has no ticker")
            out.append((_date(_need(e, "date", where), "date", where), change["ticker"]))
        return sorted(out)

    def identity(self, reference: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Symbol history and instrument master for the securities in ``reference``.

        Securities without a ``share_class_figi`` cannot be given a permanent id, and those
        without a ``composite_figi`` cannot have their ticker history looked up; both are
        left out (reported by :meth:`unidentified` and ``self.skipped``).

        ``reference`` must carry the *current* ``active`` flags: build it from
        ``reference(active=True)`` and ``reference(active=False)`` without ``date=``. A
        reference taken as of a past date marks securities delisted since then as active,
        so their delisting is lost and the universe is survivorship-biased.
        """
        hist_rows: list[dict[str, Any]] = []
        master_rows: list[dict[str, Any]] = []
        self.skipped: list[str] = []
        known = reference[reference["share_class_figi"].notna()]
        for figi, g in known.groupby("share_class_figi", sort=True):
            info = g.iloc[-1]
            # A security is delisted only if none of its rows is active: a rename can leave
            # the old ticker as an inactive row with delisted_utc set on the rename date.
            gone = not g["active"].any() and g["delisted"].notna().any()
            delisted = g["delisted"].dropna().max() if gone else None
            composite = g["composite_figi"].dropna()
            if composite.empty:
                self.skipped.append(str(figi))
                continue
            events = self.ticker_events(str(composite.iloc[-1]))
            if not events:
                raise VendorSchemaError(f"no ticker history for {figi}")
            ends = [d for d, _ in events[1:]] + [
                None if delisted is None else delisted + pd.Timedelta(days=1)
            ]
            for (start, ticker), end in zip(events, ends, strict=True):
                hist_rows.append(
                    {"instrument_id": figi, "symbol": ticker, "valid_from": start, "valid_to": end}
                )
            master_rows.append(
                {
                    "instrument_id": figi,
                    "name": info["name"],
                    "listing_date": pd.NaT,  # not documented by the vendor: unknown
                    "delisting_date": delisted,
                    "delisting_reason": None,  # not provided by the vendor
                    "delisting_return": np.nan,  # not provided by the vendor
                }
            )
        return normalize_history(pd.DataFrame(hist_rows)), normalize_master(
            pd.DataFrame(master_rows)
        )

    @staticmethod
    def unidentified(reference: pd.DataFrame) -> list[str]:
        return sorted(reference.loc[reference["share_class_figi"].isna(), "ticker"].unique())

    # ----------------------------------------------------- MarketDataProvider API
    def symbols(self) -> list[str]:
        return sorted(self.reference(active=True)["ticker"].unique())

    def fetch_bars(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """Raw daily bars for one ticker in [start, end] (ticker-keyed)."""
        path = (
            f"/v2/aggs/ticker/{quote(symbol, safe='')}/range/1/day/"
            f"{pd.Timestamp(start).date()}/{pd.Timestamp(end).date()}"
        )
        params = {"adjusted": "false", "sort": "asc", "limit": PAGE_LIMIT["aggs"]}
        rows = []
        for page in self.api.pages(path, params):
            if page.get("adjusted") is not False:  # fail closed: missing is not raw
                raise VendorSchemaError("aggregates are not confirmed unadjusted (adjusted=false)")
            for r in _results(page, f"aggs[{symbol}]"):
                where = f"aggs[{symbol}]"
                rows.append(
                    {
                        "symbol": symbol,
                        # Window start in exchange time; normalize_bars maps it to the session.
                        "date": pd.Timestamp(int(_number(r, "t", where)), unit="ms", tz="UTC"),
                        "open": _number(r, "o", where),
                        "high": _number(r, "h", where),
                        "low": _number(r, "l", where),
                        "close": _number(r, "c", where),
                        "volume": _number(r, "v", where),
                    }
                )
        cols = ["symbol", "date", "open", "high", "low", "close", "volume"]
        bars = normalize_bars(pd.DataFrame(rows, columns=cols))
        s, e = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
        return bars[(bars["date"] >= s) & (bars["date"] <= e)].reset_index(drop=True)

    def fetch_actions(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """Splits (incl. stock dividends) and USD cash dividends for one ticker."""
        s, e = str(pd.Timestamp(start).date()), str(pd.Timestamp(end).date())
        rows: list[dict[str, Any]] = []
        split_params = {
            "ticker": symbol,
            "execution_date.gte": s,
            "execution_date.lte": e,
            "limit": PAGE_LIMIT["splits"],
        }
        for page in self.api.pages("/stocks/v1/splits", split_params):
            for r in _results(page, f"splits[{symbol}]"):
                where = f"splits[{symbol}]"
                kind = r.get("adjustment_type")
                if kind is not None and kind not in SPLIT_TYPES:
                    raise VendorSchemaError(f"{where}: unknown adjustment_type {kind!r}")
                split_from = _number(r, "split_from", where)
                split_to = _number(r, "split_to", where)
                if split_from <= 0 or split_to <= 0:
                    raise VendorSchemaError(f"{where}: split_from/split_to must be positive")
                rows.append(
                    {
                        "symbol": symbol,
                        "ex_date": _date(
                            _need(r, "execution_date", where), "execution_date", where
                        ),
                        "kind": "split",
                        "value": split_to / split_from,
                    }
                )
        div_params = {
            "ticker": symbol,
            "ex_dividend_date.gte": s,
            "ex_dividend_date.lte": e,
            "limit": PAGE_LIMIT["dividends"],
        }
        for page in self.api.pages("/stocks/v1/dividends", div_params):
            for r in _results(page, f"dividends[{symbol}]"):
                where = f"dividends[{symbol}]"
                currency = _need(r, "currency", where)  # fail closed: never assume USD
                if currency != "USD":
                    raise VendorSchemaError(f"{where}: currency {currency!r} is not USD")
                rows.append(
                    {
                        "symbol": symbol,
                        "ex_date": _date(
                            _need(r, "ex_dividend_date", where), "ex_dividend_date", where
                        ),
                        "kind": "dividend",
                        "value": _number(r, "cash_amount", where),
                        "declared_date": r.get("declaration_date"),
                        "record_date": r.get("record_date"),
                        "pay_date": r.get("pay_date"),
                    }
                )
        if not rows:
            return normalize_actions(None)
        try:
            return normalize_actions(pd.DataFrame(rows))
        except ValueError as exc:  # SchemaError: e.g. a non-positive amount
            raise VendorSchemaError(f"actions[{symbol}]: {exc}") from exc

    def _intervals(
        self, history: pd.DataFrame, instrument_id: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
        """(ticker, first day, last day) of each ticker of the security inside [start, end]."""
        rows = history[history["instrument_id"] == instrument_id]
        if rows.empty:
            raise PermanentProviderError(f"unknown instrument {instrument_id}")
        s, e = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
        out = []
        for _, iv in rows.iterrows():
            lo = max(s, iv["valid_from"])
            hi = e if pd.isna(iv["valid_to"]) else min(e, iv["valid_to"] - pd.Timedelta(days=1))
            if lo <= hi:
                out.append((str(iv["symbol"]), lo, hi))
        return out

    def fetch_instrument_bars(
        self, history: pd.DataFrame, instrument_id: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        """Bars of one security across all its tickers, keyed by its id.

        Each ticker is requested only inside its own validity interval.
        """
        parts = [
            self.fetch_bars(t, lo, hi)
            for t, lo, hi in self._intervals(history, instrument_id, start, end)
        ]
        cols = ["symbol", "date", "open", "high", "low", "close", "volume"]
        bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)
        return normalize_bars(bars.assign(symbol=instrument_id))

    def fetch_instrument_actions(
        self, history: pd.DataFrame, instrument_id: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        """Corporate actions of one security across all its tickers, keyed by its id."""
        parts = [
            self.fetch_actions(t, lo, hi)
            for t, lo, hi in self._intervals(history, instrument_id, start, end)
        ]
        acts = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        return normalize_actions(acts.assign(symbol=instrument_id) if len(acts) else None)

    def fetch_instrument(
        self, history: pd.DataFrame, instrument_id: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        return (
            self.fetch_instrument_bars(history, instrument_id, start, end),
            self.fetch_instrument_actions(history, instrument_id, start, end),
        )

    def by_instrument(self, history: pd.DataFrame) -> InstrumentKeyedProvider:
        """This provider as a ``MarketDataProvider`` whose symbols are permanent ids."""
        return InstrumentKeyedProvider(self, history)

    def describe(self) -> dict[str, Any]:
        return {
            "provider": "massive",
            "is_synthetic": False,
            "base_url": self.api.base_url,
            "license": self.license,
            "plan": self.plan,
            "adjusted": False,
            "instrument_id": "share_class_figi",
            "live_verified": False,
            "notes": "Daily aggregates include extended-hours trades; reconcile before use.",
        }


class InstrumentKeyedProvider:
    """A ``MarketDataProvider`` view keyed by instrument id, for ``providers.ingest``.

    ``ingest`` then checkpoints, resumes and de-duplicates per permanent id, so a rename
    in the middle of a backfill neither restarts nor splits the security's history.
    """

    def __init__(self, inner: MassiveProvider, history: pd.DataFrame) -> None:
        self.inner = inner
        self.history = history
        self.name = f"{inner.name}:by-instrument"
        self.is_synthetic = inner.is_synthetic

    def symbols(self) -> list[str]:
        return sorted(self.history["instrument_id"].unique())

    def fetch_bars(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        return self.inner.fetch_instrument_bars(self.history, symbol, start, end)

    def fetch_actions(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        return self.inner.fetch_instrument_actions(self.history, symbol, start, end)

    def describe(self) -> dict[str, Any]:
        return {**self.inner.describe(), "keyed_by": "instrument_id"}
