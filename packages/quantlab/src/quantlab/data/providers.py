"""Market-data provider abstraction, resilience wrappers and resumable ingestion."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from quantlab.data.schema import normalize_actions, normalize_bars
from quantlab.data.synthetic import SyntheticMarket, SyntheticSpec, generate_market


class ProviderError(Exception):
    """Base class for provider failures."""


class TransientProviderError(ProviderError):
    """Retryable failure (timeouts, throttling, 5xx)."""


class PermanentProviderError(ProviderError):
    """Non-retryable failure (unknown symbol, auth failure, licensing)."""


class MarketDataProvider(Protocol):
    name: str
    is_synthetic: bool

    def symbols(self) -> list[str]: ...

    def fetch_bars(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame: ...

    def fetch_actions(
        self, symbol: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame: ...

    def describe(self) -> dict[str, Any]: ...


class SyntheticProvider:
    """Serves a deterministic synthetic market. Data is clearly labelled synthetic."""

    is_synthetic = True

    def __init__(self, spec: SyntheticSpec | None = None) -> None:
        self.name = "synthetic"
        self.market: SyntheticMarket = generate_market(spec)
        self._bars = {s: g for s, g in self.market.bars.groupby("symbol")}
        self._actions = {s: g for s, g in self.market.actions.groupby("symbol")}

    def symbols(self) -> list[str]:
        return sorted(self._bars)

    def fetch_bars(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        if symbol not in self._bars:
            raise PermanentProviderError(f"unknown symbol {symbol}")
        g = self._bars[symbol]
        return g[(g["date"] >= start) & (g["date"] <= end)]

    def fetch_actions(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        g = self._actions.get(symbol)
        if g is None:
            return normalize_actions(None)
        return g[(g["ex_date"] >= start) & (g["ex_date"] <= end)]

    def describe(self) -> dict[str, Any]:
        return dict(self.market.provenance)


class CSVProvider:
    """Reads ``<root>/bars/<SYMBOL>.csv`` and optional ``<root>/actions/<SYMBOL>.csv``.

    Intended for user-supplied data whose licence permits local research use. The caller
    supplies ``source`` and ``license`` strings so provenance is recorded truthfully.
    """

    is_synthetic = False

    def __init__(self, root: str | Path, source: str, license: str) -> None:
        self.root = Path(root)
        self.name = "csv"
        self.source = source
        self.license = license

    def symbols(self) -> list[str]:
        return sorted(p.stem.upper() for p in (self.root / "bars").glob("*.csv"))

    def fetch_bars(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        path = self.root / "bars" / f"{symbol}.csv"
        if not path.exists():
            raise PermanentProviderError(f"no CSV for {symbol}")
        df = pd.read_csv(path)
        df["symbol"] = symbol
        df = normalize_bars(df)
        return df[(df["date"] >= start) & (df["date"] <= end)]

    def fetch_actions(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        path = self.root / "actions" / f"{symbol}.csv"
        if not path.exists():
            return normalize_actions(None)
        df = pd.read_csv(path)
        df["symbol"] = symbol
        df = normalize_actions(df)
        return df[(df["ex_date"] >= start) & (df["ex_date"] <= end)]

    def describe(self) -> dict[str, Any]:
        return {
            "provider": "csv",
            "is_synthetic": False,
            "source": self.source,
            "license": self.license,
            "root": str(self.root),
        }


class TokenBucket:
    """Simple token-bucket rate limiter with injectable clock and sleep for testing."""

    def __init__(
        self,
        rate_per_sec: float,
        burst: int = 1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate_per_sec
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.clock = clock
        self.sleep = sleep
        self.last = clock()

    def acquire(self) -> float:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        waited = 0.0
        if self.tokens < 1.0:
            waited = (1.0 - self.tokens) / self.rate
            self.sleep(waited)
            self.tokens = 1.0
            self.last = self.clock()
        self.tokens -= 1.0
        return waited


@dataclass
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 30.0
    jitter: float = 0.1

    def delay(self, attempt: int, rng: random.Random) -> float:
        d = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        return d * (1 + self.jitter * (2 * rng.random() - 1))


@dataclass
class ResilientProvider:
    """Wraps a provider with throttling and exponential-backoff retries."""

    inner: MarketDataProvider
    limiter: TokenBucket | None = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Callable[[float], None] = time.sleep
    seed: int = 0
    stats: dict[str, int] = field(default_factory=lambda: {"calls": 0, "retries": 0})

    def __post_init__(self) -> None:
        self.name = self.inner.name
        self.is_synthetic = self.inner.is_synthetic
        self._rng = random.Random(self.seed)  # noqa: S311 - backoff jitter, not crypto

    def _call(self, fn: Callable[[], pd.DataFrame]) -> pd.DataFrame:
        attempt = 0
        while True:
            attempt += 1
            if self.limiter:
                self.limiter.acquire()
            self.stats["calls"] += 1
            try:
                return fn()
            except TransientProviderError:
                if attempt >= self.retry.max_attempts:
                    raise
                self.stats["retries"] += 1
                self.sleep(self.retry.delay(attempt, self._rng))

    def symbols(self) -> list[str]:
        return self.inner.symbols()

    def fetch_bars(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        return self._call(lambda: self.inner.fetch_bars(symbol, start, end))

    def fetch_actions(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        return self._call(lambda: self.inner.fetch_actions(symbol, start, end))

    def describe(self) -> dict[str, Any]:
        return self.inner.describe()


@dataclass
class IngestionCheckpoint:
    """Per-symbol high-water marks; persisting this makes backfills resumable."""

    done_through: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)


@dataclass
class IngestionResult:
    bars: pd.DataFrame
    actions: pd.DataFrame
    checkpoint: IngestionCheckpoint
    duplicates_dropped: int
    symbols_ingested: list[str]
    symbols_failed: dict[str, str]


def ingest(
    provider: MarketDataProvider,
    symbols: Iterable[str],
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    existing_bars: pd.DataFrame | None = None,
    existing_actions: pd.DataFrame | None = None,
    checkpoint: IngestionCheckpoint | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> IngestionResult:
    """Incrementally ingest ``symbols``; symbols already complete per checkpoint are skipped.

    Permanent failures are recorded per-symbol and do not abort the whole backfill.
    """
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    cp = checkpoint or IngestionCheckpoint()
    bar_frames = [existing_bars] if existing_bars is not None and len(existing_bars) else []
    act_frames: list[pd.DataFrame] = []  # fetched in this run
    syms = list(symbols)
    ingested: list[str] = []
    for k, sym in enumerate(syms):
        since = s
        if sym in cp.done_through:
            since = max(s, pd.Timestamp(cp.done_through[sym]) + pd.Timedelta(days=1))
        if since > e:
            continue
        try:
            b = provider.fetch_bars(sym, since, e)
            a = provider.fetch_actions(sym, since, e)
        except ProviderError as exc:
            cp.failed[sym] = f"{type(exc).__name__}: {exc}"
            continue
        if len(b):
            bar_frames.append(b)
        if len(a):
            act_frames.append(a)
        cp.done_through[sym] = str(e.date())
        cp.failed.pop(sym, None)
        ingested.append(sym)
        if on_progress:
            on_progress(k + 1, len(syms), sym)
    bars = (
        normalize_bars(pd.concat(bar_frames, ignore_index=True))
        if bar_frames
        else normalize_bars(
            pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "volume"])
        )
    )
    before = len(bars)
    bars = bars.drop_duplicates(["symbol", "date"], keep="last").reset_index(drop=True)
    # Every row a provider returns is kept: two dividends on one ex-date (a regular and a
    # special) are two payments. A re-fetch is the provider's current statement for the
    # (symbol, ex-date, kind) keys it returns, so it replaces those keys in the existing
    # actions instead of adding to them (a corrected amount must not be summed with the
    # old one). Exact duplicates within one fetch are left for the quality gates.
    fetched = normalize_actions(pd.concat(act_frames, ignore_index=True) if act_frames else None)
    old = normalize_actions(existing_actions)
    key = ["symbol", "ex_date", "kind"]
    restated = pd.MultiIndex.from_frame(old[key]).isin(pd.MultiIndex.from_frame(fetched[key]))
    actions = normalize_actions(
        pd.concat([old[~restated], fetched], ignore_index=True)
        if len(old) or len(fetched)
        else None
    ).reset_index(drop=True)
    return IngestionResult(
        bars=bars,
        actions=actions,
        checkpoint=cp,
        duplicates_dropped=before - len(bars),
        symbols_ingested=ingested,
        symbols_failed=dict(cp.failed),
    )
