"""Permanent identity through the API jobs (#9 part C) and stored reconciliation reports
served read-only (#12). Synthetic data only."""

from __future__ import annotations

import uuid

import pandas as pd

from quantlab.data.synthetic import SyntheticSpec, generate_market
from tests.integration.conftest import join_workspace, register, run_pending_jobs, workspace_id

SWITCH = pd.Timestamp("2021-07-01")


def _add_version(dv_id: str, bars, actions, history=None, master=None) -> str:
    """Register another version in the seeded dataset (as a dataset job would)."""
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Dataset, DatasetVersion
    from quantlab_api.tasks import gate_quality, register_version

    with SessionLocal() as db:
        seed = db.get(DatasetVersion, uuid.UUID(dv_id))
        ds = db.get(Dataset, seed.dataset_id)
        row = register_version(
            db,
            ds,
            bars,
            actions,
            {"provider": "synthetic", "is_synthetic": True, "note": "test fixture"},
            None,
            quality=gate_quality(bars, actions),
            symbol_history=history,
            master=master,
        )
        db.commit()
        return str(row.id)


def _experiment(client, seeded, dv: str, config: dict, strategy: str = "equal_weight") -> dict:
    r = client.post(
        f"/api/v1/workspaces/{seeded['ws']}/experiments",
        headers=seeded["headers"],
        json={
            "name": "identity",
            "dataset_version_id": dv,
            "strategy_key": strategy,
            "strategy_params": {},
            "config": {"commission_bps": 0, "slippage_bps": 0, **config},
        },
    )
    assert r.status_code == 202, r.text
    return r.json()


def _trades(client, seeded, exp_id: str) -> list[dict]:
    r = client.get(f"/api/v1/experiments/{exp_id}/trades?limit=500", headers=seeded["headers"])
    return r.json()["items"]


# --------------------------------------------------------------- identity in jobs
def test_synthetic_versions_carry_ids_and_results_show_tickers(client, seeded):
    h = seeded["headers"]
    inst = client.get(f"/api/v1/dataset-versions/{seeded['dv']}/instruments", headers=h).json()
    assert all(i["instrument_id"].startswith("QLSYN") for i in inst["items"])
    exp = _experiment(client, seeded, seeded["dv"], {"start": "2021-01-04", "benchmark": "SYNIDX"})
    assert run_pending_jobs() == ["completed"]
    eid = exp["experiment"]["id"]
    detail = client.get(f"/api/v1/experiments/{eid}", headers=h).json()
    assert detail["engine_version"] == "1.3.0"
    trades = _trades(client, seeded, eid)
    assert trades and all(t["symbol"].startswith("SYN") for t in trades)
    assert all(t["instrument_id"] == "QL" + t["symbol"] for t in trades)  # synthetic ids
    analysis = client.get(f"/api/v1/experiments/{eid}/analysis", headers=h).json()
    assert all(k.startswith("SYN") for k in analysis["final_holdings"])
    assert all(row["instrument_id"].startswith("QLSYN") for row in analysis["contributions"]["top"])


def test_a_version_without_identity_still_verifies_and_reproduces(client, seeded):
    """Versions written before #9 have no symbol history; they behave exactly as before."""
    from quantlab.data.store import DatasetStore
    from quantlab_api.config import get_settings
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Dataset, DatasetVersion

    m = generate_market(SyntheticSpec(n_assets=8, start="2020-01-02", end="2022-06-30", seed=3))
    dv = _add_version(seeded["dv"], m.bars, m.actions)  # no history, no master
    h = seeded["headers"]
    inst = client.get(f"/api/v1/dataset-versions/{dv}/instruments", headers=h).json()
    assert all(i["instrument_id"] is None for i in inst["items"])
    exp = _experiment(client, seeded, dv, {"start": "2021-01-04", "benchmark": "SYNIDX"})
    assert run_pending_jobs() == ["completed"]
    eid = exp["experiment"]["id"]
    trades = _trades(client, seeded, eid)
    assert trades and all(t["instrument_id"] == t["symbol"] for t in trades)
    rep = client.post(f"/api/v1/experiments/{eid}/reproduce", headers=h)
    assert rep.status_code == 202, rep.text
    assert run_pending_jobs() == ["completed"]
    child = rep.json()["experiment"]["id"]
    check = client.get(f"/api/v1/experiments/{child}/reproducibility", headers=h).json()
    assert check["comparisons"][0]["fingerprint_match"] is True
    with SessionLocal() as db:
        row = db.get(DatasetVersion, uuid.UUID(dv))
        ds = db.get(Dataset, row.dataset_id)
        store = DatasetStore(get_settings().datasets_dir)
        version = store.open(ds.slug, row.version_hash)
    assert store.verify(version)
    assert store.load_master(version) is None


