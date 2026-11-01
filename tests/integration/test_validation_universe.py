"""Validation with an explicit universe whose member lists after a leakage cut (#237).

Synthetic data only. The independent RC assessment found the validation suite failing with
"no data for symbols: ['QLSYN002']" when a universe member was first listed after a
sampled cut: the leakage check's truncated run kept the full explicit universe.
"""

from __future__ import annotations

from quantlab.data.synthetic import SyntheticSpec, generate_market
from tests.integration.conftest import run_pending_jobs
from tests.integration.test_identity_jobs import _add_version


def _late_listing_version(seeded) -> tuple[str, list[str]]:
    # SYN004 and SYN005 are first listed after the 2020-01-02 start (SYN005 on 2020-03-06).
    m = generate_market(
        SyntheticSpec(
            n_assets=8,
            start="2019-01-02",
            end="2021-12-31",
            seed=1,
            late_listing_fraction=0.5,
            split_probability=0.01,
        )
    )
    dv = _add_version(seeded["dv"], m.bars, m.actions, m.symbol_history, m.master, m.instruments)
    tickers = sorted(s for s in m.bars["symbol"].unique() if s != "SYNIDX")
    return dv, tickers


def _validate(client, seeded, dv: str, tickers: list[str]) -> dict:
    h = seeded["headers"]
    r = client.post(
        f"/api/v1/workspaces/{seeded['ws']}/experiments",
        headers=h,
        json={
            "name": "late listing",
            "dataset_version_id": dv,
            "strategy_key": "equal_weight",
            "strategy_params": {},
            # A short window so the suite's sampled cuts (4, seed 0) include one before
            # SYN005's listing: on the old check this run failed with "no data for symbols".
            "config": {
                "start": "2020-01-02",
                "end": "2020-06-30",
                "rebalance": "weekly",
                "universe": tickers,
            },
        },
    )
    assert r.status_code == 202, r.text
    assert run_pending_jobs() == ["completed"]
    exp_id = r.json()["experiment"]["id"]
    assert client.post(f"/api/v1/experiments/{exp_id}/validation", headers=h).status_code == 202
    run_pending_jobs()
    return client.get(f"/api/v1/experiments/{exp_id}/validation", headers=h).json()


def test_validation_completes_with_a_member_listed_after_a_cut(client, seeded):
    dv, tickers = _late_listing_version(seeded)
    val = _validate(client, seeded, dv, tickers)
    assert val["status"] == "completed", val
    leak = val["result"]["leakage"]
    assert leak["status"] == "verified", leak


def test_a_suite_error_names_tickers_not_instrument_ids(client, seeded, monkeypatch):
    from quantlab.backtest.engine import BacktestError
    from quantlab.research import suite

    def failing(*args, **kwargs):
        raise BacktestError("no data for symbols: ['QLSYN001', 'QLSYN005', 'QLGONE']")

    monkeypatch.setattr(suite, "run_validation_suite", failing)
    dv, tickers = _late_listing_version(seeded)
    val = _validate(client, seeded, dv, tickers)
    assert val["status"] == "failed", val
    message = val["job"]["error"]["message"]
    # QLSYN001 is listed at the start (its ticker then); QLSYN005 lists after it (its first
    # ticker); an id with no ticker in the history is kept, not dropped.
    expected = "validation could not run: no data for symbols: ['SYN001', 'SYN005', 'QLGONE']"
    assert expected in message


def test_a_naming_failure_keeps_the_original_error(client, seeded, monkeypatch):
    from quantlab.backtest.engine import BacktestError
    from quantlab.research import suite
    from quantlab_api import tasks

    def failing(*args, **kwargs):
        raise BacktestError("no data for symbols: ['QLSYN005']")

    def broken(*args, **kwargs):
        raise KeyError("history without valid_from")

    monkeypatch.setattr(suite, "run_validation_suite", failing)
    monkeypatch.setattr(tasks, "name_instruments", broken)
    dv, tickers = _late_listing_version(seeded)
    val = _validate(client, seeded, dv, tickers)
    assert val["status"] == "failed", val
    message = val["job"]["error"]["message"]
    assert "validation could not run: no data for symbols: ['QLSYN005']" in message
