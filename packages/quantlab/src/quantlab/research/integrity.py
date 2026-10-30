"""Research Integrity Report.

The report audits *how* a result was produced, not whether the strategy is good. Each
check is one of:

* ``verified``   evidence exists and supports the claim,
* ``warning``    evidence exists and reveals a risk the reader must weigh,
* ``failed``     evidence contradicts a methodological requirement,
* ``unassessed`` the evidence needed to judge this was not produced.

A clean report means the experiment is documented and its known biases are disclosed. It
is never evidence that a strategy will be profitable.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

CheckStatus = Literal["verified", "warning", "failed", "unassessed"]

DISCLAIMER = (
    "This report checks documentation, methodology and statistical uncertainty. It does not "
    "establish that the strategy will be profitable, and nothing in it is investment advice."
)


QUALITY_STATUS: dict[str, CheckStatus] = {"pass": "verified", "warn": "warning", "fail": "failed"}


@dataclass
class Check:
    key: str
    title: str
    status: CheckStatus
    finding: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _f2(x: float) -> str:
    """Two decimals without a misleading negative zero."""
    s = f"{x:.2f}"
    return "0.00" if s == "-0.00" else s


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _get(d: dict[str, Any] | None, *path: str) -> Any:
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def build_integrity_report(ev: dict[str, Any]) -> dict[str, Any]:
    """Build the report from an evidence document.

    Expected keys (all optional except ``experiment`` and ``dataset``): ``experiment``
    (config, strategy, engine/code versions, input hash, fingerprint), ``dataset``
    (version hash, provenance, quality status, delisted symbols), ``analysis`` (backtest
    analytics), ``validation`` (walk_forward, sensitivity, cost, bootstrap, deflated,
    leakage, regimes), ``reproductions`` and ``trials_on_dataset``.
    """
    exp, ds = ev["experiment"], ev["dataset"]
    cfg = exp.get("config", {})
    val = ev.get("validation") or {}
    analysis = ev.get("analysis") or {}
    checks: list[Check] = []

    # 1. dataset provenance
    prov = ds.get("provenance") or {}
    if ds.get("version_hash") and prov.get("provider"):
        if ds.get("is_synthetic"):
            checks.append(
                Check(
                    "dataset_provenance",
                    "Dataset source and version",
                    "warning",
                    "Exact dataset version recorded, but the data is synthetic. Results describe "
                    "the generator, not real markets.",
                    {"version_hash": ds["version_hash"], "provider": prov.get("provider")},
                )
            )
        else:
            checks.append(
                Check(
                    "dataset_provenance",
                    "Dataset source and version",
                    "verified",
                    "Exact dataset version and provider recorded.",
                    {
                        "version_hash": ds["version_hash"],
                        "provider": prov.get("provider"),
                        "license": prov.get("license"),
                    },
                )
            )
    else:
        checks.append(
            Check(
                "dataset_provenance",
                "Dataset source and version",
                "failed",
                "The dataset version or its provenance is missing.",
            )
        )

    # 2. data quality
    q = ds.get("quality_status")
    checks.append(
        Check(
            "data_quality",
            "Data quality checks",
            QUALITY_STATUS.get(q or "", "unassessed"),
            {
                "pass": "All automated quality checks passed.",
                "warn": "Quality checks raised warnings (see the dataset quality report).",
                "fail": "Quality checks failed; results may be corrupted by bad data.",
            }.get(q or "", "No quality report available."),
            {"quality_status": q},
        )
    )

    # 3. universe and survivorship
    delisted = set(ds.get("delisted_symbols") or [])
    universe = set(_get(analysis, "meta", "universe") or cfg.get("universe") or [])
    if not universe:
        checks.append(
            Check(
                "universe",
                "Universe definition and survivorship",
                "unassessed",
                "The traded universe was not recorded.",
            )
        )
    elif delisted and not (universe & delisted):
        checks.append(
            Check(
                "universe",
                "Universe definition and survivorship",
                "warning",
                "The universe excludes every symbol that was delisted during the sample. Choosing "
                "only survivors typically overstates historical returns.",
                {"universe_size": len(universe), "delisted_in_dataset": len(delisted)},
            )
        )
    else:
        checks.append(
            Check(
                "universe",
                "Universe definition and survivorship",
                "verified",
                f"Universe of {len(universe)} symbols recorded"
                + (
                    f", including {len(universe & delisted)} that were delisted in-sample."
                    if delisted
                    else "; the dataset contains no delistings."
                ),
                {"universe_size": len(universe), "delisted_included": len(universe & delisted)},
            )
        )

    # 4. execution timing
    checks.append(
        Check(
            "execution_timing",
            "Execution timing",
            "verified",
            "Signals use data up to the decision close; fills occur at the next session's "
            f"{'open' if cfg.get('execution', 'next_open') == 'next_open' else 'close'} "
            f"(engine v{exp.get('engine_version')}).",
            {"execution": cfg.get("execution", "next_open"), "rebalance": cfg.get("rebalance")},
        )
    )

    # 5. transaction costs
    comm, slip = float(cfg.get("commission_bps", 0)), float(cfg.get("slippage_bps", 0))
    cost = val.get("cost") or {}
    if comm + slip == 0:
        checks.append(
            Check(
                "transaction_costs",
                "Trading costs",
                "warning",
                "The backtest assumes zero commissions and slippage.",
                {"commission_bps": comm, "slippage_bps": slip},
            )
        )
    else:
        be = cost.get("breakeven_multiplier")
        finding = f"Costs modelled: {comm:g} bps commission and {slip:g} bps slippage per trade."
        status: CheckStatus = "verified"
        if cost:
            if be is not None:
                finding += f" Excess return over the benchmark disappears at {be:.1f}x these costs."
                if be < 2:
                    status = "warning"
            elif cost.get("note"):
                finding += f" Cost sweep: the strategy {cost['note']}."
        else:
            finding += " Cost sensitivity has not been run."
        drag = _get(analysis, "costs", "values", "cost_drag_cagr")
        checks.append(
            Check(
                "transaction_costs",
                "Trading costs",
                status,
                finding,
                {
                    "commission_bps": comm,
                    "slippage_bps": slip,
                    "breakeven_multiplier": be,
                    "cost_drag_cagr": drag,
                },
            )
        )

    # 6. look-ahead
    leak = val.get("leakage")
    if not leak:
        checks.append(
            Check(
                "lookahead",
                "Look-ahead leakage",
                "unassessed",
                "The empirical leakage test has not been run for this experiment.",
            )
        )
    elif leak.get("status") == "verified":
        checks.append(
            Check(
                "lookahead",
                "Look-ahead leakage",
                "verified",
                f"Signals on {len(leak.get('dates_tested', []))} sampled dates were identical when "
                "all later data was removed.",
                {"dates_tested": leak.get("dates_tested")},
            )
        )
    elif leak.get("status") == "failed":
        checks.append(
            Check(
                "lookahead",
                "Look-ahead leakage",
                "failed",
                "Signals changed when future data was removed: look-ahead bias.",
                {"mismatches": leak.get("mismatches")},
            )
        )
    else:
        checks.append(
            Check(
                "lookahead",
                "Look-ahead leakage",
                "unassessed",
                str(leak.get("reason", "Leakage test inconclusive.")),
            )
        )

    # 7. out-of-sample
    wf = val.get("walk_forward")
    if not wf or wf.get("error"):
        checks.append(
            Check(
                "out_of_sample",
                "Out-of-sample evaluation",
                "unassessed",
                wf.get("error", "No walk-forward or holdout evaluation was run.")
                if wf
                else "No walk-forward or holdout evaluation was run.",
            )
        )
    else:
        ins, oos = wf.get("mean_in_sample_sharpe"), wf.get("mean_oos_sharpe")
        combined = _get(wf, "oos_combined", "values", "sharpe")
        status = "verified"
        finding = (
            f"{len(wf['folds'])} walk-forward folds with parameters chosen in-sample. "
            f"Mean Sharpe in-sample {_f2(ins)}, out-of-sample {_f2(oos)}."
            if ins is not None and oos is not None
            else f"{len(wf['folds'])} walk-forward folds evaluated."
        )
        if oos is not None and (oos <= 0 or (ins is not None and ins > 0 and oos < 0.5 * ins)):
            status = "warning"
            finding += " Out-of-sample performance is materially weaker than in-sample."
        checks.append(
            Check(
                "out_of_sample",
                "Out-of-sample evaluation",
                status,
                finding,
                {
                    "folds": len(wf["folds"]),
                    "mean_in_sample_sharpe": ins,
                    "mean_oos_sharpe": oos,
                    "combined_oos_sharpe": combined,
                },
            )
        )

    # 8. parameter stability
    sens = val.get("sensitivity")
    if not sens:
        checks.append(
            Check(
                "parameter_stability",
                "Parameter stability",
                "unassessed",
                "No parameter sensitivity study was run.",
            )
        )
    else:
        s = sens["summary"]
        frac = s.get("positive_sharpe_fraction")
        changes = wf.get("distinct_params_selected") if wf and not wf.get("error") else None
        status = "verified"
        finding = f"{s['grid_size']} parameter sets evaluated."
        if frac is not None:
            finding += f" {frac:.0%} have a positive Sharpe ratio."
            if frac < 0.6:
                status = "warning"
        if changes and wf and changes > max(2, math.ceil(len(wf["folds"]) / 2)):
            status = "warning"
            finding += f" Walk-forward selected {changes} different parameter sets across folds."
        if s["grid_size"] <= 1:
            status, finding = "unassessed", "The strategy has no tunable parameters."
        checks.append(Check("parameter_stability", "Parameter stability", status, finding, dict(s)))

    # 9. statistical uncertainty
    boot = val.get("bootstrap")
    if not boot or boot.get("error"):
        checks.append(
            Check(
                "statistical_uncertainty",
                "Statistical uncertainty",
                "unassessed",
                "No bootstrap confidence intervals were computed.",
            )
        )
    else:
        sh = boot["sharpe"]
        status = "verified" if sh["lower"] > 0 else "warning"
        lo_s, hi_s = _f2(sh["lower"]), _f2(sh["upper"])
        finding = f"95% bootstrap interval for the Sharpe ratio: {lo_s} to {hi_s}."
        if sh["lower"] <= 0:
            finding += " The interval includes zero; skill is not distinguishable from luck."
        ex = boot.get("excess_return")
        if ex:
            finding += (
                f" Annualized excess return versus the benchmark: {ex['lower']:.1%} to "
                f"{ex['upper']:.1%}."
            )
        checks.append(
            Check(
                "statistical_uncertainty",
                "Statistical uncertainty",
                status,
                finding,
                {"sharpe": sh, "excess_return": ex},
            )
        )

    # 10. multiple testing
    dsr = val.get("deflated")
    trials = ev.get("trials_on_dataset")
    if dsr and dsr.get("deflated_sharpe") is not None:
        p = dsr["deflated_sharpe"]
        status = "verified" if p >= 0.95 else "warning"
        checks.append(
            Check(
                "multiple_testing",
                "Selection bias from multiple trials",
                status,
                f"Deflated Sharpe ratio {p:.2f} after {dsr['trials']} parameter trials"
                + (
                    f" ({_plural(trials, 'experiment')} on this dataset version in the workspace)."
                    if trials
                    else "."
                )
                + (
                    " Below 0.95, the best result is plausibly a product of search."
                    if p < 0.95
                    else ""
                ),
                {**dsr, "workspace_trials": trials},
            )
        )
    else:
        checks.append(
            Check(
                "multiple_testing",
                "Selection bias from multiple trials",
                "unassessed",
                "The deflated Sharpe ratio was not computed"
                + (
                    f"; {_plural(trials, 'experiment')} exist on this dataset version."
                    if trials
                    else "."
                ),
                {"workspace_trials": trials},
            )
        )

    # 11. benchmark
    if cfg.get("benchmark"):
        checks.append(
            Check(
                "benchmark",
                "Benchmark comparison",
                "verified",
                f"Compared against {cfg['benchmark']} on a total-return basis.",
                {"benchmark": cfg["benchmark"]},
            )
        )
    else:
        checks.append(
            Check(
                "benchmark",
                "Benchmark comparison",
                "warning",
                "No benchmark was configured, so excess return is unmeasured.",
            )
        )

    # 12. sample length
    sessions = _get(analysis, "meta", "sessions")
    if sessions is None:
        checks.append(
            Check("sample_length", "Sample length", "unassessed", "No completed backtest.")
        )
    else:
        years = sessions / 252
        checks.append(
            Check(
                "sample_length",
                "Sample length",
                "verified" if sessions >= 1250 else "warning",
                f"{years:.1f} years of daily data traded."
                + (
                    ""
                    if sessions >= 1250
                    else " Short samples rarely span multiple market regimes."
                ),
                {"sessions": sessions},
            )
        )

    # 13. reproducibility
    reps = ev.get("reproductions") or []
    finished = [r for r in reps if r.get("status") == "completed"]
    if not finished:
        checks.append(
            Check(
                "reproducibility",
                "Reproducibility",
                "unassessed",
                "Inputs, engine and code versions are recorded, but no reproduction has been run.",
                {"input_hash": exp.get("input_hash"), "code_version": exp.get("code_version")},
            )
        )
    elif all(r.get("fingerprint_match") for r in finished):
        checks.append(
            Check(
                "reproducibility",
                "Reproducibility",
                "verified",
                f"{len(finished)} independent re-run(s) produced byte-identical results.",
                {"fingerprint": exp.get("fingerprint")},
            )
        )
    else:
        checks.append(
            Check(
                "reproducibility",
                "Reproducibility",
                "failed",
                "A re-run from the same inputs produced different results.",
                {"reproductions": finished},
            )
        )

    counts = {
        s: sum(c.status == s for c in checks)
        for s in ("verified", "warning", "failed", "unassessed")
    }
    return {
        "schema": "quantlab.integrity-report/v1",
        "summary": counts,
        "checks": [asdict(c) for c in checks],
        "disclaimer": DISCLAIMER,
    }
