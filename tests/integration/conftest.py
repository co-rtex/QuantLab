"""Integration fixtures: a real PostgreSQL database (docker compose) and fakeredis."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


def _default_test_database() -> str:
    """One test database per checkout, so git worktrees sharing a PostgreSQL server do not
    TRUNCATE each other's tables mid-test. The main checkout keeps ``quantlab_test``; a
    linked worktree (whose ``.git`` is a file) gets a suffix derived from its path."""
    root = Path(__file__).resolve().parents[2]
    name = "quantlab_test"
    if (root / ".git").is_file():
        name += "_" + hashlib.sha256(str(root).encode()).hexdigest()[:10]
    port = os.environ.get("QUANTLAB_PG_PORT", "5433")
    return f"postgresql+psycopg://quantlab:quantlab@localhost:{port}/{name}"


BASE_URL = os.environ.get("QUANTLAB_TEST_DATABASE_URL") or _default_test_database()
# Arbitrary constant: sessions using the same database take this lock and run one at a time.
SESSION_LOCK_KEY = 0x51AB_7E57
os.environ["QUANTLAB_DATABASE_URL"] = BASE_URL
os.environ["QUANTLAB_ENVIRONMENT"] = "test"
os.environ.setdefault("QUANTLAB_DATA_DIR", tempfile.mkdtemp(prefix="quantlab-it-"))
os.environ["QUANTLAB_RATE_LIMIT_LOGIN_PER_MINUTE"] = "1000"
os.environ["QUANTLAB_JOB_LEASE_SECONDS"] = "30"

import fakeredis  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

TABLES = [
    "audit_events",
    "experiment_results",
    "research_runs",
    "ml_runs",
    "jobs",
    "experiments",
    "portfolio_transactions",
    "portfolios",
    "strategies",
    "instruments",
    "dataset_versions",
    "datasets",
    "workspace_members",
    "workspaces",
    "users",
]


def _ensure_database() -> None:
    url = make_url(BASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            exists = c.scalar(
                text("SELECT 1 FROM pg_database WHERE datname=:n"), {"n": url.database}
            )
            if not exists:
                c.execute(text(f'CREATE DATABASE "{url.database}"'))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    finally:
        admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    _ensure_database()
    from alembic import command
    from alembic.config import Config

    # Every test TRUNCATEs all tables, so two sessions on one database (two terminals, or
    # QUANTLAB_TEST_DATABASE_URL pointed at the same place) would corrupt each other.
    # A session-level advisory lock serializes them instead.
    locker = create_engine(BASE_URL, poolclass=NullPool)
    lock_conn = locker.connect()
    lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": SESSION_LOCK_KEY})
    lock_conn.commit()
    try:
        cfg = Config("services/api/alembic.ini")
        cfg.set_main_option("sqlalchemy.url", BASE_URL)
        command.upgrade(cfg, "head")
        yield
    finally:
        lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SESSION_LOCK_KEY})
        lock_conn.close()
        locker.dispose()


@pytest.fixture(autouse=True)
def clean(database: None) -> Iterator[None]:
    from quantlab_api.db import get_engine
    from quantlab_api.jobs import set_redis
    from quantlab_api.security import login_limiter

    with get_engine().begin() as c:
        c.execute(text("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"))
    set_redis(fakeredis.FakeRedis())
    login_limiter.reset()
    yield


@pytest.fixture
def client():  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from quantlab_api.main import app

    with TestClient(app) as c:
        yield c


def register(client, email: str = "alice@example.com", password: str = "Correct-horse-9"):  # type: ignore[no-untyped-def]
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "display_name": email[:5]},
    )
    assert r.status_code == 201, r.text
    token = r.json()["access_token"]
    client.cookies.clear()
    return {"Authorization": f"Bearer {token}"}


def workspace_id(client, headers) -> str:  # type: ignore[no-untyped-def]
    r = client.get("/api/v1/workspaces", headers=headers)
    assert r.status_code == 200
    return r.json()[0]["id"]


def run_pending_jobs(max_jobs: int = 20) -> list[str]:
    """Execute queued jobs synchronously, as a worker would."""
    from sqlalchemy import select

    from quantlab_api.db import SessionLocal
    from quantlab_api.jobs import execute_job
    from quantlab_api.models import Job, JobStatus

    outcomes = []
    for _ in range(max_jobs):
        with SessionLocal() as db:
            job = db.scalar(
                select(Job).where(Job.status == JobStatus.queued).order_by(Job.created_at)
            )
        if job is None:
            break
        outcomes.append(execute_job(str(job.id), worker_id="test-worker"))
    return outcomes


@pytest.fixture
def seeded(client):  # type: ignore[no-untyped-def]
    """A user with a small synthetic dataset version ready."""
    h = register(client)
    ws = workspace_id(client, h)
    r = client.post(
        f"/api/v1/workspaces/{ws}/datasets/synthetic",
        headers=h,
        json={
            "slug": "demo",
            "name": "Demo",
            "n_assets": 12,
            "start": "2020-01-02",
            "end": "2022-12-30",
            "seed": 5,
        },
    )
    assert r.status_code == 202, r.text
    assert run_pending_jobs() == ["completed"]
    versions = client.get(f"/api/v1/workspaces/{ws}/dataset-versions", headers=h).json()
    return {"headers": h, "ws": ws, "dv": versions[0]["id"]}