def _renamed_and_reused():
    """SYN003 is renamed NEW03 on SWITCH. On SWITCH the original SYN004 becomes SYN004B and
    an unrelated company (QLNEWCO) starts trading under the ticker SYN004."""
    m = generate_market(
        SyntheticSpec(
            n_assets=6,
            start="2020-01-02",
            end="2022-12-30",
            seed=9,
            delisting_fraction=0.0,
            late_listing_fraction=0.0,
        )
    )
    bars, acts = m.bars.copy(), m.actions.copy()
    for old, new in (("SYN003", "NEW03"), ("SYN004", "SYN004B")):
        bars.loc[(bars["symbol"] == old) & (bars["date"] >= SWITCH), "symbol"] = new
        acts.loc[(acts["symbol"] == old) & (acts["ex_date"] >= SWITCH), "symbol"] = new
    newco = m.bars[(m.bars["symbol"] == "SYN005") & (m.bars["date"] >= SWITCH)].copy()
    newco["symbol"] = "SYN004"
    for c in ("open", "high", "low", "close"):
        newco[c] *= 2
    late = m.bars[(m.bars["symbol"] == "SYN006") & (m.bars["date"] >= SWITCH)].copy()
    late["symbol"] = "LATE1"  # a new listing on SWITCH
    for c in ("open", "high", "low", "close"):
        late[c] *= 3
    bars = pd.concat([bars, newco, late], ignore_index=True)
    h = m.symbol_history
    rows = h[~h["instrument_id"].isin(["QLSYN003", "QLSYN004"])].to_dict("records")
    for iid, old, new in (("QLSYN003", "SYN003", "NEW03"), ("QLSYN004", "SYN004", "SYN004B")):
        start = h.loc[h["instrument_id"] == iid, "valid_from"].iloc[0]
        rows += [
            {"instrument_id": iid, "symbol": old, "valid_from": start, "valid_to": SWITCH},
            {"instrument_id": iid, "symbol": new, "valid_from": SWITCH, "valid_to": pd.NaT},
        ]
    rows.append(
        {"instrument_id": "QLNEWCO", "symbol": "SYN004", "valid_from": SWITCH, "valid_to": pd.NaT}
    )
    rows.append(
        {"instrument_id": "QLLATE", "symbol": "LATE1", "valid_from": SWITCH, "valid_to": pd.NaT}
    )
    return bars, acts, pd.DataFrame(rows)


