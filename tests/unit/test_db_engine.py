"""One engine (one connection pool) per database URL per process."""

from __future__ import annotations


def test_default_and_explicit_none_share_one_engine_and_pool():
    from quantlab_api.config import get_settings
    from quantlab_api.db import SessionLocal, get_engine

    engine = get_engine()
    assert get_engine(None) is engine
    assert get_engine(get_settings().database_url) is engine
    with SessionLocal() as s:
        assert s.get_bind() is engine
