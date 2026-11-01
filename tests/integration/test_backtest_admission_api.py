"""The experiments API refuses a backtest whose estimated memory exceeds the limit (#163).

Synthetic data. The limit is set either side of this dataset's own estimate.
"""

from __future__ import annotations

import math
import uuid
from datetime import date

from tests.integration.conftest import run_pending_jobs
from tests.integration.test_api_workflow import BT, submit


def _estimate(seeded) -> float:
    """The estimate the route computes for ``BT`` on the seeded version: no universe means
    every instrument of the version, as ``create_experiment`` counts it."""
    from sqlalchemy import func, select

    from quantlab_api.db import SessionLocal
    from quantlab_api.limits import backtest_cost
    from quantlab_api.models import DatasetVersion, Instrument

    with SessionLocal() as db:
        dv = db.get(DatasetVersion, uuid.UUID(seeded["dv"]))
        n = db.scalar(
            select(func.count())
            .select_from(Instrument)
            .where(Instrument.dataset_version_id == dv.id)
        )
        start = date.fromisoformat(BT["start"])
        return backtest_cost(
            int(n), dv.start_date, dv.end_date, start, None, BT["rebalance"]
        ).memory_mb


def test_a_backtest_over_the_memory_limit_is_refused_with_its_estimate(client, seeded, monkeypatch):
    from quantlab_api.config import get_settings

    est = _estimate(seeded)
    monkeypatch.setattr(get_settings(), "max_backtest_memory_mb", math.floor(est) - 1)
    r = submit(client, seeded)
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "backtest_too_large"
    assert err["details"]["estimated_mb"] == round(est, 1)
    assert err["details"]["limit_mb"] == math.floor(est) - 1
    assert f"{est:.0f} MB" in err["message"]
    assert run_pending_jobs() == []  # nothing was queued


def test_a_backtest_within_the_limit_is_accepted_and_runs(client, seeded, monkeypatch):
    from quantlab_api.config import get_settings

    monkeypatch.setattr(get_settings(), "max_backtest_memory_mb", math.ceil(_estimate(seeded)))
    assert submit(client, seeded).status_code == 202
    assert run_pending_jobs() == ["completed"]


def test_missing_version_files_are_a_clean_409_not_a_500(client, seeded, monkeypatch):
    # #174 re-review: the size check opens the version's files at submission.
    from quantlab_api.routes import experiments

    class Gone:
        def open(self, slug, version):
            raise FileNotFoundError(f"dataset {slug}@{version[:12]} not found")

    monkeypatch.setattr(experiments, "dataset_store", lambda: Gone())
    r = submit(client, seeded)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "dataset_unavailable"
    assert run_pending_jobs() == []


def test_the_default_limit_admits_the_seeded_backtest(client, seeded):
    assert submit(client, seeded).status_code == 202
