"""Migrations must round-trip and match the ORM models exactly."""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from tests.integration.conftest import BASE_URL


def _cfg() -> Config:
    cfg = Config("services/api/alembic.ini")
    cfg.set_main_option("sqlalchemy.url", BASE_URL)
    return cfg


def test_downgrade_upgrade_round_trip_and_no_drift():
    cfg = _cfg()
    command.downgrade(cfg, "base")
    eng = create_engine(BASE_URL)
    assert set(inspect(eng).get_table_names()) <= {"alembic_version"}
    command.upgrade(cfg, "head")
    assert {"jobs", "experiments", "experiment_results"} <= set(inspect(eng).get_table_names())
    command.check(cfg)  # raises if autogenerate would emit operations
    eng.dispose()


def test_single_head():
    assert len(ScriptDirectory.from_config(_cfg()).get_heads()) == 1
