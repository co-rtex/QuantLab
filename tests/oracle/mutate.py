"""Mutation harness for the engine (#180): does the test suite catch injected defects?

Usage::

    uv run python -m tests.oracle.mutate            # every mutation
    uv run python -m tests.oracle.mutate M3 M14     # some of them
    uv run python -m tests.oracle.mutate --seeds 50 # a smaller differential run
    uv run python -m tests.oracle.mutate --oracle-only  # skip the other engine tests

For each mutation it replaces one exact snippet of a source file (the snippet must occur
exactly once), runs the differential suite (``tests/unit/test_engine_oracle.py``), then the
engine's other unit tests (``OTHER_TESTS``), restores the file in a ``finally`` and checks
that ``git diff`` of the file is empty. A mutation is caught when a suite fails. It prints a
Markdown table. The suites stop at their first failure, so a caught mutation is quick.

M1-M8 are the mutations of the release-candidate assessment (2026-10-31), matched by text.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENG = "packages/quantlab/src/quantlab/backtest/engine.py"
ADJ = "packages/quantlab/src/quantlab/data/adjust.py"
ORACLE = "tests/unit/test_engine_oracle.py"
OTHER_TESTS = [
    "tests/unit/test_backtest.py",
    "tests/unit/test_corporate_actions.py",
    "tests/unit/test_delisting_returns.py",
    "tests/unit/test_engine_pit_adjustment.py",
    "tests/unit/test_zero_volume_fills.py",
    "tests/unit/test_research_correctness.py",
    "tests/unit/test_ledger.py",
]


@dataclass(frozen=True)
class Mutation:
    key: str
    path: str
    old: str
    new: str
    what: str


MUTATIONS = [
    # The release-candidate assessment's M1-M8.
    Mutation(
        "M1",
        ENG,
        "adj_full[: k + 1] / scale, index=dates[: k + 1], columns=universe",
        "adj_full[: k + 2] / scale, index=dates[: k + 2], columns=universe",
        "strategy view: one-bar look-ahead",
    ),
    Mutation(
        "M2",
        ENG,
        'px_row = O[k] if config.execution == "next_open" else C[k]',
        'px_row = C[k - 1] if config.execution == "next_open" else C[k]',
        "next_open fills at the decision close",
    ),
    Mutation(
        "M3",
        ENG,
        "fill(k, np.where(no_trade[k], np.nan, px_row), pending)",
        "fill(k, px_row, pending)",
        "fills on zero-volume bars",
    ),
    Mutation(
        "M4",
        ENG,
        "if sym in delisted or d <= when:",
        "if sym in delisted or d < when:",
        "delisting one session early",
    ),
    Mutation(
        "M5",
        ENG,
        "value = last_px[j] * (1 + ret) if has_ret else last_px[j]",
        "value = last_px[j]",
        "delisting return ignored",
    ),
    Mutation(
        "M6",
        ENG,
        "o.quantity *= ratio",
        "o.quantity *= 1.0",
        "pending orders not restated across a split",
    ),
    Mutation(
        "M7",
        ENG,
        "if divs[k, j] > 0:",
        "if divs[k, j] > 1e300:",
        "dividends never credited",
    ),
    Mutation(
        "M8",
        ADJ,
        "return self._rev[as_of_pos + 1]",
        "return self._rev[min(as_of_pos + 2, len(self._rev) - 1)]",
        "as-of adjustment: one-bar look-ahead",
    ),
    # New mutations for the differential suite.
    Mutation(
        "M9",
        ENG,
        "if need > cash > 0:",
        "if False:",
        "buys not scaled when cash is short (sort order decides)",
    ),
    Mutation(
        "M10",
        ENG,
        "key=lambda o: (o.quantity > 0, o.symbol)",
        "key=lambda o: (o.quantity < 0, o.symbol)",
        "buys before sells",
    ),
    Mutation(
        "M11",
        ENG,
        "commission = max(config.commission_min, notional * comm) if notional else 0.0",
        "commission = notional * comm",
        "minimum commission ignored",
    ),
    Mutation(
        "M12",
        ENG,
        "px = ref * (1 + slip) if qty > 0 else ref * (1 - slip)",
        "px = ref * (1 + slip)",
        "sells pay slippage in the wrong direction",
    ),
    Mutation(
        "M13",
        ENG,
        "if missing_run[j] >= config.delist_after_missing and",
        "if missing_run[j] > config.delist_after_missing and",
        "missing-bars delisting one session late",
    ),
    Mutation(
        "M14",
        ENG,
        "missing_run = np.where(has_bar[k], 0, missing_run + 1)",
        "missing_run = np.where(traded_bar[k], 0, missing_run + 1)",
        "a zero-volume bar counts as missing for the delisting rule",
    ),
    Mutation(
        "M15",
        ADJ,
        'pos = bar_dates.searchsorted(e, side="left")',
        'pos = bar_dates.searchsorted(e, side="right")',
        "actions placed one bar after their ex-date",
    ),
    Mutation(
        "M16",
        ADJ,
        '_KIND_ORDER = {"dividend": 0, "split": 1}',
        '_KIND_ORDER = {"dividend": 1, "split": 0}',
        "same-day split applied before the dividend",
    ),
    Mutation(
        "M17",
        ENG,
        'acts = acts[cutoff.isna() | (acts["ex_date"] <= cutoff)]',
        "acts = acts",
        "actions after a delisting still applied",
    ),
    Mutation(
        "M18",
        ENG,
        "tradable=pd.Series(has_bar[k] & ~(delist_day <= d.to_datetime64()), index=universe)",
        "tradable=pd.Series(has_bar[k], index=universe)",
        "delisted securities still offered to the strategy",
    ),
    Mutation(
        "M19",
        ENG,
        "t = t.clip(upper=config.max_weight)",
        "t = t",
        "max_weight ignored",
    ),
    Mutation(
        "M20",
        ENG,
        "close=view_close,",
        "close=close_raw.iloc[: k + 1],",
        "strategy sees unadjusted closes",
    ),
    Mutation(
        "M21",
        ENG,
        "stale_held = [s for s in ledger.positions if not traded_bar[k, col[s]]]",
        "stale_held = [s for s in ledger.positions if not has_bar[k, col[s]]]",
        "zero-volume marks not counted as stale",
    ),
    Mutation(
        "M22",
        ENG,
        "mask = np.r_[key[1:] != key[:-1], True]",
        "mask = np.r_[True, key[1:] != key[:-1]]",
        "rebalance on the first session of a period, not the last",
    ),
    Mutation(
        "M23",
        ENG,
        "partial = o.scaled",
        "partial = False",
        "scaled buys not flagged partial",
    ),
    Mutation(
        "M24",
        ENG,
        "last_px[j] = last_px[j] / splits[k, j]",
        "last_px[j] = last_px[j]",
        "last close not restated across a split",
    ),
]


def _pytest(paths: list[str], env: dict[str, str]) -> int:
    cmd = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *paths]
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True).returncode


def _verdict(code: int) -> str:
    return {0: "missed", 1: "caught"}.get(code, f"error (exit {code})")


def run(keys: list[str], seeds: int, others: bool = True) -> int:
    env = {**os.environ, "QUANTLAB_ORACLE_SEEDS": str(seeds), "PYTHONPATH": str(ROOT)}
    chosen = [m for m in MUTATIONS if not keys or m.key in keys]
    paths = sorted({m.path for m in chosen})
    if subprocess.run(["git", "diff", "--quiet", "--", *paths], cwd=ROOT).returncode:
        raise SystemExit(f"commit or discard local changes to {paths} first")
    # A failing baseline would make every mutation look caught.
    if _pytest([ORACLE], env) != 0 or (others and _pytest(OTHER_TESTS, env) != 0):
        raise SystemExit("the suites fail without a mutation; fix that first")
    print(f"Differential seeds: {seeds}\n")
    print("| Mutation | Defect | Differential suite | Other engine tests |")
    print("|---|---|---|---|")
    missed = 0
    for m in chosen:
        path = ROOT / m.path
        source = path.read_text()
        if source.count(m.old) != 1:
            print(f"| {m.key} | {m.what} | snippet found {source.count(m.old)} times | |")
            missed += 1
            continue
        started = time.time()
        try:
            path.write_text(source.replace(m.old, m.new))
            oracle = _pytest([ORACLE], env)
            other = _pytest(OTHER_TESTS, env) if others else None
        finally:
            path.write_text(source)
        clean = subprocess.run(["git", "diff", "--quiet", "--", m.path], cwd=ROOT).returncode
        if clean != 0:
            raise SystemExit(f"{m.path} was not restored after {m.key}")
        missed += oracle != 1
        print(
            f"| {m.key} | {m.what} | {_verdict(oracle)} |"
            f" {'not run' if other is None else _verdict(other)} |"
            f" <!-- {time.time() - started:.0f}s -->"
        )
    return 1 if missed else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    seeds = 200
    if "--seeds" in args:
        i = args.index("--seeds")
        seeds = int(args[i + 1])
        del args[i : i + 2]
    others = "--oracle-only" not in args
    args = [a for a in args if a != "--oracle-only"]
    raise SystemExit(run(args, seeds, others))
