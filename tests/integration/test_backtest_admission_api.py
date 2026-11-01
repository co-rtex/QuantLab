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
    shown = math.ceil(est * 10) / 10  # rounded up, never shown at the limit (#181)
    assert err["details"]["estimated_mb"] == shown
    assert err["details"]["limit_mb"] == math.floor(est) - 1
    assert f"{shown:.1f} MB" in err["message"]
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


def _refused_width(client, seeded, dv: str, universe: list[str] | None, monkeypatch) -> int:
    """The width the route estimates for a backtest on ``dv``: refused under the smallest
    limit allowed, the 422 names it."""
    from quantlab_api.config import get_settings

    monkeypatch.setattr(get_settings(), "max_backtest_memory_mb", 64)
    config: dict = {"start": "2021-01-04", "rebalance": "monthly"}
    if universe is not None:
        config["universe"] = universe
    r = client.post(
        f"/api/v1/workspaces/{seeded['ws']}/experiments",
        headers=seeded["headers"],
        json={
            "name": "width",
            "dataset_version_id": dv,
            "strategy_key": "equal_weight",
            "strategy_params": {},
            "config": config,
        },
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "estimated_memory"
    assert run_pending_jobs() == []
    return r.json()["error"]["details"]["symbols"]


def test_a_reused_ticker_counts_every_instrument_the_job_runs(client, seeded, monkeypatch):
    # #181 A: SYN002 is held by QLSYN002 until it delists, then by QLNEW2. Without a
    # universe the job runs every instrument, so the estimate counts instruments, not
    # tickers: main counted the version's 5 tickers for its 6 instruments.
    from tests.integration.test_identity_jobs import _add_version, _reused_after_delisting

    bars, acts, history, master, instruments, _ = _reused_after_delisting()
    dv = _add_version(seeded["dv"], bars, acts, history, master, instruments)
    assert history["symbol"].nunique() == 5 and history["instrument_id"].nunique() == 6
    assert _refused_width(client, seeded, dv, None, monkeypatch) == 6


def test_a_universe_listing_a_ticker_twice_counts_it_once(client, seeded, monkeypatch):
    # #181 C: the engine deduplicates the universe; main's estimate counted 3 here.
    assert (
        _refused_width(client, seeded, seeded["dv"], ["SYN001", "syn001", "SYN001"], monkeypatch)
        == 1
    )
