"""Upload routes authorize before they read the body (#4).

These tests drive the ASGI app directly with a lazily generated body and count the bytes
the application pulls from ``receive()``, so they measure what the server actually reads
and writes rather than only the status code. ``SpooledTemporaryFile`` creation is counted
because spooled files are unlinked on creation and never show up in a directory listing.
"""

from __future__ import annotations

import errno
import shutil
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import jwt
import pytest

from tests.integration.conftest import register, run_pending_jobs, workspace_id

BOUNDARY = "qlboundary7MA4YWxkTrZu0gW"
CHUNK = 64 * 1024
BIG = 100 * 2**20  # under the 110 MB middleware cap, so only authorization can stop it
FIELDS = {
    "slug": "big-data",
    "name": "Big",
    "source": "Broker export",
    "license": "Personal research use",
}
BARS_HEAD = b"symbol,date,open,high,low,close,volume\n"
ROW = b"abc,2024-01-02,10,10.5,9.8,10.2,1000\n"


def _field(name: str, value: str) -> bytes:
    return (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
    ).encode()


def _file_head(name: str, filename: str = "bars.csv") -> bytes:
    return (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"; '
        f'filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n'
    ).encode()


def multipart_body(
    file_bytes: int, fields: dict[str, str] | None = None, *, tail: bool = True
) -> tuple[int, Iterator[bytes]]:
    """A multipart body whose ``bars`` part holds about ``file_bytes`` of CSV rows,
    generated lazily so a 100 MB request costs no test memory."""
    head = b"".join(_field(k, v) for k, v in (fields or FIELDS).items())
    head += _file_head("bars") + BARS_HEAD
    rows = max(file_bytes - len(BARS_HEAD), 0) // len(ROW)
    end = f"\r\n--{BOUNDARY}--\r\n".encode() if tail else b""
    total = len(head) + rows * len(ROW) + len(end)

    def gen() -> Iterator[bytes]:
        yield head
        per_chunk = CHUNK // len(ROW)
        block = ROW * per_chunk
        left = rows
        while left >= per_chunk:
            yield block
            left -= per_chunk
        if left:
            yield ROW * left
        if end:
            yield end

    return total, gen()


class Result:
    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body = b""
        self.pulled = 0

    def json(self) -> Any:
        import json

        return json.loads(self.body)


