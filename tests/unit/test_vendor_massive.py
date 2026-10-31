"""Massive adapter against a scripted fake of the documented API (issue #11).

SYNTHETIC FIXTURES. Every response below is invented, follows only the *shape* of the
vendor's public documentation (docs/MARKET_DATA_PROVIDERS.md), and says nothing about real
securities. No test calls the network: the transport is a local function.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable

import pandas as pd
import pytest

from quantlab.data.calendar import trading_days
from quantlab.data.identity import key_by_instrument
from quantlab.data.providers import PermanentProviderError, TokenBucket, TransientProviderError
from quantlab.data.quality import assess_quality
from quantlab.data.vendors.http import (
    HttpResponse,
    JsonApi,
    VendorAuthError,
    VendorSchemaError,
    urllib_transport,
)
from quantlab.data.vendors.massive import BASE_URL, MassiveProvider

KEY = "SECRET-TEST-KEY-123"
SESSIONS = trading_days("2024-01-02", "2024-01-31")

# Synthetic securities: (figi, [(event date, ticker)], delisted_utc or None, base price)
SECURITIES = [
    ("BBGSYN000001", [("2024-01-02", "OLDA"), ("2024-01-10", "NEWA")], None, 100.0),
    ("BBGSYN000002", [("2024-01-02", "XYZ")], "2024-01-05", 20.0),
    ("BBGSYN000003", [("2024-01-16", "XYZ")], None, 300.0),
]


def _holder(ticker: str, day: pd.Timestamp) -> tuple[str, float] | None:
    """Which synthetic security traded under ``ticker`` on ``day`` (and its base price)."""
    for figi, events, delisted, px in SECURITIES:
        for k, (d, t) in enumerate(events):
            start = pd.Timestamp(d)
            end = pd.Timestamp(events[k + 1][0]) if k + 1 < len(events) else None
            last = pd.Timestamp(delisted) if delisted else None
            if (
                t == ticker
                and start <= day
                and (end is None or day < end)
                and (last is None or day <= last)
            ):
                return figi, px
    return None


def _bar(ticker: str, day: pd.Timestamp) -> dict | None:
    hit = _holder(ticker, day)
    if hit is None:
        return None
    figi, px = hit
    # SYN1 splits 2-for-1 on 2024-01-17: raw prices halve from then on.
    if figi == "BBGSYN000001" and day >= pd.Timestamp("2024-01-17"):
        px = px / 2
    t = int(pd.Timestamp(day).tz_localize("America/New_York").timestamp() * 1000)
    return {"o": px, "h": px * 1.01, "l": px * 0.99, "c": px, "v": 1000.0, "vw": px, "t": t, "n": 7}


class FakeMassive:
    """Routes request URLs to synthetic, documented-shape JSON. Records every request."""

    page_size = 4

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.faults: dict[str, list[tuple[int, dict[str, str]]]] = {}
        self.overrides: dict[str, Callable[[dict], dict]] = {}
        self.extra_inactive: list[dict] = []  # more rows for active=false reference pages

    def fail(self, path_fragment: str, *statuses: tuple[int, dict[str, str]]) -> None:
        self.faults[path_fragment] = list(statuses)

    def __call__(self, url: str, timeout: float) -> HttpResponse:
        self.requests.append(url)
        parts = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        assert q.get("apiKey") == KEY, "every request carries the key"
        for frag, queue in self.faults.items():
            if frag in parts.path and queue:
                status, headers = queue.pop(0)
                return HttpResponse(status, headers, b'{"status":"ERROR"}')
        body = self.route(parts.path, q)
        for frag, edit in self.overrides.items():
            if frag in parts.path:
                body = edit(body)
        return HttpResponse(200, {}, json.dumps(body).encode())

    def route(self, path: str, q: dict[str, str]) -> dict:
        if path == "/v3/reference/tickers":
            active = q["active"] == "true"
            rows = [
                {
                    "ticker": ev[-1][1],
                    "name": f"Synthetic Security {figi[-1]}",
                    "active": delisted is None,
                    "delisted_utc": None if delisted is None else f"{delisted}T21:00:00Z",
                    "share_class_figi": figi,
                    "composite_figi": figi.replace("BBG", "BBC"),
                    "cik": None,
                    "market": "stocks",
                    "type": "CS",
                }
                for figi, ev, delisted, _ in SECURITIES
                if (delisted is None) == active
            ]
            if active:
                rows.append({"ticker": "NOFIGI", "name": "No identifier", "active": True})
            else:
                rows += self.extra_inactive
            return {"results": rows, "status": "OK", "count": len(rows)}
        if path.startswith("/vX/reference/tickers/"):
            # Documented ids: ticker, CUSIP or *composite* FIGI (BBC... in this fake). A
            # share-class FIGI is not a documented id, so it is not found.
            id_ = path.split("/")[4]
            figi = id_.replace("BBC", "BBG", 1) if id_.startswith("BBC") else None
            match = [ev for f, ev, _, _ in SECURITIES if f == figi]
            if not match:
                return {"status": "NOT_FOUND"}
            events = match[0]
            return {
                "results": {
                    "name": figi,
                    "events": [
                        {"type": "ticker_change", "date": d, "ticker_change": {"ticker": t}}
                        for d, t in events
                    ],
                },
                "status": "OK",
            }
        if path.startswith("/v2/aggs/ticker/"):
            seg = path.split("/")  # /v2/aggs/ticker/{t}/range/1/day/{from}/{to}
            ticker, frm, to = seg[4], seg[8], seg[9]
            days = [d for d in SESSIONS if pd.Timestamp(frm) <= d <= pd.Timestamp(to)]
            bars = [b for b in (_bar(ticker, d) for d in days) if b is not None]
            start = int(q.get("cursor", 0))
            page = bars[start : start + self.page_size]
            out = {
                "ticker": ticker,
                "adjusted": False,
                "queryCount": len(page),
                "resultsCount": len(page),
                "status": "OK",
                "results": page,
            }
            if start + self.page_size < len(bars):
                out["next_url"] = f"{BASE_URL}{path}?cursor={start + self.page_size}"
            return out
        if path == "/stocks/v1/splits":
            rows = [
                {
                    "id": "S1",
                    "ticker": "NEWA",
                    "execution_date": "2024-01-17",
                    "split_from": 1,
                    "split_to": 2,
                    "adjustment_type": "forward_split",
                    "historical_adjustment_factor": 0.5,
                }
            ]
            return {"results": _window(rows, q, "execution_date"), "status": "OK"}
        if path == "/stocks/v1/dividends":
            rows = [
                {
                    "id": "D1",
                    "ticker": "OLDA",
                    "cash_amount": 0.5,
                    "currency": "USD",
                    "declaration_date": "2023-12-15",
                    "ex_dividend_date": "2024-01-05",
                    "record_date": "2024-01-05",
                    "pay_date": "2024-01-19",
                    "distribution_type": "recurring",
                    "frequency": 4,
                    "historical_adjustment_factor": 0.995,
                    "split_adjusted_cash_amount": 0.25,
                }
            ]
            return {"results": _window(rows, q, "ex_dividend_date"), "status": "OK"}
        return {"status": "NOT_FOUND"}


def _window(rows: list[dict], q: dict[str, str], field: str) -> list[dict]:
    return [
        r
        for r in rows
        if r["ticker"] == q["ticker"] and q[f"{field}.gte"] <= r[field] <= q[f"{field}.lte"]
    ]


@pytest.fixture
def fake() -> FakeMassive:
    return FakeMassive()


def _provider(fake: FakeMassive, **kw) -> tuple[MassiveProvider, list[float]]:
    slept: list[float] = []
    api = JsonApi(BASE_URL, KEY, transport=fake, sleep=slept.append, **kw)
    return MassiveProvider(api, license="test licence (synthetic fixtures)", plan="test"), slept


def _identity(p: MassiveProvider):
    ref = pd.concat([p.reference(active=True), p.reference(active=False)], ignore_index=True)
    return ref, *p.identity(ref)


# ------------------------------------------------------------------------- identity
def test_identity_maps_figis_through_rename_delisting_and_reuse(fake):
    p, _ = _provider(fake)
    ref, history, master = _identity(p)
    assert p.unidentified(ref) == ["NOFIGI"]
    h = history.set_index(["instrument_id", "symbol"])
    assert h.loc[("BBGSYN000001", "OLDA"), "valid_to"] == pd.Timestamp("2024-01-10")
    assert pd.isna(h.loc[("BBGSYN000001", "NEWA"), "valid_to"])
    # The delisted security's ticker ends the day after its last trade.
    assert h.loc[("BBGSYN000002", "XYZ"), "valid_to"] == pd.Timestamp("2024-01-06")
    assert h.loc[("BBGSYN000003", "XYZ"), "valid_from"] == pd.Timestamp("2024-01-16")
    m = master.set_index("instrument_id")
    assert m.loc["BBGSYN000002", "delisting_date"] == pd.Timestamp("2024-01-05")
    assert pd.isna(m.loc["BBGSYN000002", "delisting_return"])  # unknown, never invented
    assert m["listing_date"].isna().all()  # not documented by the vendor


def test_instrument_history_is_continuous_across_a_rename(fake):
    p, _ = _provider(fake)
    _, history, _ = _identity(p)
    bars, actions = p.fetch_instrument(history, "BBGSYN000001", "2024-01-02", "2024-01-31")
    assert (bars["symbol"] == "BBGSYN000001").all()
    assert list(bars["date"]) == list(SESSIONS)  # every session, no gap at the rename
    assert bars["available_at"].notna().all()
    split = actions[actions["kind"] == "split"].iloc[0]
    assert split["ex_date"] == pd.Timestamp("2024-01-17") and split["value"] == 2.0
    div = actions[actions["kind"] == "dividend"].iloc[0]
    assert div["value"] == 0.5
    assert div["declared_date"] == pd.Timestamp("2023-12-15")
    assert div["record_date"] == pd.Timestamp("2024-01-05")
    assert div["pay_date"] == pd.Timestamp("2024-01-19")
    # The keyed data passes the dataset quality checks (split explained by the action).
    report = assess_quality(bars, actions)
    assert report.status in ("pass", "warn"), [(c.name, c.status) for c in report.checks]
    assert not [c for c in report.checks if c.status == "fail"]


def test_a_reused_ticker_never_leaks_into_the_other_security(fake):
    p, _ = _provider(fake)
    _, history, _ = _identity(p)
    old, _ = p.fetch_instrument(history, "BBGSYN000002", "2024-01-02", "2024-01-31")
    new, _ = p.fetch_instrument(history, "BBGSYN000003", "2024-01-02", "2024-01-31")
    assert old["date"].max() == pd.Timestamp("2024-01-05") and (old["close"] == 20.0).all()
    assert new["date"].min() == pd.Timestamp("2024-01-16") and (new["close"] == 300.0).all()
    # Requests for XYZ were bounded by each security's own interval.
    xyz = [u for u in fake.requests if "/v2/aggs/ticker/XYZ/" in u]
    assert any("/2024-01-02/2024-01-05" in u for u in xyz)
    assert any("/2024-01-16/2024-01-31" in u for u in xyz)


def test_ticker_keyed_fetch_plus_history_matches_instrument_fetch(fake):
    p, _ = _provider(fake)
    _, history, _ = _identity(p)
    raw = pd.concat(
        [
            p.fetch_bars("OLDA", "2024-01-02", "2024-01-09"),
            p.fetch_bars("NEWA", "2024-01-10", "2024-01-31"),
        ]
    )
    keyed, _ = key_by_instrument(raw, p.fetch_actions("NEWA", "2024-01-10", "2024-01-31"), history)
    direct, _ = p.fetch_instrument(history, "BBGSYN000001", "2024-01-02", "2024-01-31")
    pd.testing.assert_frame_equal(keyed, direct)


# ------------------------------------------------------------- transport behaviour
def test_pagination_follows_next_url_and_keeps_the_key(fake):
    p, _ = _provider(fake)
    bars = p.fetch_bars("NEWA", "2024-01-10", "2024-01-31")
    assert len(bars) == len([d for d in SESSIONS if d >= pd.Timestamp("2024-01-10")])
    pages = [u for u in fake.requests if "/v2/aggs/ticker/NEWA/" in u]
    assert len(pages) == -(-len(bars) // FakeMassive.page_size)  # ceil
    assert all("cursor=" in u for u in pages[1:])


def test_rate_limit_backoff_honours_retry_after(fake):
    fake.fail("/v2/aggs/ticker/NEWA/", (429, {"Retry-After": "7"}), (503, {}))
    p, slept = _provider(fake)
    bars = p.fetch_bars("NEWA", "2024-01-10", "2024-01-12")
    assert len(bars) == 3
    assert slept[0] >= 7.0  # Retry-After is a floor on the backoff
    assert len(slept) == 2 and p.api.stats["retries"] == 2


def test_a_failure_mid_pagination_is_retried_without_losing_pages(fake):
    fake.page_size = 2
    failed: list[str] = []

    def transport(url: str, timeout: float) -> HttpResponse:
        if "cursor=2" in url and not failed:  # the second page fails once
            failed.append(url)
            return HttpResponse(502, {}, b"")
        return fake(url, timeout)

    p, _ = _provider(fake)
    p.api.transport = transport
    bars = p.fetch_bars("NEWA", "2024-01-10", "2024-01-19")
    expected = [d for d in SESSIONS if "2024-01-10" <= str(d.date()) <= "2024-01-19"]
    assert list(bars["date"]) == expected  # every page, once, in order
    assert failed


def test_retries_are_bounded_and_errors_never_contain_the_key(fake):
    fake.fail("/v2/aggs/", *[(500, {})] * 10)
    p, slept = _provider(fake)
    with pytest.raises(TransientProviderError) as exc:
        p.fetch_bars("NEWA", "2024-01-10", "2024-01-12")
    assert KEY not in str(exc.value) and "***" in str(exc.value)
    assert len(slept) == p.api.retry.max_attempts - 1


def test_auth_failure_is_permanent_and_not_retried(fake):
    fake.fail("/v3/reference/tickers", (401, {}))
    p, slept = _provider(fake)
    with pytest.raises(VendorAuthError) as exc:
        p.reference(active=True)
    assert KEY not in str(exc.value)
    assert slept == [] and len(fake.requests) == 1


def test_missing_key_and_plain_http_are_refused():
    with pytest.raises(VendorAuthError):
        JsonApi(BASE_URL, "")
    with pytest.raises(PermanentProviderError):
        urllib_transport("http://api.massive.com/v3/reference/tickers", 1.0)


def test_redirects_are_never_followed():
    # The key travels in the query string, so following a 3xx would hand it to the
    # redirect target. Exercise the real urllib opener against a loopback server.
    import http.server
    import threading
    import urllib.error

    from quantlab.data.vendors import http as vendor_http

    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v3/x?apiKey={KEY}"
        with pytest.raises(urllib.error.HTTPError) as exc:
            vendor_http._OPENER.open(url, timeout=5)
        assert exc.value.code == 302
        assert hits == [f"/v3/x?apiKey={KEY}"]  # /elsewhere was never requested
    finally:
        server.shutdown()


def test_rate_limiter_is_consulted_for_every_request(fake):
    clock = {"t": 0.0}
    waits: list[float] = []

    def sleep(s: float) -> None:
        waits.append(s)
        clock["t"] += s

    bucket = TokenBucket(rate_per_sec=5 / 60, burst=1, clock=lambda: clock["t"], sleep=sleep)
    p, _ = _provider(fake, limiter=bucket)
    p.fetch_bars("NEWA", "2024-01-10", "2024-01-31")  # several pages
    n = len(fake.requests)
    assert n > 1 and len(waits) == n - 1
    assert all(w == pytest.approx(12.0) for w in waits)  # 5 requests per minute


# ------------------------------------------------------------------ schema checks
@pytest.mark.parametrize(
    ("fragment", "edit", "message"),
    [
        (
            "/v2/aggs/",
            lambda b: {
                **b,
                "results": [{k: v for k, v in r.items() if k != "c"} for r in b["results"]],
            },
            "missing field 'c'",
        ),
        ("/v2/aggs/", lambda b: {**b, "adjusted": True}, "not confirmed unadjusted"),
        (
            "/v2/aggs/",
            lambda b: {k: v for k, v in b.items() if k != "adjusted"},
            "not confirmed unadjusted",
        ),
        (
            "/v2/aggs/",
            lambda b: {**b, "results": [{**r, "o": "12.5"} for r in b["results"]]},
            "not a finite number",
        ),
        ("/v2/aggs/", lambda b: {**b, "results": "oops"}, "not a list"),
        (
            "/stocks/v1/dividends",
            lambda b: {**b, "results": [{**r, "currency": "CAD"} for r in b["results"]]},
            "not USD",
        ),
        (
            "/stocks/v1/splits",
            lambda b: {**b, "results": [{**r, "adjustment_type": "spinoff"} for r in b["results"]]},
            "unknown adjustment_type",
        ),
        (
            "/stocks/v1/splits",
            lambda b: {**b, "results": [{**r, "split_from": 0} for r in b["results"]]},
            "must be positive",
        ),
    ],
)
def test_malformed_vendor_payloads_are_rejected(fake, fragment, edit, message):
    fake.overrides[fragment] = edit
    p, _ = _provider(fake)
    fetch = p.fetch_actions if "/stocks/" in fragment else p.fetch_bars
    ticker = "OLDA" if "dividends" in fragment else "NEWA"
    with pytest.raises(VendorSchemaError, match=message):
        fetch(ticker, "2024-01-02", "2024-01-31")


def test_next_url_to_another_host_is_refused_and_gets_no_key(fake):
    fake.overrides["/v2/aggs/"] = lambda b: {
        **b,
        "next_url": "https://api.massive.com.attacker.example/v2/aggs?cursor=4",
    }
    p, _ = _provider(fake)
    with pytest.raises(VendorSchemaError, match="unexpected host"):
        p.fetch_bars("NEWA", "2024-01-10", "2024-01-31")
    assert all(urllib.parse.urlsplit(u).netloc == "api.massive.com" for u in fake.requests)


def test_describe_never_exposes_the_key_and_states_verification(fake):
    p, _ = _provider(fake)
    d = p.describe()
    assert KEY not in json.dumps(d) and KEY not in repr(p.api)
    assert d["is_synthetic"] is False and d["live_verified"] is False


# ------------------------------------------------------- ingestion and #10 gates
def test_ingestion_by_instrument_resumes_and_passes_the_gates(fake):
    from quantlab.data.providers import IngestionCheckpoint, ingest
    from quantlab.data.quality import enforce_gates

    p, _ = _provider(fake)
    _, history, _ = _identity(p)
    keyed = p.by_instrument(history)
    ids = keyed.symbols()
    first = ingest(keyed, ids, "2024-01-02", "2024-01-12")
    assert sorted(first.symbols_ingested) == ids
    n_requests = len(fake.requests)
    second = ingest(
        keyed,
        ids,
        "2024-01-02",
        "2024-01-31",
        existing_bars=first.bars,
        existing_actions=first.actions,
        checkpoint=first.checkpoint,
    )
    # Resumed from the checkpoint: no request reaches back before 2024-01-13.
    later = fake.requests[n_requests:]
    assert not [u for u in later if "/range/1/day/2024-01-0" in u]
    assert second.duplicates_dropped == 0
    full, _ = p.fetch_instrument(history, "BBGSYN000001", "2024-01-02", "2024-01-31")
    got = second.bars[second.bars["symbol"] == "BBGSYN000001"].reset_index(drop=True)
    pd.testing.assert_frame_equal(got, full)
    assert isinstance(second.checkpoint, IngestionCheckpoint)
    enforce_gates(assess_quality(second.bars, second.actions))  # clean fixtures pass


def test_malformed_vendor_actions_block_ingestion(fake):
    from quantlab.data.providers import ingest
    from quantlab.data.quality import QualityGateError, enforce_gates

    # The vendor reports 4-for-1 for what the prices show is a 2-for-1 split.
    fake.overrides["/stocks/v1/splits"] = lambda b: {
        **b,
        "results": [{**r, "split_to": 4} for r in b["results"]],
    }
    p, _ = _provider(fake)
    _, history, _ = _identity(p)
    res = ingest(p.by_instrument(history), ["BBGSYN000001"], "2024-01-02", "2024-01-31")
    with pytest.raises(QualityGateError, match="split_price_mismatch"):
        enforce_gates(assess_quality(res.bars, res.actions))


# ------------------------------------------------------------ #50 correctness fixes
def test_a_renamed_active_security_is_not_truncated_or_delisted(fake):
    # The vendor lists the old ticker as an inactive row, "delisted" on the rename date.
    fake.extra_inactive.append(
        {
            "ticker": "OLDA",
            "name": "Synthetic Security 1",
            "active": False,
            "delisted_utc": "2024-01-09T21:00:00Z",
            "share_class_figi": "BBGSYN000001",
            "composite_figi": "BBCSYN000001",
            "market": "stocks",
            "type": "CS",
        }
    )
    p, _ = _provider(fake)
    _, history, master = _identity(p)
    m = master.set_index("instrument_id")
    assert pd.isna(m.loc["BBGSYN000001", "delisting_date"])  # still trading as NEWA
    assert pd.isna(
        history.set_index(["instrument_id", "symbol"]).loc[("BBGSYN000001", "NEWA"), "valid_to"]
    )
    bars, _ = p.fetch_instrument(history, "BBGSYN000001", "2024-01-02", "2024-01-31")
    assert list(bars["date"]) == list(SESSIONS)  # all 21 sessions, not 7


def test_ticker_events_are_requested_by_composite_figi(fake):
    p, _ = _provider(fake)
    _identity(p)
    events = [u for u in fake.requests if "/vX/reference/tickers/" in u]
    assert events and all("/BBC" in u for u in events)  # never the share-class FIGI


def test_a_security_without_a_composite_figi_is_skipped_not_guessed(fake):
    p, _ = _provider(fake)
    ref = pd.concat([p.reference(active=True), p.reference(active=False)], ignore_index=True)
    ref.loc[ref["share_class_figi"] == "BBGSYN000003", "composite_figi"] = None
    history, _ = p.identity(ref)
    assert "BBGSYN000003" not in set(history["instrument_id"])
    assert p.skipped == ["BBGSYN000003"]


def test_malformed_ticker_change_and_missing_currency_are_schema_errors(fake):
    fake.overrides["/vX/reference/tickers/"] = lambda b: {
        **b,
        "results": {
            **b["results"],
            "events": [
                {"type": "ticker_change", "date": "2024-01-02", "ticker_change": {"symbol": "X"}}
            ],
        },
    }
    p, _ = _provider(fake)
    with pytest.raises(VendorSchemaError, match="ticker_change has no ticker"):
        p.ticker_events("BBCSYN000001")
    fake.overrides.clear()
    fake.overrides["/stocks/v1/dividends"] = lambda b: {
        **b,
        "results": [{k: v for k, v in r.items() if k != "currency"} for r in b["results"]],
    }
    with pytest.raises(VendorSchemaError, match="missing field 'currency'"):
        p.fetch_actions("OLDA", "2024-01-02", "2024-01-31")


def test_paths_are_quoted(fake):
    p, _ = _provider(fake)
    p.fetch_bars("A/B", "2024-01-02", "2024-01-05")
    assert any("/v2/aggs/ticker/A%2FB/range/" in u for u in fake.requests)


def test_read_errors_during_the_body_are_retryable(monkeypatch):
    import http.client
    import ssl

    from quantlab.data.vendors import http as vendor_http

    class Resp:
        status = 200
        headers: dict[str, str] = {}

        def __init__(self, exc):
            self.exc = exc

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            raise self.exc

    for exc in (
        http.client.IncompleteRead(b"partial"),
        ssl.SSLError("bad record mac"),
        ConnectionResetError("reset"),
    ):
        monkeypatch.setattr(vendor_http._OPENER, "open", lambda *a, e=exc, **k: Resp(e))
        with pytest.raises(TransientProviderError):
            urllib_transport("https://api.massive.com/v3/x", 1.0)


def test_oversized_responses_are_refused(monkeypatch):
    from quantlab.data.vendors import http as vendor_http

    class Big:
        status = 200
        headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b"x" * n  # always as much as asked for

    monkeypatch.setattr(vendor_http, "MAX_RESPONSE_BYTES", 1000)
    monkeypatch.setattr(vendor_http._OPENER, "open", lambda *a, **k: Big())
    with pytest.raises(PermanentProviderError, match="larger than"):
        urllib_transport("https://api.massive.com/v3/x", 1.0)


def test_url_encoded_keys_are_redacted(fake):
    key = "ab+c/d="

    def transport(url, timeout):
        fake.requests.append(url)
        return HttpResponse(500, {}, b"")

    api = JsonApi(BASE_URL, key, transport=transport, sleep=lambda s: None)
    with pytest.raises(TransientProviderError) as exc:
        api.get("/v3/reference/tickers")
    msg = str(exc.value)
    assert "%2B" in fake.requests[0]  # the key really is encoded in the URL
    for form in (key, "ab%2Bc%2Fd%3D", "ab%2Bc/d%3D"):
        assert form not in msg


def test_checkpoint_stops_at_the_last_completed_session(fake):
    from quantlab.data.providers import ingest

    p, _ = _provider(fake)
    _, history, _ = _identity(p)
    # A backfill run during the session on Wed 2024-01-17 with end in the future.
    res = ingest(
        p.by_instrument(history),
        ["BBGSYN000001"],
        "2024-01-02",
        "2024-03-01",
        now=pd.Timestamp("2024-01-17 11:00", tz="America/New_York"),
    )
    assert res.checkpoint.done_through["BBGSYN000001"] == "2024-01-16"
