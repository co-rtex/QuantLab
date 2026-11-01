"""#253: reproduce SLSQP failures at the optimize API's defaults, and measure KKT residuals
at failing and at successful solves (to choose the acceptance tolerance). Synthetic only.

API defaults (routes/optimize.py): lookback 252, ledoit_wolf, max_weight 0.4, risk
aversion 5, mu = window mean x 252, cap = max(0.4, 1/n). Markets 3, 11 and 29 with
n_assets = 30 and 100, 2019-01-02 to 2021-12-31, every 20th session.

The residual is for the scaled problem _solve_qp solves (O(1) objective):
  min 0.5 w'qw - c'w  s.t.  sum w = 1, lo <= w <= hi,  q = ra*cov/scale, c = mu/scale.
With g = qw - c, w is optimal iff some lambda has g_i = lambda inside the bounds,
g_i >= lambda at lo and g_i <= lambda at hi. residual = min over lambda of the worst
violation (convex in lambda: ternary search), plus feasibility |sum w - 1|.
"""

import math
import sys
from collections import Counter

import numpy as np

from quantlab.analytics.risk import covariance
from quantlab.data.adjust import total_returns
from quantlab.data.synthetic import BENCHMARK_SYMBOL, SyntheticSpec, generate_market
from quantlab.optimize import allocators
from quantlab.optimize.allocators import Bounds, OptimizationError, mean_variance, min_variance

BOUND_TOL = 1e-9  # a weight this close to a bound counts as at the bound


def scaled_problem(cov, mu, ra):
    n = cov.shape[0]
    lin = np.zeros(n) if mu is None else np.asarray(mu, dtype=float)
    scale = max(float(np.trace(cov)) / n * ra, float(np.abs(lin).max()), 1e-12)
    return ra * cov / scale, lin / scale


def kkt_residual(w, q, c, lo, hi):
    g = q @ w - c
    at_lo, at_hi = w <= lo + BOUND_TOL, w >= hi - BOUND_TOL
    inner = ~(at_lo | at_hi)

    def worst(lam):
        v = np.zeros_like(g)
        v[inner] = np.abs(g[inner] - lam)
        v[at_lo] = np.maximum(0.0, lam - g[at_lo])
        v[at_hi] = np.maximum(0.0, g[at_hi] - lam)
        return float(v.max())

    a, b = float(g.min()), float(g.max())
    for _ in range(200):
        m1, m2 = a + (b - a) / 3, b - (b - a) / 3
        if worst(m1) <= worst(m2):
            b = m2
        else:
            a = m1
    return worst(0.5 * (a + b)), abs(float(w.sum()) - 1.0)


last = {}
real = allocators.minimize


def recording(*args, **kwargs):
    res = real(*args, **kwargs)
    last["res"] = res
    return res


allocators.minimize = recording
rows = []
for n_assets in (30, 100):
    for seed in (3, 11, 29):
        m = generate_market(SyntheticSpec(n_assets=n_assets, start="2019-01-02", end="2021-12-31", seed=seed))
        rets = total_returns(m.bars, m.actions).drop(columns=[BENCHMARK_SYMBOL])
        for end in range(253, len(rets) + 1, 20):
            w = rets.iloc[end - 252 : end]
            usable = [s for s in w.columns if w[s].notna().sum() >= int(0.9 * 252)]
            w = w[usable].dropna()
            if w.shape[1] < 2:
                continue
            cov = covariance(w, method="ledoit_wolf").to_numpy()
            mu = w.mean().to_numpy() * 252
            n = len(usable)
            cap = max(0.4, 1.0 / n)
            for method in ("min_variance", "mean_variance"):
                try:
                    if method == "min_variance":
                        min_variance(cov, Bounds(0, cap))
                    else:
                        mean_variance(mu, cov, 5.0, Bounds(0, cap))
                    status = "ok"
                except OptimizationError as exc:
                    status = str(exc)
                res = last["res"]
                q, c = scaled_problem(cov, None if method == "min_variance" else mu, 5.0 if method == "mean_variance" else 1.0)
                r, feas = kkt_residual(res.x, q, c, 0.0, cap)
                rows.append((n_assets, seed, method, n, status, int(res.status), r, feas,
                             float(res.x.min()), float(res.x.max())))

print("platform note:", sys.platform)
for n_assets in (30, 100):
    for method in ("min_variance", "mean_variance"):
        sub = [r for r in rows if r[0] == n_assets and r[2] == method]
        st = Counter(r[4] if r[4] == "ok" else f"fail (mode {r[5]})" for r in sub)
        print(f"n={n_assets} {method}: {len(sub)} solves {dict(st)}")
        for label, pick in (("ok", lambda r: r[4] == "ok"), ("failed", lambda r: r[4] != "ok")):
            res_ = [r[6] for r in sub if pick(r)]
            if not res_:
                continue
            feas = max(r[7] for r in sub if pick(r))
            lo_ = min(r[8] for r in sub if pick(r))
            print(f"    {label:6s}: KKT residual max {max(res_):.3e}, median {np.median(res_):.3e}; "
                  f"max |sum-1| {feas:.1e}; min weight {lo_:.2e}")
            hist = Counter(math.floor(math.log10(x)) if x > 0 else -99 for x in res_)
            print("            log10 bins:", dict(sorted(hist.items())))