async def drive(
    path: str,
    chunks: Iterator[bytes],
    *,
    headers: dict[str, str] | None = None,
    content_length: int | None = None,
    content_type: str = f"multipart/form-data; boundary={BOUNDARY}",
    disconnect_after: int | None = None,
    on_pull: Any = None,
) -> Result:
    """Run one request through the real middleware stack, counting bytes pulled."""
    from quantlab_api.main import app

    res = Result()
    raw = [(b"content-type", content_type.encode()), (b"host", b"testserver")]
    if content_length is not None:
        raw.append((b"content-length", str(content_length).encode()))
    for k, v in (headers or {}).items():
        raw.append((k.lower().encode(), v.encode()))
    scope = {
        "type": "http",
        # uvicorn 0.54 advertises 2.3, which enables Starlette's disconnect listener on
        # streaming responses; test with what production runs.
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": raw,
        "client": ("203.0.113.9", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    sent_all = False
    calls = 0

    async def receive() -> dict[str, Any]:
        nonlocal sent_all, calls
        calls += 1
        if disconnect_after is not None and calls > disconnect_after:
            return {"type": "http.disconnect"}
        if sent_all:
            await anyio.sleep_forever()
        chunk = next(chunks, None)
        if chunk is None:
            sent_all = True
            return {"type": "http.request", "body": b"", "more_body": False}
        res.pulled += len(chunk)
        if on_pull is not None:
            await anyio.to_thread.run_sync(on_pull, calls)
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            res.status = message["status"]
            res.headers = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            res.body += message.get("body", b"")

    await app(scope, receive, send)
    return res


def run(coro: Any) -> Any:
    return anyio.run(lambda: coro)


@pytest.fixture
def spool_counter(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Counts SpooledTemporaryFile objects created by Starlette's form parser."""
    import starlette.formparsers as fp

    created: list[int] = []
    real = fp.SpooledTemporaryFile

    def counting(*args: Any, **kwargs: Any) -> Any:
        created.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(fp, "SpooledTemporaryFile", counting)
    return created


@pytest.fixture
def uploads_dir() -> Iterator[Any]:
    from quantlab_api.config import get_settings

    d = get_settings().data_dir / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    before = set(d.iterdir())

    class Watch:
        def new(self) -> set[Any]:
            return set(d.iterdir()) - before

    yield Watch()
    # Accepted uploads whose jobs were never run keep their staging directories.
    for p in Watch().new():
        shutil.rmtree(p, ignore_errors=True)


def _csv_path(ws: str) -> str:
    return f"/api/v1/workspaces/{ws}/datasets/csv"


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _token(claims: dict[str, Any], key: str | None = None) -> str:
    from quantlab_api.config import get_settings

    s = get_settings()
    return jwt.encode(claims, key or s.signing_key, algorithm=s.jwt_algorithm)


def _rejected_cheaply(res: Result, status: int, spool: list[int], uploads: Any) -> None:
    assert res.status == status, res.body
    # Nothing of the 100 MB body was read: authorization ran before the body.
    assert res.pulled == 0, f"{res.pulled} bytes read before rejection"
    assert spool == []
    assert uploads.new() == set()


def test_unauthenticated_large_upload_reads_no_body(client, spool_counter, uploads_dir):
    ws = str(uuid.uuid4())
    total, chunks = multipart_body(BIG)
    res = run(drive(_csv_path(ws), chunks, content_length=total))
    _rejected_cheaply(res, 401, spool_counter, uploads_dir)
    assert res.json()["error"]["code"] == "not_authenticated"


def test_chunked_unauthenticated_upload_reads_no_body(client, spool_counter, uploads_dir):
    _, chunks = multipart_body(BIG)
    res = run(drive(_csv_path(str(uuid.uuid4())), chunks, content_length=None))
    _rejected_cheaply(res, 401, spool_counter, uploads_dir)


@pytest.mark.parametrize("kind", ["garbage", "wrong_key", "expired", "revoked", "no_sub"])
def test_bad_tokens_are_rejected_before_the_body(
    client, spool_counter, uploads_dir, kind: str
) -> None:
    h = register(client)
    ws = workspace_id(client, h)
    me = client.get("/api/v1/auth/me", headers=h).json()
    now = datetime.now(UTC)
    base = {"sub": me["id"], "ver": 0, "iss": "quantlab", "iat": now, "exp": now + timedelta(1)}
    token = {
        "garbage": "not-a-jwt",
        "wrong_key": _token(base, key="x" * 64),
        "expired": _token({**base, "exp": now - timedelta(seconds=5)}),
        "revoked": _token({**base, "ver": 999}),
        "no_sub": _token({k: v for k, v in base.items() if k != "sub"}),
    }[kind]
    total, chunks = multipart_body(BIG)
    res = run(drive(_csv_path(ws), chunks, headers=_bearer(token), content_length=total))
    _rejected_cheaply(res, 401, spool_counter, uploads_dir)
    expected = "token_expired" if kind == "expired" else "invalid_token"
    assert res.json()["error"]["code"] == expected


def test_valid_token_with_wrong_signature_algorithm_none_is_rejected(
    client, spool_counter, uploads_dir
):
    h = register(client)
    ws = workspace_id(client, h)
    me = client.get("/api/v1/auth/me", headers=h).json()
    unsigned = jwt.encode(
        {"sub": me["id"], "ver": 0, "iss": "quantlab", "exp": datetime.now(UTC) + timedelta(1)},
        key=None,
        algorithm="none",
    )
    total, chunks = multipart_body(BIG)
    res = run(drive(_csv_path(ws), chunks, headers=_bearer(unsigned), content_length=total))
    _rejected_cheaply(res, 401, spool_counter, uploads_dir)


def _add_member(ws: str, email: str, role: str) -> None:
    from sqlalchemy import select

    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Role, User, WorkspaceMember

    with SessionLocal() as db:
        uid = db.scalar(select(User.id).where(User.email == email))
        db.add(WorkspaceMember(workspace_id=uuid.UUID(ws), user_id=uid, role=Role(role)))
        db.commit()


def test_outsider_and_viewer_are_rejected_before_the_body(client, spool_counter, uploads_dir):
    owner = register(client, "owner@example.com")
    ws = workspace_id(client, owner)
    outsider = register(client, "outsider@example.com")
    viewer = register(client, "viewer@example.com")
    _add_member(ws, "viewer@example.com", "viewer")
    for headers, status in ((outsider, 404), (viewer, 403)):
        total, chunks = multipart_body(BIG)
        auth = {"authorization": headers["Authorization"]}
        res = run(drive(_csv_path(ws), chunks, headers=auth, content_length=total))
        _rejected_cheaply(res, status, spool_counter, uploads_dir)


def test_session_cookie_is_accepted(client, uploads_dir):
    """The web app authenticates with the httpOnly cookie, not a bearer header."""
    h = register(client)
    ws = workspace_id(client, h)
    token = h["Authorization"].removeprefix("Bearer ")
    total, chunks = multipart_body(10_000)
    res = run(
        drive(
            _csv_path(ws),
            chunks,
            headers={"cookie": f"quantlab_session={token}"},
            content_length=total,
        )
    )
    assert res.status == 202, res.body
    assert run_pending_jobs() == ["completed"]


def test_concurrent_unauthenticated_uploads_read_nothing(client, spool_counter, uploads_dir):
    results: list[Result] = []

    async def many() -> None:
        async def one() -> None:
            total, chunks = multipart_body(BIG)
            results.append(await drive(_csv_path(str(uuid.uuid4())), chunks, content_length=total))

        async with anyio.create_task_group() as tg:
            for _ in range(8):
                tg.start_soon(one)

    anyio.run(many)
    assert [r.status for r in results] == [401] * 8
    assert sum(r.pulled for r in results) == 0
    assert spool_counter == []
    assert uploads_dir.new() == set()


def _authorized(client) -> tuple[dict[str, str], str]:  # type: ignore[no-untyped-def]
    h = register(client)
    return {"authorization": h["Authorization"]}, workspace_id(client, h)


def test_oversized_authenticated_file_stops_reading_at_the_limit(
    client, spool_counter, uploads_dir
):
    h, ws = _authorized(client)
    total, chunks = multipart_body(BIG)  # 100 MB file; the per-file limit is 50 MB
    res = run(drive(_csv_path(ws), chunks, headers=h, content_length=total))
    assert res.status == 413, res.body
    assert res.json()["error"]["code"] == "upload_too_large"
    # Reading stopped within one chunk of the 50 MB file limit, not at the 110 MB cap.
    assert 50 * 2**20 < res.pulled <= 50 * 2**20 + 2 * CHUNK
    assert spool_counter == []
    assert uploads_dir.new() == set()


def test_authorized_upload_does_not_use_the_system_temp_dir(client, spool_counter, uploads_dir):
    h, ws = _authorized(client)
    total, chunks = multipart_body(5 * 2**20)
    res = run(drive(_csv_path(ws), chunks, headers=h, content_length=total))
    assert res.status == 202, res.body
    assert res.pulled == total
    assert spool_counter == []
    (staged,) = uploads_dir.new()
    assert sorted(p.name for p in staged.iterdir()) == ["bars.csv"]
    assert (staged / "bars.csv").read_bytes().startswith(BARS_HEAD)
    assert run_pending_jobs() == ["completed"]


def _parts(*parts: bytes, end: bool = True) -> tuple[int, Iterator[bytes]]:
    body = b"".join(parts) + (f"--{BOUNDARY}--\r\n".encode() if end else b"")
    return len(body), iter([body])


def _bars_part(content: bytes = BARS_HEAD + ROW, name: str = "bars", filename: str = "b.csv"):
    return _file_head(name, filename) + content + b"\r\n"


@pytest.mark.parametrize(
    ("case", "status", "code"),
    [
        ("unknown_field", 422, "validation_error"),
        ("duplicate_file", 422, "validation_error"),
        ("duplicate_field", 422, "validation_error"),
        ("file_as_field", 422, "validation_error"),
        ("field_as_file", 422, "validation_error"),
        ("missing_bars", 422, "validation_error"),
        ("empty_bars_filename", 422, "validation_error"),
        ("missing_license", 422, "validation_error"),
        ("bad_slug", 422, "validation_error"),
        ("long_field", 422, "validation_error"),
        ("non_utf8_field", 422, "validation_error"),
        ("no_name", 400, "invalid_multipart"),
        ("huge_part_headers", 400, "invalid_multipart"),
        ("truncated", 400, "invalid_multipart"),
        ("garbage", 400, "invalid_multipart"),
    ],
)
def test_malformed_multipart_is_rejected_and_cleaned_up(
    client, spool_counter, uploads_dir, case: str, status: int, code: str
) -> None:
    h, ws = _authorized(client)
    fields = b"".join(_field(k, v) for k, v in FIELDS.items())
    bars = _bars_part()
    bodies = {
        "unknown_field": (fields, _field("extra", "x"), bars),
        "duplicate_file": (fields, bars, bars),
        "duplicate_field": (fields, _field("slug", "other"), bars),
        "file_as_field": (fields, _file_head("slug") + b"x\r\n", bars),
        "field_as_file": (fields, _field("bars", "x")),
        "missing_bars": (fields,),
        "empty_bars_filename": (fields, _bars_part(b"", filename="")),
        "missing_license": (
            b"".join(_field(k, v) for k, v in FIELDS.items() if k != "license"),
            bars,
        ),
        "bad_slug": (
            _field("slug", "../etc"),
            *(_field(k, v) for k, v in FIELDS.items() if k != "slug"),
            bars,
        ),
        "long_field": (_field("source", "s" * 5000), bars),
        "non_utf8_field": (_field("name", "x")[:-3] + b"\xff\xfe\r\n", bars),
        "no_name": (
            f"--{BOUNDARY}\r\nContent-Disposition: form-data\r\n\r\nx\r\n".encode(),
            bars,
        ),
        "huge_part_headers": (
            f"--{BOUNDARY}\r\nX-Pad: {'a' * 20000}\r\n".encode()
            + b'Content-Disposition: form-data; name="slug"\r\n\r\nx\r\n',
            bars,
        ),
    }
    if case == "truncated":
        total, chunks = multipart_body(1000, tail=False)
    elif case == "garbage":
        total, chunks = 11, iter([b"not a form!"])
    else:
        total, chunks = _parts(*bodies[case])
    res = run(drive(_csv_path(ws), chunks, headers=h, content_length=total))
    assert res.status == status, (case, res.body)
    assert res.json()["error"]["code"] == code
    assert spool_counter == []
    assert uploads_dir.new() == set()


def test_non_multipart_content_type_is_rejected(client, uploads_dir):
    h, ws = _authorized(client)
    res = run(
        drive(
            _csv_path(ws),
            iter([b'{"slug": "x"}']),
            headers=h,
            content_length=13,
            content_type="application/json",
        )
    )
    assert res.status == 415, res.body
    assert res.pulled == 0
    assert uploads_dir.new() == set()


def test_empty_optional_actions_file_is_ignored(client, uploads_dir):
    """Browsers send an empty part with filename="" for an unused file input."""
    h, ws = _authorized(client)
    fields = b"".join(_field(k, v) for k, v in FIELDS.items())
    total, chunks = _parts(fields, _bars_part(), _bars_part(b"", name="actions", filename=""))
    res = run(drive(_csv_path(ws), chunks, headers=h, content_length=total))
    assert res.status == 202, res.body
    (staged,) = uploads_dir.new()
    assert sorted(p.name for p in staged.iterdir()) == ["bars.csv"]


def test_disk_full_returns_507_and_removes_partial_files(client, monkeypatch, uploads_dir):
    import quantlab_api.uploads as uploads

    h, ws = _authorized(client)
    real = uploads._write

    def full(fh: Any, data: bytes) -> None:
        if fh.tell() > 2**20:
            raise OSError(errno.ENOSPC, "No space left on device")
        real(fh, data)

    monkeypatch.setattr(uploads, "_write", full)
    total, chunks = multipart_body(5 * 2**20)
    res = run(drive(_csv_path(ws), chunks, headers=h, content_length=total))
    assert res.status == 507, res.body
    assert res.json()["error"]["code"] == "insufficient_storage"
    assert res.pulled < 3 * 2**20
    assert uploads_dir.new() == set()


def test_client_disconnect_mid_upload_removes_partial_files(client, uploads_dir):
    h, ws = _authorized(client)
    total, chunks = multipart_body(20 * 2**20)
    res = run(drive(_csv_path(ws), chunks, headers=h, content_length=total, disconnect_after=40))
    assert res.status == 400, res.body
    assert res.json()["error"]["code"] == "upload_incomplete"
    assert res.pulled < 3 * 2**20
    assert uploads_dir.new() == set()
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Job

    with SessionLocal() as db:
        assert db.query(Job).count() == 0


def test_concurrent_authorized_uploads_each_get_their_own_staging(client, uploads_dir):
    h, ws = _authorized(client)
    results: list[Result] = []

    async def many() -> None:
        async def one(i: int) -> None:
            total, chunks = multipart_body(2**20, {**FIELDS, "slug": f"c-{i}"})
            results.append(await drive(_csv_path(ws), chunks, headers=h, content_length=total))

        async with anyio.create_task_group() as tg:
            for i in range(4):
                tg.start_soon(one, i)

    anyio.run(many)
    assert sorted(r.status for r in results) == [202] * 4, [r.body for r in results]
    assert len(uploads_dir.new()) == 4


def test_openapi_still_documents_the_multipart_body(client):
    spec = client.get("/api/v1/openapi.json").json()
    op = spec["paths"]["/api/v1/workspaces/{workspace_id}/datasets/csv"]["post"]
    schema = op["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert set(schema["required"]) == {"slug", "name", "source", "license", "bars"}
    assert schema["properties"]["bars"]["format"] == "binary"


# Bytes a careless boundary search would mangle: CR/LF runs and boundary prefixes.
TRICKY = (
    b"symbol,date,open,high,low,close,volume\r\n\r\r\n\n"
    + f"\r\n--{BOUNDARY[:-1]}X\r\n--{BOUNDARY}-x\r\n-{BOUNDARY}\r".encode()
    + bytes(range(256))
    + b"\r\n--\r\n"
)


@pytest.mark.parametrize("split", [1, 7, 4096, 65536])
def test_stored_file_matches_upload_byte_for_byte(client, uploads_dir, split: int) -> None:
    h, ws = _authorized(client)
    fields = b"".join(_field(k, v) for k, v in FIELDS.items())
    content = TRICKY * 50
    body = (
        fields
        + _bars_part(content)
        + _bars_part(TRICKY, name="actions")
        + (f"--{BOUNDARY}--\r\n".encode())
    )
    chunks = iter([body[i : i + split] for i in range(0, len(body), split)])
    res = run(drive(_csv_path(ws), chunks, headers=h, content_length=len(body)))
    assert res.status == 202, res.body
    (staged,) = uploads_dir.new()
    assert (staged / "bars.csv").read_bytes() == content
    assert (staged / "actions.csv").read_bytes() == TRICKY


def test_part_header_cap_applies_below_the_library_line_limit(client, uploads_dir):
    h, ws = _authorized(client)
    pad = "".join(f"X-Pad-{i}: {'a' * 3000}\r\n" for i in range(4))  # 4 x 3 KB lines
    part = (
        f'--{BOUNDARY}\r\n{pad}Content-Disposition: form-data; name="slug"\r\n\r\nx\r\n'
    ).encode()
    total, chunks = _parts(part, _bars_part())
    res = run(drive(_csv_path(ws), chunks, headers=h, content_length=total))
    assert res.status == 400, res.body
    assert "headers are too large" in res.json()["error"]["message"]


def test_upload_holds_no_database_connection_while_streaming(client, uploads_dir):
    """A trickling upload must not pin a pooled connection (pool exhaustion DoS)."""
    from quantlab_api.db import SessionLocal

    h, ws = _authorized(client)
    # The pool request sessions use (get_engine() and get_engine(None) cache separately).
    with SessionLocal() as probe:
        pool = probe.get_bind().pool
    baseline = pool.checkedout()  # type: ignore[attr-defined]
    during: list[int] = []

    def sample(call: int) -> None:
        if call in (3, 10):
            during.append(pool.checkedout())  # type: ignore[attr-defined]

    total, chunks = multipart_body(2 * 2**20)
    res = run(drive(_csv_path(ws), chunks, headers=h, content_length=total, on_pull=sample))
    assert res.status == 202, res.body
    assert during == [baseline, baseline]


def test_membership_revoked_mid_upload_is_rejected(client, uploads_dir):
    from sqlalchemy import delete

    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Job, Role, WorkspaceMember

    owner = register(client, "owner2@example.com")
    ws = workspace_id(client, owner)
    editor = register(client, "editor@example.com")
    _add_member(ws, "editor@example.com", "editor")

    def revoke(call: int) -> None:
        if call == 2:
            with SessionLocal() as db:
                db.execute(
                    delete(WorkspaceMember).where(
                        WorkspaceMember.workspace_id == uuid.UUID(ws),
                        WorkspaceMember.role == Role.editor,
                    )
                )
                db.commit()

    total, chunks = multipart_body(2**20)
    auth = {"authorization": editor["Authorization"]}
    res = run(drive(_csv_path(ws), chunks, headers=auth, content_length=total, on_pull=revoke))
    assert res.status == 404, res.body
    assert uploads_dir.new() == set()
    with SessionLocal() as db:
        assert db.query(Job).count() == 0


def test_epilogue_after_the_closing_boundary_is_not_read(client, uploads_dir):
    h, ws = _authorized(client)
    total, chunks = multipart_body(2**20)

    def with_epilogue() -> Iterator[bytes]:
        yield from chunks
        for _ in range(800):  # 50 MB of trailing junk
            yield b"z" * CHUNK

    res = run(drive(_csv_path(ws), with_epilogue(), headers=h, content_length=None))
    assert res.status == 202, res.body
    assert res.pulled < total + 2 * CHUNK


@pytest.mark.parametrize(
    ("case", "status", "present"),
    [
        ("empty_text_for_file", 202, ["bars.csv"]),
        ("unnamed_file_with_content", 202, ["actions.csv", "bars.csv"]),
        ("mixed_case_form_data", 202, ["bars.csv"]),
    ],
)
def test_browser_and_client_form_variants(client, uploads_dir, case, status, present):
    """Compatibility with what FastAPI's form parsing accepted."""
    h, ws = _authorized(client)
    fields = b"".join(_field(k, v) for k, v in FIELDS.items())
    extra = {
        # requests' data={"actions": ""}: FastAPI treated an empty value as absent.
        "empty_text_for_file": _field("actions", ""),
        "unnamed_file_with_content": _bars_part(TRICKY, name="actions", filename=""),
        "mixed_case_form_data": b"",
    }[case]
    bars = _bars_part()
    if case == "mixed_case_form_data":
        bars = bars.replace(b"form-data", b"Form-Data")
    total, chunks = _parts(fields, bars, extra) if extra else _parts(fields, bars)
    res = run(drive(_csv_path(ws), chunks, headers=h, content_length=total))
    assert res.status == status, res.body
    (staged,) = uploads_dir.new()
    assert sorted(p.name for p in staged.iterdir()) == present


def test_validation_errors_are_reported_together(client, uploads_dir):
    h, ws = _authorized(client)
    total, chunks = _parts(_field("slug", "BAD"), _field("name", "n"))
    res = run(drive(_csv_path(ws), chunks, headers=h, content_length=total))
    assert res.status == 422
    locs = {tuple(d["loc"]) for d in res.json()["error"]["details"]}
    assert {("body", "slug"), ("body", "source"), ("body", "license"), ("body", "bars")} <= locs
    assert uploads_dir.new() == set()
