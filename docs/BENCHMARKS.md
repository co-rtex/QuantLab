# Benchmarks

No formal benchmark run has been recorded yet; the harness in `benchmarks/` is on the
roadmap. Until then the only figures are informal single runs, listed so they are not
mistaken for measurements.

## Informal observations (not a benchmark)

* Machine: Apple Silicon laptop, 8 cores, 8 GB RAM, macOS 26.6, Python 3.12.16.
* Synthetic market, 100 assets, 2018-01-02 to 2023-12-29, trading from 2019-01-02, monthly
  rebalance: each of the nine strategies completed in 0.16–0.59 s of engine time in a
  single warm run (2026-10-30). Target for comparison: < 60 s for 100 assets × 5 years.
