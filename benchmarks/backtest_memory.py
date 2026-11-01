"""Peak memory of one backtest job, as the worker runs it (#163).

The dataset is generated once per size and saved as Parquet. Each configuration then runs
in a fresh process along ``backtest_task``'s path: load and key the bars by instrument, run
the engine, analyse, relabel for display, write the artifacts and take the fingerprint. It
reports the peak resident set after each phase, and the result fingerprint (so a memory
change can be shown not to change results). **Synthetic data**; measured, never
extrapolated. The worker container's own figure (``worker_memory.py``) also counts the
RQ parent process, which shares its cgroup.

Usage::

    uv run python benchmarks/backtest_memory.py --assets 100 200 400 --start 2005 \
        --end 2023 --rebalance daily monthly --strategy equal_weight momentum
"""

from __future__ import annotations

import argparse
import itertools
import json
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness


def _peak_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Bytes on macOS, kilobytes on Linux.
    return round(peak / 2**20 if sys.platform == "darwin" else peak / 2**10, 1)


def generate(assets: int, start: int, end: int, out: Path) -> None:
    """A synthetic dataset as Parquet files, so the measured process only loads it."""
    from quantlab.data.synthetic import SyntheticSpec, generate_market

    m = generate_market(
        SyntheticSpec(n_assets=assets, start=f"{start}-01-03", end=f"{end}-12-29", seed=7)
    )
    m.bars.to_parquet(out / "bars.parquet", index=False)
    m.actions.to_parquet(out / "actions.parquet", index=False)
    m.symbol_history.to_parquet(out / "history.parquet", index=False)


def one(data: Path, rebalance: str, strategy: str) -> dict[str, Any]:
    """One configuration in this process, along ``backtest_task``'s path."""
    import pandas as pd

    from quantlab.analytics.summary import analyze_backtest
    from quantlab.backtest.engine import BacktestConfig
    from quantlab.data.identity import key_by_instrument
    from quantlab_api import artifacts
    from quantlab_api.tasks import ExperimentInputs, _for_display, run_experiment_backtest

    phases = {"imports": _peak_mb()}
    history = pd.read_parquet(data / "history.parquet")
    bars, actions = key_by_instrument(
        pd.read_parquet(data / "bars.parquet"), pd.read_parquet(data / "actions.parquet"), history
    )
    bench = history.loc[history["symbol"] == "SYNIDX", "instrument_id"].iloc[0]
    cfg = BacktestConfig(rebalance=rebalance, benchmark=bench)  # type: ignore[arg-type]
    inputs = ExperimentInputs(bars, actions, cfg, history, master=None)
    phases["data"] = _peak_mb()
    exp = type("Exp", (), {"strategy_key": strategy, "strategy_params": {}})()
    t = time.perf_counter()
    res = run_experiment_backtest(exp, inputs)  # type: ignore[arg-type]
    engine_s = time.perf_counter() - t
    phases["engine"] = _peak_mb()
    analysis = analyze_backtest(res, None)
    phases["analysis"] = _peak_mb()
    shown, analysis = _for_display(res, analysis, inputs)
    phases["display"] = _peak_mb()
    with tempfile.TemporaryDirectory() as tmp:
        artifacts.write_backtest(Path(tmp), shown, analysis)
    res.fingerprint()
    phases["artifacts"] = _peak_mb()
    return {
        "assets": int(history["instrument_id"].nunique()) - 1,
        "bars": len(bars),
        "sessions": int(res.meta["sessions"]),
        "rebalance": rebalance,
        "strategy": strategy,
        "trades": len(res.trades),
        "position_rows": len(res.positions),
        "decisions": len(res.weights_target),
        "engine_s": round(engine_s, 2),
        "fingerprint": res.fingerprint(),
        "peak_rss_mb": phases,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", type=int, nargs="+", default=[100, 200, 400])
    ap.add_argument("--start", type=int, default=2005)
    ap.add_argument("--end", type=int, default=2023)
    ap.add_argument("--rebalance", nargs="+", default=["daily", "monthly"])
    ap.add_argument("--strategy", nargs="+", default=["equal_weight"])
    ap.add_argument("--one", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--data", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    if a.one:
        print(json.dumps(one(a.data, a.rebalance[0], a.strategy[0])))
        return
    start = harness.load_snapshot()
    rows = []
    for n in a.assets:
        with tempfile.TemporaryDirectory() as tmp:
            generate(n, a.start, a.end, Path(tmp))
            for reb, strat in itertools.product(a.rebalance, a.strategy):
                cmd = [
                    sys.executable,
                    __file__,
                    "--one",
                    "--data",
                    tmp,
                    "--rebalance",
                    reb,
                    "--strategy",
                    strat,
                ]
                out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
                rows.append(json.loads(out.strip().splitlines()[-1]))
                print(json.dumps(rows[-1]), flush=True)
    res = harness.finalize(
        {
            "mode": "backtest_memory",
            "environment": harness.machine(),
            "data_is_synthetic": True,
            "rows": rows,
        },
        start,
    )
    if a.out:
        a.out.write_text(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
