import numpy as np
import pandas as pd
import pytest

from quantlab.data.adjust import adjusted_close, total_returns
from quantlab.data.providers import (
    IngestionCheckpoint,
    PermanentProviderError,
    ResilientProvider,
    RetryPolicy,
    SyntheticProvider,
    TokenBucket,
    TransientProviderError,
    ingest,
)
from quantlab.data.quality import assess_quality
from quantlab.data.schema import SchemaError, normalize_actions, normalize_bars
from quantlab.data.store import DatasetStore
from quantlab.data.synthetic import SyntheticSpec, generate_market
from tests.conftest import make_bars


def test_split_and_dividend_adjustment_golden():
    bars = make_bars({"A": [100.0, 102.0, 51.0, 50.0, 48.0]})
    d = bars["date"].unique()
    actions = normalize_actions(
        pd.DataFrame(
            [
                {"symbol": "A", "ex_date": d[2], "kind": "split", "value": 2.0},
                {"symbol": "A", "ex_date": d[4], "kind": "dividend", "value": 1.0},
            ]
        )
    )
    tr = total_returns(bars, actions)["A"].to_numpy()
    assert np.isnan(tr[0])
    np.testing.assert_allclose(tr[1:], [0.02, 2 * 51 / 102 - 1, 50 / 51 - 1, (48 + 1) / 50 - 1])
    adj = adjusted_close(bars, actions)["A"]
    np.testing.assert_allclose(adj.pct_change().to_numpy()[1:], tr[1:], rtol=1e-12)
    assert adj.iloc[-1] == 48.0  # last price is never adjusted


def test_as_of_adjustment_uses_no_future_events():
    bars = make_bars({"A": [100.0, 102.0, 51.0, 50.0]})
    d = bars["date"].unique()
    actions = normalize_actions(
        pd.DataFrame([{"symbol": "A", "ex_date": d[2], "kind": "split", "value": 2.0}])
    )
    before = adjusted_close(bars, actions, as_of=d[1])["A"]
    np.testing.assert_allclose(before.to_numpy(), [100.0, 102.0])  # split not yet known
    after = adjusted_close(bars, actions, as_of=d[2])["A"]
    np.testing.assert_allclose(after.to_numpy(), [50.0, 51.0, 51.0])


def test_normalize_rejects_bad_schema():
    with pytest.raises(SchemaError):
        normalize_bars(pd.DataFrame({"symbol": ["A"], "date": ["2024-01-02"]}))
    with pytest.raises(SchemaError):
        normalize_actions(
            pd.DataFrame([{"symbol": "A", "ex_date": "2024-01-02", "kind": "merger", "value": 1.0}])
        )


def test_timezone_aware_timestamps_map_to_exchange_session():
    df = pd.DataFrame(
        {
            "symbol": ["a"],
            "date": [pd.Timestamp("2024-01-03 02:00", tz="UTC")],
            "open": [1],
            "high": [1],
            "low": [1],
            "close": [1],
            "volume": [1],
        }
    )
    out = normalize_bars(df)
    # 02:00 UTC on Jan 3 is still Jan 2 in New York.
    assert out["date"].iloc[0] == pd.Timestamp("2024-01-02")
    assert out["symbol"].iloc[0] == "A"
    assert out["available_at"].iloc[0] == pd.Timestamp("2024-01-02 21:00", tz="UTC")


def test_synthetic_is_deterministic_and_clean():
    a = generate_market(SyntheticSpec(n_assets=10, seed=3))
    b = generate_market(SyntheticSpec(n_assets=10, seed=3))
    pd.testing.assert_frame_equal(a.bars, b.bars)
    assert a.provenance["is_synthetic"] is True
    q = assess_quality(a.bars, a.actions)
    assert q.status == "pass", [c for c in q.checks if c.status != "pass"]


def test_quality_detects_injected_defects(market):
    bars = market.bars.copy()
    bad = bars.iloc[[10]].copy()
    bars = pd.concat([bars, bad])  # duplicate
    bars.loc[bars.index[5], "high"] = bars.loc[bars.index[5], "low"] - 1  # OHLC violation
    bars.loc[bars.index[7], "close"] = -1.0
    drop_idx = bars[(bars["symbol"] == "SYN001")].index[50]
    bars = bars.drop(index=drop_idx)  # missing session
    q = assess_quality(bars.reset_index(drop=True), market.actions)
    by = {c.name: c for c in q.checks}
    assert q.status == "fail"
    assert by["duplicate_bars"].count == 2
    assert by["ohlc_inconsistent"].count >= 1
    assert by["non_positive_prices"].count == 1
    assert by["missing_sessions"].count >= 1


