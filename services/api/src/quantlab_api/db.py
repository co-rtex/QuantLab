"""Database engine, session factory and declarative base."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from quantlab_api.config import get_settings

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _instrument(engine: Engine) -> None:
    from quantlab_api.observability import DB_QUERY_SECONDS

    @event.listens_for(engine, "before_cursor_execute")
    def _start(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        conn.info.setdefault("qstart", []).append(time.perf_counter())

    @event.listens_for(engine, "after_cursor_execute")
    def _end(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        start = conn.info["qstart"].pop()
        verb = statement.lstrip().split(" ", 1)[0].upper()
        DB_QUERY_SECONDS.labels(
            verb if verb in {"SELECT", "INSERT", "UPDATE", "DELETE"} else "OTHER"
        ).observe(time.perf_counter() - start)


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    engine = create_engine(
        url or get_settings().database_url, pool_pre_ping=True, pool_size=10, max_overflow=20
    )
    _instrument(engine)
    return engine


@lru_cache
def _factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)


def SessionLocal() -> Session:  # noqa: N802 - factory-style name
    return _factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