def test_universe_tickers_resolve_as_of_the_backtest_start(client, seeded):
    bars, acts, history = _renamed_and_reused()
    dv = _add_version(seeded["dv"], bars, acts, history=history)
    h = seeded["headers"]
    # Before the switch: the renamed security stays in (continuously, as NEW03 later), and
    # the company that later reuses SYN004 is not picked up.
    early = _experiment(
        client,
        seeded,
        dv,
        {"start": "2020-06-01", "universe": ["SYN001", "SYN003", "SYN004"], "rebalance": "monthly"},
    )
    # After the switch the same ticker SYN004 names the new company.
    late = _experiment(
        client, seeded, dv, {"start": "2022-01-03", "universe": ["SYN004"], "rebalance": "monthly"}
    )
    # A ticker that names nothing anywhere in the window is rejected, not guessed: SYN003
    # was retired at the rename, before this start.
    wrong = _experiment(
        client, seeded, dv, {"start": "2022-01-03", "universe": ["SYN003"], "rebalance": "monthly"}
    )
    # A later listing joins when it lists (as in an unkeyed dataset); a start before the
    # data (a holiday here) means the first session, benchmark included.
    joined = _experiment(
        client,
        seeded,
        dv,
        {
            "start": "2020-01-01",
            "universe": ["SYN001", "LATE1"],
            "benchmark": "SYNIDX",
            "rebalance": "monthly",
        },
    )
    assert sorted(run_pending_jobs()) == ["completed", "completed", "completed", "failed"]
    t_joined = _trades(client, seeded, joined["experiment"]["id"])
    late_buys = [t for t in t_joined if t["instrument_id"] == "QLLATE"]
    assert late_buys and min(t["date"] for t in late_buys) > "2021-07-01"
    analysis = client.get(
        f"/api/v1/experiments/{joined['experiment']['id']}/analysis", headers=h
    ).json()
    assert analysis["meta"]["universe"] == ["LATE1", "SYN001"]  # as the user named them
    assert analysis["meta"]["universe_instrument_ids"] == ["QLLATE", "QLSYN001"]
    assert analysis["meta"]["benchmark"] == "SYNIDX"
    t_early = _trades(client, seeded, early["experiment"]["id"])
    assert {t["instrument_id"] for t in t_early} == {"QLSYN001", "QLSYN003", "QLSYN004"}
    assert not [t for t in t_early if (t.get("note") or "").startswith("delisting")]
    shown = {t["symbol"] for t in t_early if t["date"] >= "2021-07-01"}
    assert {"NEW03", "SYN004B"} <= shown  # tickers valid on each trade date
    pos = client.get(
        f"/api/v1/experiments/{early['experiment']['id']}/positions?on=2022-06-15", headers=h
    ).json()
    assert {(p["symbol"], p["instrument_id"]) for p in pos["items"]} >= {
        ("NEW03", "QLSYN003"),
        ("SYN004B", "QLSYN004"),
    }
    t_late = _trades(client, seeded, late["experiment"]["id"])
    assert {t["instrument_id"] for t in t_late} == {"QLNEWCO"}
    job = client.get(f"/api/v1/experiments/{wrong['experiment']['id']}/job", headers=h).json()
    assert "not listed between 2022-01-03" in job["error"]["message"]


# ------------------------------------------------- reconciliation reports (read-only)
def _store_report(dv_id: str, name: str) -> None:
    from quantlab.data.reconcile import Source, reconcile
    from quantlab.data.store import DatasetStore
    from quantlab_api.config import get_settings
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Dataset, DatasetVersion

    with SessionLocal() as db:
        row = db.get(DatasetVersion, uuid.UUID(dv_id))
        ds = db.get(Dataset, row.dataset_id)
    store = DatasetStore(get_settings().datasets_dir)
    dv = store.open(ds.slug, row.version_hash)
    bars, acts = store.load_bars(dv), store.load_actions(dv)
    report = reconcile(
        Source("primary", bars, acts, store.load_symbol_history(dv), True, version=dv.version),
        Source("csv copy", bars, acts, None, True),
    )
    store.write_report(dv, name, report.to_dict())


