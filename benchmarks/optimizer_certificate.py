"""#253: why SLSQP reports failure at the optimum, and the Frank-Wolfe-gap separation that
sets allocators.OPTIMALITY_GAP (docs/METHODOLOGY.md, *Optimizers*). Synthetic data only.

    uv run python benchmarks/optimizer_certificate.py      # macOS (Accelerate)
    # and in the Linux API image (OpenBLAS), from the repository root:
    docker run --rm -v "$PWD/benchmarks:/b:ro" --entrypoint python <api image> \
        /b/optimizer_certificate.py

The problem, as _solve_qp scales it: min f(w) = 0.5 w'qw - c'w, sum w = 1, lo <= w <= hi.
FW gap(w) = g'w - min_{s in S} g's with g = qw - c; for convex f, f(w) - f* <= gap(w).
Everything here is independent of the allocator's code: the reference optimum is FISTA with
the capped-simplex projection, run to a gap under 1e-13, and SLSQP is called directly.
For each window it records SLSQP's status and point, the gap, f - f* and max|w - w*| there,
the residue at weights whose optimum is 0, SLSQP with ftol 1e-12, and the gaps of clearly
non-converged points (iteration-limited iterates, optima perturbed by noise).
"""

import math
import sys
from collections import Counter, defaultdict

import numpy as np
from scipy.optimize import minimize

from quantlab.analytics.risk import covariance
from quantlab.data.adjust import total_returns
from quantlab.data.synthetic import BENCHMARK_SYMBOL, SyntheticSpec, generate_market


def scaled(cov, mu, ra):
    n = cov.shape[0]
    lin = np.zeros(n) if mu is None else np.asarray(mu, float)
    s = max(float(np.trace(cov)) / n * ra, float(np.abs(lin).max()), 1e-12)
    return ra * cov / s, lin / s


def lmo(g, lo, hi):
    """argmin_s g's over the capped simplex: everything at lo, fill the rest cheapest-first."""
    n = len(g)
    s = np.full(n, float(lo))
    rest = 1.0 - n * lo
    for i in np.argsort(g, kind="stable"):
        if rest <= 0:
            break
        add = min(hi - lo, rest)
        s[i] += add
        rest -= add
    return s


def fw_gap(w, q, c, lo, hi):
    g = q @ w - c
    return float(g @ w - g @ lmo(g, lo, hi))


def proj(v, lo, hi):
    a, b = v.min() - hi - 1, v.max() - lo + 1
    for _ in range(200):
        t = 0.5 * (a + b)
        if np.clip(v - t, lo, hi).sum() > 1:
            a = t
        else:
            b = t
    return np.clip(v - 0.5 * (a + b), lo, hi)


def fista(q, c, lo, hi, tol=1e-13, iters=200000):
    L = float(np.linalg.eigvalsh(q).max())
    x = proj(np.full(len(c), 1 / len(c)), lo, hi)
    y, t = x.copy(), 1.0
    for k in range(iters):
        xn = proj(y - (q @ y - c) / L, lo, hi)
        tn = 0.5 * (1 + math.sqrt(1 + 4 * t * t))
        y = xn + (t - 1) / tn * (xn - x)
        x, t = xn, tn
        if k % 50 == 0 and fw_gap(x, q, c, lo, hi) < tol:
            break
    return x


def slsqp(q, c, lo, hi, ftol=1e-15, maxiter=500):
    n = len(c)
    return minimize(
        lambda w: float(0.5 * w @ q @ w - c @ w),
        proj(np.full(n, 1 / n), lo, hi),
        jac=lambda w: q @ w - c,
        method="SLSQP",
        bounds=[(lo, hi)] * n,
        constraints=[{"type": "eq", "fun": lambda w: w.sum() - 1.0, "jac": lambda w: np.ones(n)}],
        options={"maxiter": maxiter, "ftol": ftol},
    )