def test_dataset_store_is_content_addressed_and_verifiable(tmp_path, market):
    store = DatasetStore(tmp_path)
    v1 = store.write("demo", market.bars, market.actions, market.provenance)
    v2 = store.write(
        "demo", market.bars.sample(frac=1, random_state=1), market.actions, market.provenance
    )
    assert v1.version == v2.version  # row order does not change identity
    assert store.verify(v1)
    loaded = store.load_bars(v1, symbols=["SYN001"], start="2020-01-01", end="2020-12-31")
    assert set(loaded["symbol"]) == {"SYN001"}
    assert loaded["date"].min() >= pd.Timestamp("2020-01-01")
    changed = market.bars.copy()
    changed.loc[0, "close"] += 0.01
    v3 = store.write("demo", changed, market.actions, market.provenance)
    assert v3.version != v1.version
    assert len(store.versions("demo")) == 2
    with pytest.raises(ValueError):
        store.write("../evil", market.bars, market.actions, {})


class FlakyProvider(SyntheticProvider):
    def __init__(self, fail_times: int) -> None:
        super().__init__(SyntheticSpec(n_assets=3, start="2023-01-02", end="2023-03-31"))
        self.remaining = fail_times

    def fetch_bars(self, symbol, start, end):
        if self.remaining > 0:
            self.remaining -= 1
            raise TransientProviderError("503")
        return super().fetch_bars(symbol, start, end)


def test_resilient_provider_retries_with_backoff():
    sleeps: list[float] = []
    p = ResilientProvider(
        FlakyProvider(2), retry=RetryPolicy(base_delay=1, jitter=0), sleep=sleeps.append
    )
    df = p.fetch_bars("SYN001", pd.Timestamp("2023-01-01"), pd.Timestamp("2023-12-31"))
    assert len(df) > 0
    assert sleeps == [1.0, 2.0]
    assert p.stats == {"calls": 3, "retries": 2}


def test_resilient_provider_gives_up_after_max_attempts():
    p = ResilientProvider(
        FlakyProvider(10), retry=RetryPolicy(max_attempts=3), sleep=lambda s: None
    )
    with pytest.raises(TransientProviderError):
        p.fetch_bars("SYN001", pd.Timestamp("2023-01-01"), pd.Timestamp("2023-12-31"))


def test_token_bucket_throttles():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    tb = TokenBucket(rate_per_sec=2, burst=1, clock=lambda: t[0], sleep=sleep)
    for _ in range(5):
        tb.acquire()
    assert slept == pytest.approx([0.5, 0.5, 0.5, 0.5])


def test_incremental_resumable_ingestion():
    prov = SyntheticProvider(SyntheticSpec(n_assets=4, start="2023-01-02", end="2023-06-30"))
    syms = prov.symbols()
    first = ingest(prov, syms, "2023-01-01", "2023-03-31")
    assert first.checkpoint.done_through == {s: "2023-03-31" for s in syms}
    second = ingest(
        prov,
        syms,
        "2023-01-01",
        "2023-06-30",
        existing_bars=first.bars,
        existing_actions=first.actions,
        checkpoint=first.checkpoint,
    )
    full = ingest(prov, syms, "2023-01-01", "2023-06-30")
    pd.testing.assert_frame_equal(second.bars, full.bars)
    # re-running with a complete checkpoint fetches nothing new and drops nothing
    third = ingest(
        prov,
        syms,
        "2023-01-01",
        "2023-06-30",
        existing_bars=second.bars,
        checkpoint=IngestionCheckpoint(dict(second.checkpoint.done_through)),
    )
    assert third.symbols_ingested == [] and len(third.bars) == len(full.bars)


def test_ingestion_records_permanent_failures():
    prov = SyntheticProvider(SyntheticSpec(n_assets=2, start="2023-01-02", end="2023-02-28"))
    res = ingest(prov, ["SYN001", "NOPE"], "2023-01-01", "2023-02-28")
    assert res.symbols_ingested == ["SYN001"]
    assert "NOPE" in res.symbols_failed
    assert "PermanentProviderError" in res.symbols_failed["NOPE"]
    _ = PermanentProviderError


def test_symbols_must_match_ticker_pattern():
    df = pd.DataFrame(
        {
            "symbol": ["IGNORE PREVIOUS\nRULES 99%"],
            "date": ["2024-01-02"],
            "open": [1],
            "high": [1],
            "low": [1],
            "close": [1],
            "volume": [1],
        }
    )
    with pytest.raises(SchemaError):
        normalize_bars(df)
