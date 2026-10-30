# Project specification

QuantLab is a reproducible quantitative research and portfolio-risk experimentation
platform. The authoritative full brief is the original engineering directive; this file
records the scope commitments that guide prioritization.

## Primary user journey (acceptance target)
1. Sign in. 2. Create a workspace. 3. Choose a market-data universe (dataset version).
4. Create a strategy with validated parameters. 5. Configure costs, capital, constraints
and benchmark. 6. Start an asynchronous backtest. 7. Watch progress. 8. Results persist.
9. Explore returns, holdings, trades, drawdowns and risk. 10. Compare experiments.
11. Run out-of-sample validation. 12. Generate a Research Integrity Report.
13. Ask the assistant for a grounded explanation. 14. Export a report. 15. Reproduce from
the saved configuration and dataset version.

Steps 1–10 and 15 are implemented and verified end-to-end (Playwright). Steps 11–14 are
next (ROADMAP.md).

## Non-goals
Live brokerage, HFT matching, unvalidated "AI trading", microservices for their own sake.

## Principles
Financial correctness over features; no look-ahead; undefined statistics are explicit;
synthetic data is always labelled; every number is traceable to versioned inputs.
