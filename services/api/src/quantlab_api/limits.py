"""Request body size limits enforced before any parsing (pure ASGI middleware), and the
memory bound a backtest must fit at submission (#163)."""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from quantlab.backtest.engine import rebalance_mask
from quantlab.data.calendar import trading_days
from quantlab_api.errors import ApiError

DEFAULT_LIMIT = 2 * 2**20
# Upload routes: two files of up to 50 MB each plus multipart overhead.
UPLOAD_LIMIT = 110 * 2**20
UPLOAD_SUFFIXES = ("/datasets/csv",)


class BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Rejects bodies over the limit from the Content-Length header, and counts streamed
    bytes for chunked requests, so oversized uploads are never buffered or parsed."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        limit = UPLOAD_LIMIT if path.endswith(UPLOAD_SUFFIXES) else DEFAULT_LIMIT
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await _reject(send, limit)
            return
        received = 0
        exceeded = False
        started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise BodyTooLarge
            return message

        # Once the limit is passed the answer is 413, whatever the app made of the
        # interrupted read: FastAPI's JSON parsing catches the exception and would answer
        # 400 "error parsing the body" to a chunked client (#8).
        async def guarded_send(message: Message) -> None:
            nonlocal started
            if exceeded:
                if not started:
                    started = True
                    await _reject(send, limit)
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except BodyTooLarge:
            if not started:
                started = True
                await _reject(send, limit)


