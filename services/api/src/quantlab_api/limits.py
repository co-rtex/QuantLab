"""Request body size limits enforced before any parsing (pure ASGI middleware)."""

from __future__ import annotations

from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
