"""A/B record of engine outputs, to show a change leaves the economics unchanged (#184).

Usage::

    uv run python -m tests.oracle.ab record before.json   # on the old engine
    uv run python -m tests.oracle.ab record after.json    # on the new one
    uv run python -m tests.oracle.ab compare before.json after.json

It runs the 800 differential runs (``default_runs`` over seeds 0-199) and every library
strategy on three synthetic markets (both execution modes, whole shares and fractional
shares with a small capital, so cash runs short). For each run it stores a hash of each
output separately: equity, trades, positions, decisions, ledger transactions, meta
(without ``engine_version``), the fingerprint and the rejections. ``compare`` lists the
outputs that differ in any run.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

OUTPUTS = (
    "equity",
    "trades",
    "positions",
    "decisions",
    "ledger",
    "meta",
    "fingerprint",
    "rejections",
)


def _h(frame: pd.DataFrame) -> str:
    if not len(frame):
        return "empty:" + ",".join(map(str, frame.columns))
    text = frame.to_csv(index=True, float_format="%.17g")
    return hashlib.sha256(text.encode()).hexdigest()


def digest(res: Any) -> dict[str, str]:
    meta = {k: v for k, v in res.meta.items() if k != "engine_version"}
    ledger = pd.DataFrame([t.to_dict() for t in res.ledger.transactions])
    return {
        "equity": _h(res.equity),
        "trades": _h(res.trades),
        "positions": _h(res.positions),
        "decisions": _h(res.weights_target),
        "ledger": _h(ledger),
        "meta": hashlib.sha256(json.dumps(meta, sort_keys=True, default=str).encode()).hexdigest(),
        "fingerprint": res.fingerprint(),
        "rejections": _h(res.rejections),
    }


def runs() -> dict[str, dict[str, str]]:
    from quantlab.backtest.engine import BacktestConfig, run_backtest
    from quantlab.data.synthetic import BENCHMARK_SYMBOL, SyntheticSpec, generate_market
    from quantlab.strategies.library import REGISTRY, create_strategy
    from tests.oracle.differential import default_runs, keyed, run_engine, strategies_for
    from tests.oracle.universes import scenario

    out: dict[str, dict[str, str]] = {}
    for seed in range(200):
        sc = scenario(seed)
        data = keyed(sc)
        for execution, name in default_runs(seed):
            engine_strategy, _ = strategies_for(name, seed)
            res = run_engine(sc, {**sc.cfg, "execution": execution}, engine_strategy, data)
            out[f"oracle/{seed}/{execution}/{name}"] = digest(res)
    small = {"lookback": 30, "skip": 5, "fast": 10, "slow": 40}
    for mseed in (3, 11, 29):
        market = generate_market(
            SyntheticSpec(n_assets=30, start="2019-01-02", end="2021-12-31", seed=mseed)
        )
        master = market.master
        for key, cls in sorted(REGISTRY.items()):
            fields = cls.Params.model_fields
            params = {k: v for k, v in small.items() if k in fields}
            for execution in ("next_open", "next_close"):
                for fractional, capital in ((False, 1_000_000.0), (True, 20_000.0)):
                    cfg = BacktestConfig(
                        execution=execution,
                        rebalance="weekly",
                        fractional_shares=fractional,
                        initial_capital=capital,
                        benchmark=BENCHMARK_SYMBOL,
                    )
                    res = run_backtest(
                        market.bars,
                        market.actions,
                        create_strategy(key, params),
                        cfg,
                        instruments=master,
                    )
                    out[f"synthetic/{mseed}/{key}/{execution}/{fractional}"] = digest(res)
    return out


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    if set(before) != set(after):
        raise SystemExit("the two records cover different runs")
    return {o: sum(before[r][o] != after[r][o] for r in before) for o in OUTPUTS}


if __name__ == "__main__":
    cmd, *paths = sys.argv[1:]
    if cmd == "record":
        with open(paths[0], "w") as f:
            json.dump(runs(), f, indent=0, sort_keys=True)
    elif cmd == "compare":
        before, after = (json.loads(Path(p).read_text()) for p in paths)
        print(f"{len(before)} runs; runs in which each output differs:")
        for output, n in compare(before, after).items():
            print(f"  {output}: {n}")
    else:
        raise SystemExit(__doc__)