rows = []
for n_assets in (30, 100):
    for seed in (3, 11, 29):
        m = generate_market(
            SyntheticSpec(n_assets=n_assets, start="2019-01-02", end="2021-12-31", seed=seed)
        )
        rets = total_returns(m.bars, m.actions).drop(columns=[BENCHMARK_SYMBOL])
        for end in range(253, len(rets) + 1, 20):
            w = rets.iloc[end - 252 : end]
            usable = [s for s in w.columns if w[s].notna().sum() >= int(0.9 * 252)]
            w = w[usable].dropna()
            cov = covariance(w, method="ledoit_wolf").to_numpy()
            cov = 0.5 * (cov + cov.T)
            mu = w.mean().to_numpy() * 252
            n = len(usable)
            hi = max(0.4, 1.0 / n)
            for method, mu_, ra in (("min_var", None, 1.0), ("mean_var", mu, 5.0)):
                q, c = scaled(cov, mu_, ra)

                def f(x, q=q, c=c):
                    return float(0.5 * x @ q @ x - c @ x)

                ref = fista(q, c, 0.0, hi)
                r = slsqp(q, c, 0.0, hi)
                r12 = slsqp(q, c, 0.0, hi, ftol=1e-12)
                row = dict(
                    n=n_assets,
                    method=method,
                    status=int(r.status),
                    nit=int(r.nit),
                    gap=fw_gap(r.x, q, c, 0.0, hi),
                    ref_gap=fw_gap(ref, q, c, 0.0, hi),
                    dobj=f(r.x) - f(ref),
                    dw=float(np.abs(r.x - ref).max()),
                    feas=abs(float(r.x.sum()) - 1),
                    viol=float(max(0, -r.x.min(), r.x.max() - hi)),
                    s12=int(r12.status),
                    dw12=float(np.abs(r12.x - r.x).max()),
                    # residue: SLSQP weight where the reference optimum is at the 0 bound
                    resid=float(r.x[ref < 1e-12].max()) if (ref < 1e-12).any() else 0.0,
                    real=float(ref[ref >= 1e-12].min()),
                )
                # Non-converged points: early-stopped iterates and perturbed optima.
                for it in (3, 10, 30):
                    e = slsqp(q, c, 0.0, hi, maxiter=it)
                    if int(e.status) == 9:  # iteration limit: a genuinely unfinished point
                        row[f"gap_it{it}"] = fw_gap(proj(e.x, 0.0, hi), q, c, 0.0, hi)
                rng = np.random.default_rng(len(rows))
                for eps in (1e-6, 1e-5, 1e-4, 1e-3):
                    p = proj(ref + eps * rng.standard_normal(n), 0.0, hi)
                    row[f"gap_p{eps:g}"] = fw_gap(p, q, c, 0.0, hi)
                    row[f"dobj_p{eps:g}"] = f(p) - f(ref)
                rows.append(row)


def summ(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return "-"
    return f"min {min(xs):.2e} med {np.median(xs):.2e} max {max(xs):.2e} (n={len(xs)})"


print("platform:", sys.platform, "numpy", np.__version__)
groups = defaultdict(list)
for r in rows:
    groups[(r["n"], r["method"])].append(r)
for (n, meth), sub in sorted(groups.items()):
    print(
        f"\n== n={n} {meth}: status {dict(Counter(r['status'] for r in sub))}; "
        f"ftol=1e-12 status {dict(Counter(r['s12'] for r in sub))}"
    )
    for st in sorted({r["status"] for r in sub}):
        ss = [r for r in sub if r["status"] == st]
        print(f"  status {st}: FW gap        {summ([r['gap'] for r in ss])}")
        print(f"            f - f*(ref)   {summ([r['dobj'] for r in ss])}")
        print(f"            max|w - w*|   {summ([r['dw'] for r in ss])}")
        print(
            f"            |sum-1|, viol {max(r['feas'] for r in ss):.1e},"
            f" {max(r['viol'] for r in ss):.1e}"
        )
        print(f"            nit           {summ([r['nit'] for r in ss])}")
        print(f"            zero-bound residue {summ([r['resid'] for r in ss])}")
        print(f"            smallest w*>0      {summ([r['real'] for r in ss])}")
    print(f"  reference FW gap          {summ([r['ref_gap'] for r in sub])}")
    print(f"  ftol 1e-12 vs 1e-15 max|dw| {summ([r['dw12'] for r in sub])}")
    for k in (
        "gap_it3",
        "gap_it10",
        "gap_it30",
        "gap_p1e-06",
        "gap_p1e-05",
        "gap_p0.0001",
        "gap_p0.001",
    ):
        print(f"  non-converged {k:12s} {summ([r.get(k) for r in sub])}")