def test_reconciliation_reports_are_read_only_and_workspace_scoped(client, seeded):
    owner, ws, dv = seeded["headers"], seeded["ws"], seeded["dv"]
    _store_report(dv, "reconciliation-csv")
    _store_report(dv, "other-report")  # not a reconciliation: never served here
    base = f"/api/v1/workspaces/{ws}/dataset-versions/{dv}/reconciliations"

    listed = client.get(base, headers=owner)
    assert listed.status_code == 200, listed.text
    assert [r["name"] for r in listed.json()] == ["reconciliation-csv"]
    assert listed.json()[0]["status"] == "pass"
    full = client.get(f"{base}/reconciliation-csv", headers=owner).json()
    assert full["inputs"]["secondary"]["name"] == "csv copy" and "samples" in full

    viewer = register(client, "viewer@example.com")
    join_workspace(client, owner, ws, viewer, "viewer@example.com", role="viewer")
    assert client.get(base, headers=viewer).status_code == 200
    assert client.get(f"{base}/reconciliation-csv", headers=viewer).status_code == 200

    outsider = register(client, "outsider@example.com")
    other_ws = workspace_id(client, outsider)
    for url in (base, f"{base}/reconciliation-csv"):
        assert client.get(url, headers=outsider).status_code == 404
    # Cross-workspace: the outsider's own workspace in the path, someone else's version.
    cross = f"/api/v1/workspaces/{other_ws}/dataset-versions/{dv}/reconciliations"
    assert client.get(cross, headers=outsider).status_code == 404
    assert client.get(f"{cross}/reconciliation-csv", headers=outsider).status_code == 404

    for name in ("other-report", "reconciliation-missing", "BAD NAME"):
        assert client.get(f"{base}/{name}", headers=owner).status_code == 404
    for method in (client.post, client.put, client.delete):
        assert method(f"{base}/reconciliation-csv", headers=owner).status_code == 405


# ------------------------------------------------------- review of #62 regressions
def test_integrity_report_sees_delisted_names_in_a_keyed_universe(client, seeded):
    """The stored universe is labelled with tickers again, so the survivorship check sees
    the delisted name it includes (it reported a false warning on every keyed run)."""
    h = seeded["headers"]
    inst = client.get(f"/api/v1/dataset-versions/{seeded['dv']}/instruments", headers=h).json()
    gone = sorted(i["symbol"] for i in inst["items"] if i["delisted"])
    assert gone, "the seeded synthetic market has a delisting"
    exp = _experiment(
        client,
        seeded,
        seeded["dv"],
        {"start": "2020-03-02", "universe": ["SYN001", gone[0]], "benchmark": "SYNIDX"},
    )
    assert run_pending_jobs() == ["completed"]
    report = client.get(f"/api/v1/experiments/{exp['experiment']['id']}/integrity", headers=h)
    check = next(c for c in report.json()["checks"] if c["key"] == "universe")
    assert check["status"] != "warning", check


def test_catalog_marks_instruments_not_tickers_as_delisted(client, seeded):
    bars, acts, history = _renamed_and_reused()
    dv = _add_version(seeded["dv"], bars, acts, history=history)
    inst = client.get(
        f"/api/v1/dataset-versions/{dv}/instruments?limit=50", headers=seeded["headers"]
    ).json()
    by = {i["symbol"]: i for i in inst["items"]}
    # SYN003's last bar is the day before the rename, but the security still trades.
    assert by["SYN003"]["instrument_id"] == "QLSYN003" and by["SYN003"]["delisted"] is False
    assert by["NEW03"]["instrument_id"] == "QLSYN003"


def test_a_malformed_stored_report_does_not_hide_the_valid_ones(client, seeded):
    from quantlab.data.store import DatasetStore
    from quantlab_api.config import get_settings
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Dataset, DatasetVersion

    owner, ws, dv = seeded["headers"], seeded["ws"], seeded["dv"]
    _store_report(dv, "reconciliation-good")
    with SessionLocal() as db:
        row = db.get(DatasetVersion, uuid.UUID(dv))
        ds = db.get(Dataset, row.dataset_id)
    store = DatasetStore(get_settings().datasets_dir)
    store.write_report(store.open(ds.slug, row.version_hash), "reconciliation-bad", {"status": "x"})
    base = f"/api/v1/workspaces/{ws}/dataset-versions/{dv}/reconciliations"
    listed = client.get(base, headers=owner)
    assert listed.status_code == 200
    names = [r["name"] for r in listed.json()]
    assert "reconciliation-good" in names and "reconciliation-bad" not in names
    bad = client.get(f"{base}/reconciliation-bad", headers=owner)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_report"


