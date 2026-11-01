"""Bounded streaming of multipart uploads, for routes that authorize before reading.

FastAPI reads a route's ``Form``/``File`` parameters with ``request.form()`` before it
runs any dependency (``fastapi.routing.get_request_handler``). An upload route that
declared them therefore spooled the whole body, up to the 110 MB middleware cap, before
``CurrentUser`` could return 401. Upload routes declare no body parameters instead: the
dependencies authenticate, the handler authorizes, and only then is the body streamed
through ``read_multipart``.

File parts go straight to their destination path, never to the system temp directory,
and each file's limit is enforced as bytes arrive, so an oversized file stops being read
at its limit. Only the declared fields are accepted, each at most once.

Unused file inputs: a file part with an empty filename and no content, or an empty text
value for a file field, is treated as absent, as FastAPI did. A file part with an empty
filename but some content is kept. Text fields must be UTF-8 without NUL characters
(PostgreSQL cannot store NUL in text; it used to surface as a 500).
"""

from __future__ import annotations

import errno
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import anyio
from fastapi.exceptions import RequestValidationError
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect, Request

from quantlab_api.errors import ApiError

MAX_PART_HEADER_BYTES = 8 * 1024
_NO_SPACE = {errno.ENOSPC, errno.EDQUOT}


@dataclass
class Upload:
    fields: dict[str, str]
    # Bytes written per file field. An unused file input (empty filename and no content,
    # or an empty text value, which FastAPI also treated as absent) is omitted.
    files: dict[str, int]


@dataclass
class _Part:
    name: str = ""
    is_file: bool = False
    dest: Path | None = None
    named: bool = False  # the file part carried a non-empty filename
    empty_only: bool = False  # a text part for a file field: only "" is accepted
    limit: int = 0
    size: int = 0
    data: bytearray = field(default_factory=bytearray)
    out: BinaryIO | None = None


# Longest part name echoed back in an error: names come from the client, and a part's
# headers may be up to MAX_PART_HEADER_BYTES long.
MAX_ECHOED_NAME = 64


def invalid(name: str, msg: str, kind: str) -> RequestValidationError:
    """A 422 in the same shape FastAPI gives for body validation errors."""
    if len(name) > MAX_ECHOED_NAME:
        name = name[:MAX_ECHOED_NAME] + "…"
    return RequestValidationError([{"loc": ("body", name), "msg": msg, "type": kind}])


def _write(fh: BinaryIO, data: bytes) -> None:
    fh.write(data)


