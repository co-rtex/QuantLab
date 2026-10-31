"""Peak memory of one backtest job, as the worker runs it (#163).

Each configuration runs in a fresh process, which does what ``backtest_task`` does: run the
engine, analyse the result, take its fingerprint and write the artifact frames. It reports
the peak resident set after each phase. **Synthetic data**; measured, never extrapolated.

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


def one(assets: int, start: int, end: int, rebalance: str, strategy: str) -> dict[str, Any]:
    """One configuration, in this process."""
    from quantlab.analytics.summary import analyze_backtest
    from quantlab.backtest.engine import BacktestConfig, run_backtest
    from quantlab.data.synthetic import SyntheticSpec, generate_market
    from quantlab.strategies.library import create_strategy

    m = generate_market(
        SyntheticSpec(n_assets=assets, start=f"{start}-01-03", end=f"{end}-12-29", seed=7)
    )
    phases = {"data": _peak_mb()}
    cfg = BacktestConfig(rebalance=rebalance, benchmark="SYNIDX")  # type: ignore[arg-type]
    t = time.perf_counter()
    res = run_backtest(m.bars, m.actions, create_strategy(strategy), cfg)
    engine_s = time.perf_counter() - t
    phases["engine"] = _peak_mb()
    analysis = analyze_backtest(res, None)
    phases["analysis"] = _peak_mb()
    res.fingerprint()
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("trades", "positions", "weights_target", "rejections"):
            getattr(res, name).to_parquet(Path(tmp) / f"{name}.parquet", index=False)
        (Path(tmp) / "analysis.json").write_text(json.dumps(analysis, default=str))
    phases["artifacts"] = _peak_mb()
    return {
        "assets": assets,
        "years": f"{start}-{end}",
        "sessions": int(res.meta["sessions"]),
        "rebalance": rebalance,
        "strategy": strategy,
        "trades": len(res.trades),
        "position_rows": len(res.positions),
        "decisions": len(res.weights_target),
        "engine_s": round(engine_s, 2),
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
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    if a.one:
        print(json.dumps(one(a.assets[0], a.start, a.end, a.rebalance[0], a.strategy[0])))
        return
    start = harness.load_snapshot()
    rows = []
    for n, reb, strat in itertools.product(a.assets, a.rebalance, a.strategy):
        cmd = [
            sys.executable,
            __file__,
            "--one",
            "--assets",
            str(n),
            "--start",
            str(a.start),
            "--end",
            str(a.end),
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
