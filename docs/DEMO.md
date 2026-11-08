# Demo walkthrough

A walkthrough of QuantLab's main features on synthetic data. It needs no cloud service,
credentials or paid API. Setup takes a few minutes; the tour about ten.

**Everything here is synthetic data.** It shows how the platform works, not that any
strategy makes money. Say so when you present it; the application says it on every screen.

## 1. Start the stack and seed a workspace

```bash
make setup
make app-up                      # on a Docker VM under 5 GiB: QUANTLAB_WORKERS=1 make app-up
uv run python scripts/demo_seed.py
```

`scripts/demo_seed.py` uses the public API, as the browser does, and creates:
* the account `demo@example.com` (password `Demo-workspace-2026`);
* a 100-asset synthetic dataset (2018–2023, seed 7);
* three experiments: momentum, equal weight and minimum variance;
* the validation suite on the momentum run, and a reproduction of it;
* a portfolio with two purchases;
* portfolio construction on all 100 symbols at the optimize API's defaults.

A fresh run took 32 s on a one-worker stack on the development Mac. It prints the pages to open. From a linked git worktree, pass
`--url http://localhost:<web port>` (from `make stack-env`). The script refuses any
non-local URL.

Sign in at http://localhost:3000 with the account above.

## 2. The tour

Each stop shows one engineering idea. The line in **bold** is the point to make.

1. **Market data** → the dataset version.
   * Raw bars and a total-return-adjusted chart.
   * The instrument list, with delisted names marked.
   * 23 quality checks, and the version's SHA-256.
   * **A dataset version is immutable and content-addressed; every experiment names the
     exact one it used.**
2. **The momentum experiment.**
   * The evidence panel: dataset, engine version, input hash, output fingerprint.
   * Then the equity curve against the benchmark, the performance table (with "before
     costs") and tail risk.
   * **Fills happen at the next session's open, after costs; a statistic that is undefined
     shows a dash and a reason, never 0.**
3. **Out-of-sample validation**, on the same page.
   * Walk-forward with parameters chosen in-sample, bootstrap intervals and the deflated
     Sharpe ratio.
   * The cost sweep, with the multiple of costs at which the edge vanishes.
   * The regime table.
   * **The suite is designed to argue against the result.**
4. **The research integrity report**, on the same page.
   * Expect warnings: the data is synthetic, the out-of-sample Sharpe is lower, and the
     Sharpe interval includes zero.
   * The look-ahead check is "verified".
   * **The report states what the evidence doesn't support.**
5. **Ask about this experiment.** Choose "Is the result robust out-of-sample?".
   * Each line of the answer cites a computed value, listed under it.
   * **The answer is checked against those values before it is shown, and works without an
     LLM key.**
6. **The reproduction** (Experiments → "… (reproduction)"). It links to its original.
   * Its integrity report says the outputs are byte-identical.
   * **Same inputs, same engine, same bytes.**
7. **Compare**: select the three experiments.
   * Growth of $1 on one scale, with the statistics side by side.
   * The picker shows each run's time and input hash: runs with the same hash are the
     same configuration.
   * **The page warns that the best of many variants is inflated by chance.**
8. **Construction**: open the `optimize?run=…` link the script printed. That is the seeded
   run on all 100 symbols with a 40% cap.
   * Equal weight, minimum variance, mean-variance and risk parity on the same history.
   * **Mean-variance at these settings used to fail. The solver stopped at the optimum
     without recognizing it. A failed solve is now accepted only with an optimality
     certificate (ENGINEERING_CASE_STUDY.md §6).**
9. **The portfolio** "Demo book": the ledger, holdings, time-weighted return and risk.
   * **The ledger is append-only. A trade that would overdraw cash or sell more than is
     held is refused, and nothing is stored.**
10. **ML research**: train with the defaults (about a minute).
    * The models are compared with a majority-class baseline and a 12-1 momentum rank on
      purged chronological folds.
    * Expect AUCs near 0.5.
    * **No skill on synthetic data is the honest answer, and the page explains how to read
      it.**
11. **Open report**, on the experiment page: a standalone HTML report with the same
    provenance and integrity table.

## 3. For a technical interview

A suggested order, in 15–20 minutes:
1. **The README's "problem it addresses"** (1 minute): why backtests go wrong quietly.
2. **The architecture diagram** (2 minutes):
   * Postgres holds job truth; Redis only delivers.
   * Routes never compute; workers do.
3. **The tour's steps 1–4** (5 minutes): the result, and the machinery that challenges it.
4. **One deep dive** from ENGINEERING_CASE_STUDY.md, with the test that proves it open in
   the editor:
   * the 50% fabricated loss (`test_corporate_action_without_a_bar_on_its_ex_date_is_applied_next_session`);
   * or the optimizer certificate (`tests/unit/test_optimizer_certificate.py`).
5. **Verification** (3 minutes): `docs/TEST_MATRIX.md`, and a mutation run:
   ```bash
   uv run python -m tests.oracle.mutate M4
   ```
   M4 makes delistings happen one session early. The run takes a minute or two and prints
   whether each suite caught it.
6. **Limitations** (1 minute): synthetic data only, not publicly deployed, measured on one
   machine.

Questions to prepare for are in [INTERVIEW_GUIDE.md](INTERVIEW_GUIDE.md).

## Reset
`make down` stops the stack and keeps its data. To start over from an empty workspace,
register another account, or pass `--email` to the seed script.
