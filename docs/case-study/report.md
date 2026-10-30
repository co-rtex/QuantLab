# Momentum 12-1, top 20

Generated 2026-10-30 20:07 UTC by QuantLab from persisted experiment results.

> **Synthetic data.** These results describe a simulated market, not real observations.

## Overview

* Strategy: `momentum` with parameters `{'skip': 21, 'top_n': 20, 'lookback': 252}`
* Sample: 2015-01-02 to 2023-12-29 (2265 sessions), 200 symbols
* Benchmark: SYNIDX
* Execution: next_open fills, monthly rebalance, 5.0 bps commission, 10.0 bps slippage
* Dataset version: `cffdee8b64476209a3ee0f59c1653c08d8d952124ca54196a58ec873737e3e7a` (synthetic)
* Engine 1.1.0, code unknown, inputs `3d738dd9fea8d1df5d1ef7742852eaecbe876bfc55d38b90307f2eba4043ede5`, output fingerprint `65fb40d834df89cae1184490f2c766f94f30537fe432d12f7f4044e4d9d40bd7`

## Performance

| Measure | Strategy | Before costs | Benchmark |
|---|---|---|---|
| Total return | 33.5% | 44.9% | 85.0% |
| CAGR | 3.27% | 4.21% | 7.08% |
| Volatility | 20.0% | 19.3% | 18.6% |
| Sharpe ratio | 0.26 | 0.31 | 0.46 |
| Sortino ratio | 0.37 | 0.44 | 0.65 |
| Max drawdown | -37.0% | -35.1% | -30.5% |
| Calmar ratio | 0.09 | 0.12 | 0.23 |

## Risk

* One-day historical VaR 95%: 1.95%; expected shortfall 2.93%
* Annual turnover 3.74x; total costs $113,457

| Drawdown peak | Depth | Sessions | Recovered |
|---|---|---|---|
| 2015-03-24 | -37.0% | 808 | 2018-06-07 |
| 2019-12-03 | -24.1% | 610 | 2022-05-05 |
| 2023-02-23 | -23.9% | 214 | not yet |
| 2018-06-15 | -20.9% | 324 | 2019-09-27 |
| 2022-05-25 | -6.2% | 12 | 2022-06-13 |

## Research integrity

| Check | Status | Finding |
|---|---|---|
| Dataset source and version | warning | Exact dataset version recorded, but the data is synthetic. Results describe the generator, not real markets. |
| Data quality checks | verified | All automated quality checks passed. |
| Universe definition and survivorship | verified | Universe of 200 symbols recorded, including 12 that were delisted in-sample. |
| Execution timing | verified | Signals use data up to the decision close; fills occur at the next session's open (engine v1.1.0). |
| Trading costs | verified | Costs modelled: 5 bps commission and 10 bps slippage per trade. Cost sweep: the strategy underperforms the benchmark even with zero costs. |
| Look-ahead leakage | verified | Signals on 4 sampled dates were identical when all later data was removed. |
| Out-of-sample evaluation | verified | 7 walk-forward folds with parameters chosen in-sample. Mean Sharpe in-sample 0.55, out-of-sample 0.35. |
| Parameter stability | verified | 6 parameter sets evaluated. 83% have a positive Sharpe ratio. |
| Statistical uncertainty | warning | 95% bootstrap interval for the Sharpe ratio: -0.37 to 0.94. The interval includes zero; skill is not distinguishable from luck. Annualized excess return versus the benchmark: -7.6% to 1.3%. |
| Selection bias from multiple trials | warning | Deflated Sharpe ratio 0.63 after 6 parameter trials (3 experiments on this dataset version in the workspace). Below 0.95, the best result is plausibly a product of search. |
| Benchmark comparison | verified | Compared against SYNIDX on a total-return basis. |
| Sample length | verified | 9.0 years of daily data traded. |
| Reproducibility | verified | 1 independent re-run(s) produced byte-identical results. |

## Conclusions

* Over the sample the strategy underperformed SYNIDX by 51.4% in total return, net of modelled costs.
* Walk-forward mean Sharpe was 0.55 in-sample and 0.35 out-of-sample across 7 folds.
* The 95% bootstrap interval for the Sharpe ratio is -0.37 to 0.94; it includes zero, so the result is not statistically distinguishable from no skill.
* Open integrity concerns: dataset source and version; statistical uncertainty; selection bias from multiple trials.

## Limitations

This report checks documentation, methodology and statistical uncertainty. It does not establish that the strategy will be profitable, and nothing in it is investment advice.
See docs/METHODOLOGY.md for definitions and known modelling limitations.

## Reproduce

```
POST /api/v1/experiments/922a7c21-6ca4-4e75-94eb-3c0ec649e2e8/reproduce
```
