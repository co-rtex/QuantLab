"""Smoke test for benchmarks/ml_liquidity_selection.py (the METHODOLOGY table, #120)."""

from __future__ import annotations

from benchmarks import ml_liquidity_selection as sel


def test_covariates_keep_every_candidate_and_the_cap_drops_thin_names():
    rows = {
        r["name"]: r for r in sel.selection_table(n_assets=12, start="2018-01-02", end="2021-12-31")
    }
    # A name can lack candidates in this small market (it may delist before 252 own bars).
    probed = {k: r for k, r in rows.items() if r["candidates"] > 0}
    for r in probed.values():
        assert r["kept_covariates"] == r["candidates"], r  # nothing excluded for staleness
    thin = [probed[k] for k in ("random 50%", "random 45%") if k in probed]
    assert thin, rows
    assert all(r["kept_cap"] < r["candidates"] for r in thin), thin  # the cap's bias
    dense = probed["dense, never halted"]
    assert dense["kept_cap"] == dense["candidates"]
    assert probed["40-session halt"]["max_gap_252"] == 40


def test_the_output_is_labelled_synthetic():
    text = sel.markdown(
        [
            {
                "name": "x",
                "traded": 1.0,
                "candidates": 1,
                "kept_cap": 1,
                "kept_covariates": 1,
                "bars_span_21": 1.0,
                "max_gap_252": 0.0,
            }
        ]
    )
    assert text.startswith("SYNTHETIC market")