async def _reject(send: Send, limit: int) -> None:
    body = (
        b'{"error":{"code":"payload_too_large","message":"Request body exceeds '
        + str(limit // 2**20).encode()
        + b' MB","details":null,"request_id":null}}'
    )
    start: dict[str, Any] = {
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    }
    await send(start)
    await send({"type": "http.response.body", "body": body})


# --------------------------------------------------------------- backtest memory (#163)
# Peak anonymous memory of one backtest job in the worker container, as an envelope over
# measured jobs: benchmarks/worker_memory.py on the 1536 MB compose worker, synthetic data,
# 2005-2023 (4,785 sessions), 100-400 symbols, equal weight (which trades every name at
# every rebalance, the worst case for result rows) at daily, weekly and monthly
# rebalancing. Least squares on the jobs clear of the limit, then the base raised until
# every one is covered. Raw results: benchmarks/results/; method: docs/RUNBOOK.md. The
# base includes the RQ parent and the forked horse's copy of it.
BACKTEST_BASE_MB = 372.3
BACKTEST_MB_PER_MILLION_SYMBOL_SESSIONS = 320.1  # bars loaded and position rows
BACKTEST_MB_PER_MILLION_SYMBOL_REBALANCES = 574.2  # worst-case trades and decisions
# The measured range (#174 review). Every measured job had 4,785 sessions, so sessions and
# symbols were not varied separately; what was measured is their products, up to
# 400 x 4,785. A backtest beyond those (or beyond 400 symbols) is refused rather than
# estimated. Per-session costs that do not scale with symbols (the calendar, the equity
# record) are about 1 KB a session, negligible, so a long history of few symbols is inside.
MEASURED_MAX_SYMBOLS = 400
MEASURED_MAX_SYMBOL_SESSIONS = 400 * 4785
MEASURED_MAX_SYMBOL_REBALANCES = 400 * 4785
# What reduces a backtest's memory. The job loads the data from its first bar to the end,
# whatever the start, so a later start shortens only the traded window (rebalances).
ADVICE = (
    "Use fewer symbols, a less frequent rebalance or an earlier end date. The whole history "
    "up to the end date is loaded whatever the start, so a later start helps only by "
    "cutting rebalances."
)


@dataclass(frozen=True)
class BacktestCost:
    symbols: int
    sessions: int
    rebalances: int

    @property
    def memory_mb(self) -> float:
        return (
            BACKTEST_BASE_MB
            + BACKTEST_MB_PER_MILLION_SYMBOL_SESSIONS * self.symbols * self.sessions / 1e6
            + BACKTEST_MB_PER_MILLION_SYMBOL_REBALANCES * self.symbols * self.rebalances / 1e6
        )


def universe_width(catalog: Iterable[tuple[str, str | None]], universe: list[str] | None) -> int:
    """How many instruments the job simulates, from the version's catalog rows
    ``(ticker, instrument_id)`` (#181).

    A universe names each ticker once, however often it is listed: the engine deduplicates
    it. Without one, the job runs every instrument of the version, and a reused ticker has
    a catalog row per holder, so instruments are counted, not tickers (a version without
    identity data has one instrument per ticker). The benchmark is counted too, although
    the engine leaves it out, which overestimates by one at most.
    """
    if universe:
        return len({s.strip().upper() for s in universe})
    return len({iid if iid is not None else symbol for symbol, iid in catalog})


def backtest_cost(
    symbols: int,
    data_start: date,
    data_end: date,
    start: date | None,
    end: date | None,
    rebalance: str,
    bar_dates: pd.DatetimeIndex | None = None,
) -> BacktestCost:
    """The job's size from what is known at submission.

    Sessions run from the data's first session, since the job loads that history. The
    engine steps through the version's distinct bar dates (``bar_dates``, up to the end),
    which may include days the exchange calendar does not (such bars only warn), and it
    rebalances on those dates too (#181). Sessions and rebalances (over the traded window,
    from ``start``) are therefore each the larger count, on the calendar or on the dates.
    """
    last = min(end, data_end) if end else data_end
    loaded = trading_days(data_start, last)
    candidates = [loaded]
    if bar_dates is not None:
        candidates.append(bar_dates[bar_dates <= pd.Timestamp(last)])
    sessions = max(len(days) for days in candidates)
    rebalances = max(
        int(rebalance_mask(days[days >= pd.Timestamp(start)] if start else days, rebalance).sum())
        for days in candidates
    )
    return BacktestCost(symbols, sessions, rebalances)


def check_backtest_memory(cost: BacktestCost, limit_mb: int) -> None:
    """Reject at submission a backtest whose estimated peak would not fit the worker, or
    that lies outside the range the estimate was measured on."""
    outside = (
        cost.symbols > MEASURED_MAX_SYMBOLS
        or cost.symbols * cost.sessions > MEASURED_MAX_SYMBOL_SESSIONS
        or cost.symbols * cost.rebalances > MEASURED_MAX_SYMBOL_REBALANCES
    )
    if outside:
        raise ApiError(
            422,
            "backtest_too_large",
            f"This backtest has {cost.symbols} symbols, {cost.sessions:,} sessions loaded and "
            f"{cost.rebalances:,} rebalances. Memory was measured only up to "
            f"{MEASURED_MAX_SYMBOLS} symbols over 4,785 sessions, so larger backtests are "
            f"refused. {ADVICE}",
            {
                "reason": "outside_measured_range",
                "symbols": cost.symbols,
                "sessions": cost.sessions,
                "rebalances": cost.rebalances,
                "max_symbols": MEASURED_MAX_SYMBOLS,
                "max_symbol_sessions": MEASURED_MAX_SYMBOL_SESSIONS,
                "max_symbol_rebalances": MEASURED_MAX_SYMBOL_REBALANCES,
            },
        )
    if cost.memory_mb > limit_mb:
        # Rounded up to 0.1 MB, so the figure shown is over the limit whenever the estimate
        # is: 1228.04 MB showed as "1228 MB, over the 1228 MB limit" (#181).
        shown = math.ceil(cost.memory_mb * 10) / 10
        raise ApiError(
            422,
            "backtest_too_large",
            f"This backtest needs an estimated {shown:.1f} MB, over the "
            f"{limit_mb} MB limit for one backtest. {ADVICE}",
            {
                "reason": "estimated_memory",
                "estimated_mb": shown,
                "limit_mb": limit_mb,
                "symbols": cost.symbols,
                "sessions": cost.sessions,
                "rebalances": cost.rebalances,
            },
        )


# ------------------------------------------------------------ optimization memory (#224)
# Peak anonymous memory of one optimization job in the worker container, as an envelope
# over measured jobs: benchmarks/optimize_memory.py on the 1536 MB compose worker (one
# replica, recreated per job, sampled every 0.1 s), synthetic data of 756, 3,275 and 7,056
# sessions, 10, 50 and 100 symbols plus a benchmark, every method, look-back 1260 (raw
# results: benchmarks/results/20261101T1700Z-optimize-memory-8afffa4.json). Least squares
# on all nine jobs, then the base raised, rounded up to 0.1 MB, until every one is covered.
# The base includes the RQ parent and the forked horse's copy of it. The job loads each
# symbol's whole history up to as_of, so the cost is in symbol-sessions, whatever the
# look-back.
OPTIMIZE_BASE_MB = 332.7
OPTIMIZE_MB_PER_MILLION_SYMBOL_SESSIONS = 461.4
# The largest measured: 101 series over 7,056 sessions. Beyond it a job is refused rather
# than estimated; within it the estimate peaks at 661.5 MB.
OPTIMIZE_MEASURED_MAX_SYMBOL_SESSIONS = 101 * 7056


@dataclass(frozen=True)
class OptimizeCost:
    symbols: int  # the requested symbols and the benchmark
    sessions: int  # loaded: from the data's first session to as_of

    @property
    def memory_mb(self) -> float:
        return (
            OPTIMIZE_BASE_MB
            + OPTIMIZE_MB_PER_MILLION_SYMBOL_SESSIONS * self.symbols * self.sessions / 1e6
        )


def optimize_cost(
    symbols: int,
    data_start: date,
    data_end: date,
    as_of: date | None,
    bar_dates: pd.DatetimeIndex | None = None,
) -> OptimizeCost:
    """The job's size from what is known at submission: the sessions from the data's first
    to ``as_of`` (or the data's last). As for backtests, the larger of the exchange
    calendar's count and the version's own distinct bar dates (``bar_dates``), which can
    include days the calendar does not (#181)."""
    last = min(as_of, data_end) if as_of else data_end
    sessions = len(trading_days(data_start, last))
    if bar_dates is not None:
        sessions = max(sessions, int((bar_dates <= pd.Timestamp(last)).sum()))
    return OptimizeCost(symbols, sessions)


def check_optimize_memory(cost: OptimizeCost, limit_mb: int) -> None:
    """Reject at submission an optimization outside the measured range, or whose estimated
    peak would not fit the worker."""
    details = {"symbols": cost.symbols, "sessions": cost.sessions}
    advice = "Use fewer symbols or an earlier as_of: the whole history up to as_of is loaded."
    if cost.symbols * cost.sessions > OPTIMIZE_MEASURED_MAX_SYMBOL_SESSIONS:
        raise ApiError(
            422,
            "optimization_too_large",
            f"This optimization loads {cost.symbols} series over {cost.sessions:,} sessions. "
            f"Memory was measured only up to {OPTIMIZE_MEASURED_MAX_SYMBOL_SESSIONS:,} "
            f"series-sessions, so larger ones are refused. {advice}",
            {
                **details,
                "reason": "outside_measured_range",
                "max_symbol_sessions": OPTIMIZE_MEASURED_MAX_SYMBOL_SESSIONS,
            },
        )
    if cost.memory_mb > limit_mb:
        shown = math.ceil(cost.memory_mb * 10) / 10  # never shown under the limit (#181)
        raise ApiError(
            422,
            "optimization_too_large",
            f"This optimization needs an estimated {shown:.1f} MB, over the {limit_mb} MB "
            f"limit for one job. {advice}",
            {**details, "reason": "estimated_memory", "estimated_mb": shown, "limit_mb": limit_mb},
        )


# ------------------------------------------------------- heavy request-time work (#223)
# Routes that compute over full price histories in the API process itself: the portfolio
# ledger (optimization has been a worker job since #224). FastAPI runs a sync route in its
# process's thread pool, up to 40 at once, so concurrent heavy requests stacked in memory:
# 16 optimizations of 100 symbols drove the Docker VM into a global OOM, in which the
# kernel killed API processes and could have killed Postgres. Each process admits at most
# heavy_requests_per_process of them at a time; more are refused at once, never queued.
_heavy_lock = threading.Lock()
_heavy: threading.BoundedSemaphore | None = None
HEAVY_RETRY_AFTER_SECONDS = 2


def _heavy_semaphore() -> threading.BoundedSemaphore:
    global _heavy
    with _heavy_lock:
        if _heavy is None:
            from quantlab_api.config import get_settings

            _heavy = threading.BoundedSemaphore(get_settings().heavy_requests_per_process)
        return _heavy


def reset_heavy_slots() -> None:
    """Test hook: the next heavy request reads the setting again."""
    global _heavy
    with _heavy_lock:
        _heavy = None


@contextmanager
def heavy_slot(route: str) -> Iterator[None]:
    """Hold one of this process's heavy-work slots for the block, or refuse with 503."""
    from quantlab_api.observability import HEAVY_REFUSED

    sem = _heavy_semaphore()
    if not sem.acquire(blocking=False):
        HEAVY_REFUSED.labels(route).inc()
        raise ApiError(
            503,
            "server_busy",
            "The server is busy with other analyses; try again in a few seconds.",
            {"retry_after_seconds": HEAVY_RETRY_AFTER_SECONDS},
            headers={"Retry-After": str(HEAVY_RETRY_AFTER_SECONDS)},
        )
    try:
        yield
    finally:
        sem.release()