class _Reader:
    def __init__(self, files: Mapping[str, tuple[Path, int]], fields: Mapping[str, int]):
        self.file_specs = files
        self.field_limits = fields
        self.seen: set[str] = set()
        self.fields: dict[str, str] = {}
        self.files: dict[str, int] = {}
        self.part = _Part()
        self.header_bytes = 0
        self.header_name = b""
        self.header_value = b""
        self.disposition = b""
        self.ended = False
        # Callbacks are synchronous; file I/O is queued and run in a thread afterwards.
        self.ops: list[tuple[str, _Part, bytes]] = []
        self.open_parts: list[_Part] = []

    def callbacks(self) -> Any:
        return {
            "on_part_begin": self.on_part_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_end": self.on_end,
        }

    def on_part_begin(self) -> None:
        self.part = _Part()
        self.header_bytes = 0
        self.disposition = b""

    def _header_chunk(self, n: int) -> None:
        self.header_bytes += n
        if self.header_bytes > MAX_PART_HEADER_BYTES:
            raise ApiError(400, "invalid_multipart", "Multipart part headers are too large")

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_chunk(end - start)
        self.header_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_chunk(end - start)
        self.header_value += data[start:end]

    def on_header_end(self) -> None:
        if self.header_name.lower() == b"content-disposition":
            self.disposition = self.header_value
        self.header_name = self.header_value = b""

    def on_headers_finished(self) -> None:
        kind, options = parse_options_header(self.disposition)
        raw_name = options.get(b"name")
        if kind.lower() != b"form-data" or raw_name is None:
            raise ApiError(400, "invalid_multipart", "Each part needs a form-data name")
        name = raw_name.decode("utf-8", "replace")
        is_file = b"filename" in options
        if name not in self.file_specs and name not in self.field_limits:
            raise invalid(name, "Unexpected field", "extra_forbidden")
        if name in self.seen:
            raise invalid(name, "Field given more than once", "duplicate")
        self.seen.add(name)
        if is_file and name not in self.file_specs:
            raise invalid(name, "Expected a text field", "type_error")
        p = self.part
        p.name, p.is_file = name, is_file
        if is_file:
            p.dest, p.limit = self.file_specs[name]
            p.named = bool(options[b"filename"])
        elif name in self.file_specs:
            p.empty_only = True
        else:
            p.limit = self.field_limits[name]

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        p = self.part
        p.size += end - start
        if p.empty_only:
            raise invalid(p.name, "Expected a file", "type_error")
        if p.size > p.limit:
            if p.is_file:
                mb = p.limit // 2**20
                raise ApiError(413, "upload_too_large", f"Uploads are limited to {mb} MiB")
            raise invalid(p.name, "Value is too long", "string_too_long")
        if p.is_file:
            self.ops.append(("write", p, data[start:end]))
        else:
            p.data += data[start:end]

    def on_part_end(self) -> None:
        p = self.part
        if p.empty_only or (p.is_file and not p.named and p.size == 0):
            return  # an unused file input; nothing was written
        if p.is_file:
            self.files[p.name] = p.size
            self.ops.append(("close", p, b""))
            return
        try:
            value = p.data.decode("utf-8")
        except UnicodeDecodeError:
            raise invalid(p.name, "Value is not valid UTF-8", "string_unicode") from None
        if "\x00" in value:
            raise invalid(p.name, "Value must not contain NUL characters", "string_pattern")
        self.fields[p.name] = value

    def on_end(self) -> None:
        self.ended = True

    async def flush(self) -> None:
        ops, self.ops = self.ops, []
        for op, p, data in ops:
            # Opened on first use, so an unused file input creates no file.
            if p.out is None:
                assert p.dest is not None
                p.out = await anyio.to_thread.run_sync(p.dest.open, "wb")
                self.open_parts.append(p)
            if op == "write":
                await anyio.to_thread.run_sync(_write, p.out, data)
            else:
                await anyio.to_thread.run_sync(p.out.close)

    def close_all(self) -> None:
        for p in self.open_parts:
            if p.out is not None and not p.out.closed:
                p.out.close()


async def read_multipart(
    request: Request,
    *,
    files: Mapping[str, tuple[Path, int]],
    fields: Mapping[str, int],
) -> Upload:
    """Stream a ``multipart/form-data`` body.

    ``files`` maps each accepted file field to its destination and byte limit; ``fields``
    maps each accepted text field to its byte limit. The caller removes the destination
    files if this raises. Errors: 415 not multipart, 400 malformed or truncated, 413 a
    file over its limit, 422 an unexpected, repeated, mistyped or overlong field, 507
    out of disk space.
    """
    ctype, params = parse_options_header(request.headers.get("content-type"))
    if ctype.lower() != b"multipart/form-data":
        raise ApiError(415, "unsupported_media_type", "Expected a multipart/form-data body")
    boundary = params.get(b"boundary")
    if not boundary:
        raise ApiError(400, "invalid_multipart", "Missing multipart boundary")
    reader = _Reader(files, fields)
    try:
        parser = MultipartParser(boundary, reader.callbacks())
        async for chunk in request.stream():
            parser.write(chunk)
            await reader.flush()
            if reader.ended:
                break  # never read an epilogue after the closing boundary
        parser.finalize()
        await reader.flush()
    except FormParserError as exc:
        raise ApiError(400, "invalid_multipart", "Invalid multipart body") from exc
    except ClientDisconnect as exc:
        raise ApiError(400, "upload_incomplete", "The client disconnected mid-upload") from exc
    except OSError as exc:
        if exc.errno in _NO_SPACE:
            raise ApiError(
                507, "insufficient_storage", "The server is out of upload space; try later"
            ) from exc
        raise
    finally:
        reader.close_all()
    if not reader.ended:
        raise ApiError(400, "invalid_multipart", "The multipart body is truncated")
    return Upload(fields=reader.fields, files=reader.files)