def _ml_predictions(client, seeded, config: dict) -> pd.DataFrame:
    from quantlab_api import artifacts

    h, ws = seeded["headers"], seeded["ws"]
    r = client.post(
        f"/api/v1/workspaces/{ws}/ml-runs",
        headers=h,
        json={
            "name": "exclude check",
            "dataset_version_id": seeded["dv"],
            "config": {
                "folds": 2,
                "min_train_dates": 6,
                "sample_every": 10,
                "models": ["majority"],
                **config,
            },
        },
    )
    assert r.status_code == 202, r.text
    assert run_pending_jobs() == ["completed"]
    return pd.read_parquet(artifacts.ml_dir(r.json()["id"]) / "predictions.parquet")


def test_ml_excludes_the_benchmark_and_user_tickers_on_a_keyed_dataset(client, seeded):
    # The default exclude names the index ticker; the keyed series is QLSYNIDX.
    default = _ml_predictions(client, seeded, {})
    assert len(default) and "QLSYNIDX" not in set(default["instrument_id"])
    assert "SYNIDX" not in set(default["symbol"])
    assert set(default["instrument_id"]) >= {"QLSYN002"}  # ordinary assets are predicted
    # A user exclude by ticker works too.
    custom = _ml_predictions(client, seeded, {"exclude": ["SYNIDX", "syn001"]})
    assert not {"QLSYN001", "QLSYNIDX"} & set(custom["instrument_id"])
    assert "SYN001" not in set(custom["symbol"])


# ------------------------------------------------------------------------------- #63
def test_reproducing_an_older_engines_run_names_the_engine_change(client, seeded):
    from quantlab.backtest.engine import ENGINE_VERSION
    from quantlab_api.db import SessionLocal
    from quantlab_api.models import Experiment

    h = seeded["headers"]
    exp = _experiment(client, seeded, seeded["dv"], {"start": "2020-03-02"})
    assert run_pending_jobs() == ["completed"]
    exp_id = exp["experiment"]["id"]
    with SessionLocal() as db:  # as if it had run on an older engine
        row = db.get(Experiment, uuid.UUID(exp_id))
        row.engine_version = "1.2.0"
        row.input_hash = "0" * 64
        db.commit()
    child = client.post(f"/api/v1/experiments/{exp_id}/reproduce", headers=h).json()
    assert run_pending_jobs() == ["completed"]
    child_id = child["experiment"]["id"]
    for eid in (child_id, exp_id):  # both directions name it the same way
        comp = client.get(f"/api/v1/experiments/{eid}/reproducibility", headers=h).json()
        (c,) = comp["comparisons"]
        assert c["same_inputs"] is False
        assert c["input_differences"] == [
            {"input": "engine_version", "original": "1.2.0", "reproduction": ENGINE_VERSION}
        ]
    report = client.get(f"/api/v1/experiments/{child_id}/integrity", headers=h).json()
    check = next(c for c in report["checks"] if c["key"] == "reproducibility")
    assert check["status"] == "unassessed", check  # not "failed"
    assert f"engine_version 1.2.0 → {ENGINE_VERSION}" in check["finding"]


def test_trades_filter_by_instrument_across_a_rename(client, seeded):
    bars, acts, history = _renamed_and_reused()
    dv = _add_version(seeded["dv"], bars, acts, history=history)
    exp = _experiment(
        client,
        seeded,
        dv,
        {"start": "2020-03-02", "universe": ["SYN001", "SYN003"], "rebalance": "monthly"},
    )
    assert run_pending_jobs() == ["completed"]
    url = f"/api/v1/experiments/{exp['experiment']['id']}/trades?limit=500"
    h = seeded["headers"]
    by_id = client.get(f"{url}&instrument_id=qlsyn003", headers=h).json()
    assert by_id["total"] > 0
    assert {t["instrument_id"] for t in by_id["items"]} == {"QLSYN003"}
    assert {t["symbol"] for t in by_id["items"]} == {"SYN003", "NEW03"}  # both tickers
    by_ticker = client.get(f"{url}&symbol=NEW03", headers=h).json()
    assert 0 < by_ticker["total"] < by_id["total"]
    none = client.get(f"{url}&instrument_id=QLNOPE", headers=h).json()
    assert none["total"] == 0
