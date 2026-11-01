# #253: SLSQP mode 8 at the optimize API's defaults (work in progress, no PR)

Synthetic data only. Reproduced on main 7dff629 under `scripts/fullstack-lock.sh`, on
macOS (Accelerate) and in the Linux API image (OpenBLAS). Output: `repro253-output.txt`.

## Reproduction
- **n = 100, mean_variance at the API defaults:** mode 8 ("Positive directional
  derivative for linesearch") in 77 of 78 windows on macOS and 78 of 78 on Linux.
- **n = 30:** 0 of 78 on macOS, 3 of 78 on Linux.
- **min_variance:** 0 failures at either n on both platforms.

## KKT residuals
The residual is for the scaled problem `_solve_qp` solves: the minimum over λ of the worst
violation, with a bound tolerance of 1e-9. Points SLSQP reports as failed sit in the same
range as successful solves:
- **Successful solves:** at most 2.1e-8.
- **Failed points:** at most 7.5e-8, median about 4e-10, with |Σw−1| ≤ 7.5e-14.

So mode 8 here is SLSQP stalling at the optimum.

## Backtests
min_variance in backtests had 0 failures in 4,980 solves on both platforms:
- the A/B grid's min_variance runs: 2,700 solves;
- default params, weekly: 390;
- default params, daily: 1,890.

## Decided with the lead
- On SLSQP failure, accept the point if it is feasible and its KKT residual is ≤ ε_KKT. Otherwise raise as today.
- Don't loosen ftol: that would change min_variance outputs and the #227 dust gap.
- Apply it uniformly (strategy and API).
- **Engine 1.8.0 even with 0 A/B changes,** because a backtest whose solve hits mode 8 would now trade instead of keeping its holdings. The PR must state that the A/B grid has 0 such solves.
- Add a mutation, for example ε_KKT × 1e6 accepting a perturbed point.

## Not done
- **ε_KKT is not chosen.** The success and failure residuals overlap, so it must sit above both. Measure residuals at clearly non-optimal points (perturbed optima, early-stopped iterates such as `maxiter` 5 or 20) and pick ε_KKT in the gap between converged and non-converged points, with margin.
- The KKT check in `allocators._solve_qp`, the tests:
  - n = 100 at the API defaults succeeds;
  - a perturbed point is rejected;
  - a forced failure still gives a per-method error.
- Mutation M36, the attributed A/B, the 1.8.0 bump and METHODOLOGY.
