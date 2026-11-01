"""Request body size limits enforced before any parsing (pure ASGI middleware), and the
memory bound a backtest must fit at submission (#163)."""

from __future__ import annotations

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


def backtest_cost(
    symbols: int,
    data_start: date,
    data_end: date,
    start: date | None,
    end: date | None,
    rebalance: str,
) -> BacktestCost:
    """The job's size from what is known at submission. Sessions run from the data's first
    session, since the job loads that history; rebalances count over the traded window."""
    last = min(end, data_end) if end else data_end
    loaded = trading_days(data_start, last)
    traded = loaded[loaded >= pd.Timestamp(start)] if start else loaded
    return BacktestCost(symbols, len(loaded), int(rebalance_mask(traded, rebalance).sum()))


def check_backtest_memory(cost: BacktestCost, limit_mb: int) -> None:
    """Reject at submission a backtest whose estimated peak would not fit the worker."""
    if cost.memory_mb > limit_mb:
        raise ApiError(
            422,
            "backtest_too_large",
            f"This backtest needs an estimated {cost.memory_mb:.0f} MB, over the "
            f"{limit_mb} MB limit for one backtest. Use fewer symbols, a shorter period "
            "or a less frequent rebalance.",
            {
                "estimated_mb": round(cost.memory_mb, 1),
                "limit_mb": limit_mb,
                "symbols": cost.symbols,
                "sessions": cost.sessions,
                "rebalances": cost.rebalances,
            },
        )
