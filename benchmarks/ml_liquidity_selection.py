"""ML panel selection by liquidity: the retracted 2·L staleness cap vs the covariates (#120).

Reproduces the table in docs/METHODOLOGY.md ("ML features"). **The market is synthetic**
(``generate_market``, seed 11). Some of its names are thinned to trade on a random share
of sessions, one trades every other session, one is halted for 40 sessions, and one, with
full history, has the same halt printed as zero-volume bars repeating its last close. For
each name the table reports the share of its *candidate* rows each rule keeps. A candidate
row is a date the panel samples (at least 10 tradable names) on which the name has a bar
with volume (a zero-volume bar is no bar, #142), its own-bar features are complete apart
from staleness, and its label is defined.

* **2·L cap (retracted in #125).** A row is excluded when any feature's oldest bar lies
  more than twice its lookback in sessions back. It is recomputed here from the session
  positions of every printed bar, as it counted them, so it cannot see a zero-volume halt.
* **Covariates (pipeline 3).** Nothing is excluded for staleness; ``bars_span_21`` and
  ``log1p_max_gap_252`` (model inputs) describe it. Zero-volume bars are removed first,
  so a halt printed as zero-volume bars reads exactly like one without bars and none of
  its bars is a panel row (last column). This is what ``build_panel`` does.

Usage::

    uv run python benchmarks/ml_liquidity_selection.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quantlab.data.synthetic import SyntheticSpec, generate_market
from quantlab.ml.pipeline import FEATURES, MLConfig, build_panel, compute_features

# The retracted rule's per-feature lookbacks (the oldest bar used is t - L).
RETRACTED_LOOKBACK = {
    "mom_21": 21,
    "mom_63": 63,
    "mom_126": 126,
    "mom_252_21": 252,
    "rev_5": 5,
    "vol_21": 21,
    "vol_63": 63,
    "dist_high_252": 251,
    "volume_z_21": 20,
}
COVARIATES = ("bars_span_21", "log1p_max_gap_252")


def _market(n_assets: int, start: str, end: str, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    m = generate_market(
        SyntheticSpec(n_assets=n_assets, start=start, end=end, seed=seed, split_probability=0.002)
    )
    return m.bars, m.actions


def selection_table(
    n_assets: int = 30,
    start: str = "2018-01-02",
    end: str = "2022-12-30",
    seed: int = 11,
    halt_at: int = 600,
) -> list[dict]:
    """One row per probed name: liquidity and the share of candidate rows each rule keeps."""
    bars, acts = _market(n_assets, start, end, seed)
    d = pd.DatetimeIndex(sorted(bars["date"].unique()))
    syms = sorted(set(bars["symbol"]) - {"SYNIDX"})
    rng = np.random.default_rng(1)
    names: dict[str, object] = {
        syms[1]: 0.50,
        syms[2]: 0.45,
        syms[3]: 0.60,
        syms[4]: 0.80,
        syms[5]: "every other session",
        syms[6]: "40-session halt",
        syms[8]: "40-session halt, zero-volume bars",  # full history, like syms[6]
        syms[10]: "dense, never halted",
    }
    keep = pd.Series(True, index=bars.index)
    for s, p in names.items():
        idx = bars.index[bars["symbol"] == s]
        if p == "every other session":
            keep[idx] = np.arange(len(idx)) % 2 == 0
        elif p == "40-session halt":
            keep[idx] = ~bars.loc[idx, "date"].isin(d[halt_at : halt_at + 40])
        elif isinstance(p, float):
            keep[idx] = rng.random(len(idx)) < p
    sb = bars[keep].copy()
    # The same halt printed as zero-volume bars repeating the last close (#142).
    zs = next(s for s, p in names.items() if p == "40-session halt, zero-volume bars")
    zero = (sb["symbol"] == zs) & sb["date"].isin(d[halt_at : halt_at + 40])
    last_close = sb.loc[(sb["symbol"] == zs) & (sb["date"] < d[halt_at]), "close"].iloc[-1]
    sb.loc[zero, ["open", "high", "low", "close"]] = last_close
    sb.loc[zero, "volume"] = 0.0
    cfg = MLConfig(sample_every=1)
    panel = build_panel(sb, acts, cfg)
    feats, _ = compute_features(sb, acts)
    base = [f for f in FEATURES if f not in COVARIATES]
    last = d[-1 - cfg.horizon]
    # Dates the panel samples at all: build_panel skips a date with fewer than 10 tradable
    # names, a cross-section rule that is not about any one name's staleness.
    sampled = set(panel["date"])
    out = []
    for s, p in names.items():
        own = sb.loc[sb["symbol"] == s, "date"].sort_values().reset_index(drop=True)
        pos = pd.Series(d.get_indexer(own), index=own)
        complete = pd.concat([feats[f][s] for f in base], axis=1).notna().all(axis=1)
        cand = [t for t in own if t in sampled and t <= last and complete.get(t, False)]
        stale = pd.Series(False, index=own)
        for lookback in RETRACTED_LOOKBACK.values():
            stale |= (pos - pos.shift(lookback)) > 2 * lookback
        rows = panel[panel["symbol"] == s]
        zero_dates = set(sb.loc[(sb["symbol"] == s) & (sb["volume"] <= 0), "date"])
        out.append(
            {
                "name": p if isinstance(p, str) else f"random {p:.0%}",
                # Sessions with an actual trade (a zero-volume bar is not one).
                "traded": int(((sb["symbol"] == s) & (sb["volume"] > 0)).sum()) / len(d),
                "candidates": len(cand),
                "kept_cap": sum(not stale[t] for t in cand),
                "kept_covariates": len(set(rows["date"]) & set(cand)),
                "bars_span_21": float(rows["bars_span_21"].median()) if len(rows) else None,
                "max_gap_252": float(rows["max_gap_252"].max()) if len(rows) else None,
                "rows_on_zero_volume": len(set(rows["date"]) & zero_dates),
            }
        )
    return out


def markdown(rows: list[dict]) -> str:
    def share(k: int, n: int) -> str:
        return f"{k / n:.0%} ({k})" if n else "n/a"

    lines = [
        "SYNTHETIC market (quantlab.data.synthetic); not real data.",
        "",
        "| name | sessions traded | candidate rows | kept, 2·L cap (retracted) "
        "| kept, covariates (pipeline 3) | median bars_span_21 | max max_gap_252 "
        "| panel rows on zero-volume bars |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        n = r["candidates"]
        span = "n/a" if r["bars_span_21"] is None else f"{r['bars_span_21']:.2f}"
        gap = "n/a" if r["max_gap_252"] is None else f"{r['max_gap_252']:.0f}"
        lines.append(
            f"| {r['name']} | {r['traded']:.0%} | {n} | {share(r['kept_cap'], n)} "
            f"| {share(r['kept_covariates'], n)} | {span} | {gap} "
            f"| {r['rows_on_zero_volume']} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    print(markdown(selection_table()))
