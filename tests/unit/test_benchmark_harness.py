"""The benchmark harness's measurement rules (#24). No published numbers come from here."""

from __future__ import annotations

import pytest
from benchmarks import harness, run_all


def test_stats_report_median_iqr_and_keep_raw_samples():
    s = harness.stats([1.0, 2.0, 3.0, 4.0], in_process=True, cache="warm")
    assert s["n"] == 4
    assert s["median_ms"] == 2500.0
    assert (s["p25_ms"], s["p75_ms"], s["iqr_ms"]) == (1750.0, 3250.0, 1500.0)
    assert s["samples_ms"] == [1000.0, 2000.0, 3000.0, 4000.0]
    assert s["in_process"] is True and s["cache"] == "warm"


def test_no_samples_is_undefined_not_zero():
    s = harness.stats([], in_process=False, cache="warm")
    assert s["median_ms"] is None and s["reason"]


@pytest.mark.parametrize(
    ("load", "cpus", "expected"), [(4.01, 8, True), (4.0, 8, False), (0.2, 1, False)]
)
def test_contended_means_one_minute_load_above_half_the_cores(load, cpus, expected):
    assert harness.is_contended(load, cpus) is expected


def _load(l1: float, cpus: int = 8) -> dict:
    return {"load_1m": l1, "contended": harness.is_contended(l1, cpus)}


def _finalize(monkeypatch, start: float, end: float, settled: float, settle_s=None):
    """finalize with synthetic readings: ``end`` straight after the run, ``settled`` after
    the settle period. Returns the result and the sleeps it asked for."""
    readings = iter([_load(end), _load(settled)])
    monkeypatch.setattr(harness, "load_snapshot", lambda: next(readings))
    slept: list[float] = []
    kw = {} if settle_s is None else {"settle_s": settle_s}
    return harness.finalize({}, _load(start), sleep=slept.append, **kw), slept


def test_a_runs_own_load_at_the_end_does_not_make_it_contended(monkeypatch):
    # #160: the --skip-parallel large run started at 2.98 and ended at 4.76 on 8 cores
    # because of its own work. Once that has decayed, the background is quiet.
    res, slept = _finalize(monkeypatch, start=2.98, end=4.76, settled=1.4)
    assert res["contended"] is False and slept == [harness.SETTLE_S] == [180.0]
    load = res["load"]
    assert load["end"]["load_1m"] == 4.76 and load["end"]["contended"] is True  # kept
    assert load["settled"]["load_1m"] == 1.4 and load["run_load_estimate"] == 3.36
    assert load["residue_fraction"] == pytest.approx(0.0498, abs=1e-4)  # exp(-180/60)


@pytest.mark.parametrize(
    ("start", "end", "settled", "contended"),
    [
        (2.0, 6.0, 4.5, True),  # background still above half the cores after settling
        (4.5, 4.6, 1.0, True),  # contended before the run started
        (1.0, 2.0, 4.0, False),  # at the threshold is not above it
        (1.0, 2.0, 4.01, True),
    ],
)
def test_the_start_and_the_settled_end_decide(monkeypatch, start, end, settled, contended):
    res, _ = _finalize(monkeypatch, start, end, settled)
    assert res["contended"] is contended


def test_no_settling_is_the_old_rule_and_does_not_wait(monkeypatch):
    readings = iter([_load(4.76)])
    monkeypatch.setattr(harness, "load_snapshot", lambda: next(readings))
    slept: list[float] = []
    res = harness.finalize({}, _load(2.98), settle_s=0, sleep=slept.append)
    assert res["contended"] is True and slept == []
    assert res["load"]["settled"] is res["load"]["end"] and res["load"]["residue_fraction"] == 1.0


def test_machine_metadata_names_the_commit_and_resources():
    m = harness.machine()
    for key in (
        "cpu_count",
        "memory_gb",
        "docker_vm_memory_gb",
        "git_sha",
        "git_dirty",
        "platform",
    ):
        assert key in m
    snap = harness.load_snapshot()
    assert {"load_1m", "load_5m", "load_15m", "uptime", "contended"} <= set(snap)


def test_every_pool_worker_is_warmed_before_timing():
    with harness.warmed_pool(2) as (ex, warmed):
        assert warmed == 2  # each worker took part; none took two warm-ups
        assert ex._max_workers == 2


def test_retrieval_samples_are_distinct_and_fresh_per_repeat():
    universe = [f"S{k:03d}" for k in range(50)]
    a, b = run_all.sample_symbols(universe, 20, 0), run_all.sample_symbols(universe, 20, 1)
    assert len(a) == len(set(a)) == 20  # without replacement (#1)
    assert a != b  # each repeat draws its own sample
    assert run_all.sample_symbols(universe, 20, 0) == a  # and is reproducible
    assert len(run_all.sample_symbols(universe[:5], 20, 0)) == 5


def test_a_cold_run_without_privileges_stays_labelled_warm(monkeypatch):
    monkeypatch.setattr(run_all.harness, "cold_capability", lambda: (None, "needs root"))
    out = run_all.bench(
        "tiny",
        None,
        repeats=2,
        cold=True,
        skip_parallel=True,
        skip_postgres=True,
        skip_redis=True,
        settle_s=0,  # the settle wait itself is tested above
    )
    assert out["cache"] == {"mode": "warm", "note": "needs root"}
    duck = out["retrieval"]["duckdb_parquet"]
    assert duck["cache"] == "warm" and duck["in_process"] is True
    assert len(duck["repeat_medians_ms"]) == 2 and duck["n"] == 2 * 10
    assert out["data_is_synthetic"] is True
    assert isinstance(out["contended"], bool) and "start" in out["load"]
    assert all(e["n"] == 2 and e["in_process"] for e in out["engine"])
    assert out["environment"]["git_sha"] is None or len(out["environment"]["git_sha"]) == 40


def test_the_parallel_section_reports_warmed_workers_and_spread(tmp_path):
    from quantlab.data.store import DatasetStore
    from quantlab.data.synthetic import SyntheticSpec, generate_market

    m = generate_market(SyntheticSpec(n_assets=10, start="2021-01-04", end="2022-12-30", seed=2))
    dv = DatasetStore(tmp_path).write("bench", m.bars, m.actions, m.provenance)
    par = run_all.parallel(str(dv.path), runs=2, repeats=2)
    assert par["workers"]["2"]["warmed_workers"] == 2
    assert par["workers"]["4"]["warmed_workers"] == 4
    assert par["workers"]["4"]["n"] == 2 and par["workers"]["4"]["in_process"] is False
    assert par["serial_wall"]["n"] == 2
