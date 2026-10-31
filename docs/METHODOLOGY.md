# Research methodology

This document states exactly how QuantLab turns data into numbers. Code references are
the source of truth. Tests in `tests/golden` (`pytest -m golden`) pin definitions to
values computed by hand or published elsewhere; `tests/unit` covers the rest.

## Data

* **Bars** are stored raw (as traded). Each bar carries `available_at`, the instant it
  became knowable (session close, 16:00 New York, expressed in UTC).
* **Corporate actions**: splits (`value` = new shares per old share) and cash dividends
  (`value` = cash per share held at the ex-date open), both effective at the ex-date open.
  Optional `declared_date`, `record_date` and `pay_date` are kept for provenance and
  validated at ingestion; the engine and the adjuster act on the ex-date only.
* **Placement** (`quantlab.data.adjust.place_actions`): an action takes effect on the
  first bar *of its own symbol* dated on or after its ex-date. For clean data that is the
  ex-date. If the ex-date is a non-session day, or a session where the symbol printed no
  bar, the action applies at the symbol's next bar: nothing traded in between, so the
  holders and the price discontinuity are the same. Actions dated before a symbol's first
  bar or after its last bar are outside its history and are not applied (a warning).
* **Ordering**: actions placed on one bar are composed in ex-date order into one
  equivalent event per share held before the bar: `S = Π s_i` and `D = Σ (Π_{j<i} s_j) d_i`,
  where a split multiplies the running share count and a dividend pays `d_i` per share held
  at that point. On a *shared* ex-date the dividend comes first (per pre-split share). So
  a split on Saturday followed by a dividend on Sunday, quoted per post-split share, pays
  `s * d` per pre-split share at Monday's open.
* **Total return identity** used everywhere: `(S_t * C_t + D_t) / C_prev = 1 + r_t`, where
  `C_prev` is the symbol's previous bar. After a gap that bar is several sessions back, so
  the first bar after a gap carries the whole gap's return; a row without a bar has none.
  Compounded total returns therefore always equal the ratio of adjusted closes.
* **Adjustment** (`quantlab.data.adjust`): backward factor `f_t = C_t / (S_t C_t + D_t)`
  per bar with events. `C_t` is always a real post-event close, because events sit on bars
  of their own symbol. Adjusting *as of* row `a` uses only events placed on rows `<= a`.
  Adjusted returns are identical for every as-of date; adjusted price levels are not, so
  strategies receive the as-of view.
* **Ingestion gates** (`quantlab.data.quality`): a check with status `fail` blocks the
  dataset (`enforce_gates` raises `QualityGateError` with a per-check report). Gates are
  reserved for input that is malformed by construction, so that legitimate market data
  never blocks a dataset. The gates are:
  * Invalid bars: non-finite, non-positive, negative volume, duplicate rows, or OHLC
    inconsistent by more than half a tick (Reg NMS Rule 612 increments: $0.005 for prices
    of $1 or more, $0.00005 below). These checks existed before #10 and were reported but
    not enforced; they now block.
  * Duplicate corporate actions: an identical repeated row, or two splits on one ex-date.
    A same-amount regular and special dividend must be supplied as one row.
  * Split ratios equal to 1 or outside `[1/10000, 10000]`.
  * A dividend of at least the previous close.
  * A dividend of at least 10% of the previous close, less than half reflected in the
    price, that the price rose through or that is at least half the price (for example,
    cents entered as dollars).
  * A split bar whose gross total return is still 2x or 1/2x or worse (3x or 1/3x for a
    reverse split, which distressed issuers often do on −30% to −60% days): a wrong ratio,
    a wrong ex-date or a phantom split.

  Pay or record dates *before* the ex-date are allowed: for distributions of 25% or more
  the ex-date is the first business day after the payable date (FINRA Rule 11140(b)).

  The following are warnings only:
  * Missing sessions, a bar dated on a non-session day, stale prices.
  * An ex-date without a bar (the action applies at the next bar).
  * Actions the engine never applies: for a symbol without bars, or dated before its first
    or after its last bar. Full-history vendor action files with windowed bars, announced
    future actions and terminal distributions all look like this.
  * Impossible action dates (declared after the ex, record or pay date, or paid before the
    record date): they are provenance only.
  * A dividend of 2% or more less than half reflected in the price, below the gate.
  * A split bar moving more than 25%, below the gate.
  * Other moves above 50%.
  * A move within 2.5% of a k-for-1 or 1-for-k split signature (k ≥ 2) on a bar without a
    split.

  The last warning is the signature of an unrecorded split. It stays a warning because a
  genuine −50% or +100% day (a trial readout, a takeover, a one-tick move in a penny
  stock) produces the same prices: telling them apart needs a second source
  (reconciliation, #12). Spin-offs are not modelled (only splits and cash dividends), so a
  parent's ex-date drop shows up as a large move.
* **Calendar**: rule-based NYSE full-day holidays (verified against published session
  counts for 2021–2024). Early closes and one-off closures are not modelled.
* **Instrument identity** (`quantlab.data.identity`): research series are keyed by a
  permanent `instrument_id`, never by ticker. A ticker can be renamed (one security, two
  tickers) or reused (two securities, one ticker). A symbol history maps tickers to
  instruments with half-open validity intervals `[valid_from, valid_to)`. At any instant a
  ticker names at most one instrument, and an instrument has at most one ticker.
  Resolution is point-in-time: as of date `d` it uses only an interval with
  `valid_from <= d < valid_to`, never one that starts later (property-tested).
  `key_by_instrument` resolves each bar on its session date and each action on its
  ex-date, and puts the id in the `symbol` column. The engine, strategies, ML and
  analytics treat that column as an opaque key, so a rename is one continuous series (no
  phantom delisting and re-listing), a reused ticker is two series, and tickers are
  display labels. Bars or actions that no interval covers, and an instrument printing
  under two tickers on one session, are rejected rather than guessed. The instrument
  master records listing and delisting dates, a delisting reason and, when the vendor
  supplies one, a delisting return (unknown is `NaN`, never 0). Datasets without identity
  data use the legacy map (each ticker is its own instrument), which re-keys nothing and
  leaves their content hashes unchanged. A real symbol history is part of a version's
  content hash. Back-to-back intervals of one (instrument, ticker) pair are merged first,
  so a vendor file that splits a row at a name or exchange change hashes the same.
  Synthetic data carries `QLSYN…` ids and remains labelled synthetic. Conventions and
  limits:
  * An id identifies a *security* (a share class), not a company: BRK.A and BRK.B are two
    instruments.
  * A security trading under two tickers at once (when-issued alongside regular-way) is
    rejected; such feeds must be collapsed before ingestion.
  * Vendor end dates that are inclusive (CRSP style) must be converted to the next day
    for `valid_to`; otherwise the final day's bars are rejected as unmapped.
  * Many vendors file a security's *whole* history under its current ticker. Such data
    must be keyed by the vendor's permanent id directly, not resolved as of each bar's
    date: if the current ticker once belonged to another security, as-of resolution
    would silently assign the old bars to that security. This is the vendor adapter's
    responsibility (#11).
  * The instrument master (including delisting returns) is not yet part of the content
    hash. It will be once delisting returns affect backtests (#9 part B).
  *In the API jobs* (engine 1.3.0):
  * Dataset versions store their symbol history and instrument master. Synthetic versions
    always do; CSV uploads have none and use the legacy map.
  * Backtest, research and ML jobs key data by instrument and pass the master to the
    engine and to ML.
  * **Universe rule:** the tickers in an experiment's `universe` and `benchmark` are
    resolved to instruments **as of the backtest start**, never as of today. A start
    before the data (a holiday, say) means the first session, and with no start set the
    first session is used too.
    * A security renamed later stays in under its later ticker.
    * A ticker reused later by another issuer does not pull that issuer in.
    * A ticker that has never named anything by the start (a later listing) means the
      first instrument to take it within the backtest window, but only if it is that
      instrument's first-ever ticker. It joins when it lists, as in an unkeyed
      dataset. A security *renamed into* the ticker later was trading under another
      name at the start, so selecting it by its later name would be a today's-ticker
      bias; that ticker fails the job.
    * A ticker that named another security before the start but names nothing on it
      (retired, or in a gap before a reuse) fails the job instead of being guessed.

    All tickers each resolved instrument ever had are loaded, so its history is
    complete. The stored `meta.universe` and `meta.benchmark` are labelled with the
    tickers as named at the start, with `universe_instrument_ids` and
    `benchmark_instrument_id` beside them.
  * Results stay keyed by instrument for simulation, fingerprints and stored metrics.
    The trades, positions, target-weight, rejection and ML-prediction frames written for
    the API show the ticker valid on each row's date in `symbol`, with the permanent id
    in an added `instrument_id` column. Rows after a delisting show the last ticker.
    Final holdings and P&L contributions are labelled as of the last session, never with
    a later ticker, and `[id]` disambiguates two instruments that show the same ticker.
    `meta.universe` uses the same rule (two holders of a reused ticker in one run show
    as `T [id]`).
  * ML `exclude` names tickers (the benchmark index by default). On keyed data every
    instrument that ever held an excluded ticker is excluded.
  * The catalog's `delisted` flag is per instrument: a renamed security's old ticker is
    not marked delisted.
  * The catalog has one row per (ticker, instrument) in a version (migration
    `b5e1d7c3a924`, unique with `NULLS NOT DISTINCT`), so with ticker reuse the earlier
    holder keeps its own row, dates, delisting facts, `delisted` flag and sector.
    `InstrumentOut` exposes the instrument and delisting facts as optional fields.
    Ticker-keyed metadata (name, sector) is applied only to the ticker's holder on its
    last bar; metadata with an `instrument_id` column is matched by id. Downgrading the
    migration refuses while any version has a reused ticker's rows.
  * The survivorship check compares instrument ids (`universe_instrument_ids` against
    the catalog's delisted ids) when both sides carry them, and tickers otherwise. It
    never mixes the two.
* **Reconciliation** (`quantlab.data.reconcile`) compares a dataset with an independent
  second source, after keying both by instrument. A secondary with its own symbol history
  is keyed by it (`id_map` translates its id space, one-to-one only) and the two
  histories' tickers are compared. A secondary without one (a ticker-keyed CSV) is
  resolved through the primary's dated history, so renames and reused tickers map as the
  primary says. Inside the window both sources cover for an instrument, each discrepancy
  falls into one class:
  * `missing_bar`
  * `invalid_value`: a non-finite close or volume
  * `price_mismatch`: closes differ by more than `max(5 bp, half a tick)` ($0.005 at or
    above $1, $0.00005 below)
  * `adjusted_series`: the close ratio sits on two or more levels that step at the
    primary's ex-dates, the signature of an adjusted series compared with raw prices.
    Reported once per instrument; reconcile raw data. The rules:
    * A level needs a segment of at least 3 bars with positive closes.
    * The levels must end at 1, because back-adjustment leaves the latest prices raw.
      A final step to 1 across ex-dates in a too-short last segment is checked like
      the others.
    * Every step must be the factor the primary's actions imply, all in one
      orientation. Per ex-date with split `s` and cash `d` that factor is
      `s / (1 - d/c_prev)` or `s + d/c_ex`, both computed from the raw side's closes,
      or `s` alone for split-only adjustment.
    * The tolerance is the bar tolerance plus both segments' rounding (half a tick
      over price on each side), so a 10:1 split adjusted as 9:1, or a level 3% off
      around a split, is not explained.
    * For steps above about 10% the raw side must also show the jump, so a split
      applied the wrong way is not explained. A final step after the window (a vendor
      adjusted through a split after the last compared bar) has no bar to show the
      jump; its continuity is not checked, but its factor must match an action the
      primary carries after the window.

    Ten bad prints between two dividends are therefore price mismatches, not a level.
    Known limits:
    * A one-session segment between consecutive ex-dates defines no level, so its bars
      stay price mismatches even in a genuine adjusted series.
    * For dividend-sized steps the orientation cannot be checked against daily moves,
      so a series adjusted the wrong way for a dividend is still labelled
      `adjusted_series`. The class fails by default either way.
    * Secondary bars that equal the raw primary inside an adjusted segment are not
      mismatches and are not listed.
  * `scale_mismatch`: one constant close ratio other than 1 (pence vs pounds, a
    currency, a fixed offset), which is not an adjustment. A series adjusted only for
    an action outside the compared window also has one constant ratio and lands here.
    So does one whose steps inside the window are explained but whose last level is a
    constant other than 1 that no action in the primary explains (adjusted through a
    later split the primary lacks, or a tail scaled by some other cause): the window
    cannot tell those apart, and it is never called an adjusted series. Such a row lists
    every level in `ratios`, so it can show two or more. A genuine vendor that also
    adjusted for a *dividend* after the window lands here too: that factor needs raw
    closes around the ex-date, which the window does not have.

    For both ratio classes only bars on the ratio level are explained: a bar off it by
    more than the close tolerance (a bad print inside an adjusted series) is still a
    `price_mismatch`.
  * `volume_mismatch`: more than 5%
  * `action_missing`, `action_mismatch`: dividends within $0.0001; split ratios within
    1e-6 relative; ex-dates up to 3 sessions apart are one mismatch, not two missing
    actions. Pairing prefers an agreeing amount over a nearer date, so two dividends
    recorded a session late pair with their own amounts. When two diagnoses have the
    same number of issues this can move the blame: primary 0.50 on day 5 against
    secondary 0.52 on day 5 and 0.50 on day 7 reports the 0.50s as `ex_date differs`
    and the 0.52 as missing. Same-day actions of one kind are combined first, so regular plus special
    equals their total.
  * `listing_mismatch`: first or last bar dates differ (a stale secondary shows here)
  * `identity_mismatch`: an instrument in one source only, the two histories naming
    different tickers on a session, or a history-less secondary's ticker that the primary
    does not know on that date

  Tolerances and each class's severity are configurable. By default volume and listing
  differences warn and the rest fail, because vendors legitimately differ on
  consolidated vs primary-exchange volume. A report records its inputs (names, content
  hashes, whether a supplied version hash matches the data compared (a stored version's
  hash covers its symbol history and instrument master, so build its side with
  `Source.from_version`), provider
  descriptions, synthetic flags, coverage) and a UTC timestamp. Duplicate bars in either
  source are rejected rather than compared. Two known limits of a history-less secondary:
  * A vendor that maps a reused ticker to the *old* security shows up as price and volume
    mismatches, not as an `identity_mismatch`, because there is no history to compare.
  * A vendor that files a security's whole history under its current ticker needs a
    symbol history of its own to be compared before a rename.

  A report is stored per holder of the version, not beside the shared content directory:
  `.reports/<owner>/<version>/<name>.json`, where the API's owner is the version *row*
  id (#96). Two workspaces holding identical content share one version directory but
  never see each other's reports. Files left at the pre-#96 location
  (`<slug>/_reports/<version>/`) are not served. A report is never overwritten unless
  `replace=True`. Real cross-vendor reconciliation is unverified
  until a licensed second source exists (#11).
* **Dataset versions** are content-addressed (SHA-256 of a canonical serialization) and
  immutable.
* **Synthetic data** is generated by a seeded regime-switching factor model with sectors,
  splits, dividends, late listings and delistings. It is always labelled synthetic; any
  statistic computed on it says nothing about real markets.

## Backtest engine (`quantlab.backtest.engine`)

Per session `k`:

1. Corporate actions placed on this session (see *Placement* above), applied at the open
   as one equivalent event: dividend `D` on the shares held before the open, then split
   `S`. Pending orders decided before a split are restated in post-split shares.
2. Delistings. With an instrument master (`run_backtest(..., instruments=)`), a held
   security is closed on the first session after its `delisting_date` at
   `last close x (1 + delisting_return)` (a `delisting` ledger entry, which may be worth
   0), or at its last close when the return is unknown. From its delisting date it is not
   offered to the strategy, and corporate actions dated after it are not applied (the
   delisting return includes post-delisting distributions). A halt before the delisting
   does not trigger the missing-bars rule for a security the master delists. This is
   CRSP's convention, and realistic: a halted stock cannot be sold. It does mean the
   engine's handling during a halt depends on the later delisting, so positions and
   cash during the halt differ from a run without a master (pinned in
   `tests/unit/test_delisting_returns.py`). Decisions before the halt begins are
   identical. The same holds for any data gap in a security the master delists later,
   not only a gap that runs into the delisting: the missing-bars rule never sells it. A
   delisting dated on or after the last session of the window is not applied, and the
   position keeps its last mark. If the security traded on the last session, that mark
   is a real close (a study ending before its data does); this is counted as
   `meta.delistings.after_window_current` and is not a concern. If it did not, the mark
   is stale; this is counted as `after_window` and warned about. `BacktestConfig.performance_delisting_return`
   (default off) prices a performance delisting (bankruptcy, liquidation, noncompliance)
   whose return is unknown, for example at Shumway's (1997) -30% instead of the
   optimistic last close; `meta.delistings.with_fallback_return` counts those. Delisting
   fills are recorded as sells and count toward turnover, like the missing-bars rule.
   The integrity report's `delisting_exits` check warns when held delistings exited at
   the last close without a known return, were priced with the assumed fallback (it names
   the assumed return), or sit at a stale mark after the window. It is "not assessed" for
   a dataset without an instrument master.
   The delisting date is public on the day, so acting on the next session uses no
   future data; the delisting return (final value relative to the last close, as in
   CRSP's DLRET) is booked at that point, which is standard practice even though the
   final value may be settled later. Otherwise (no master, or a security the master does
   not delist), a held symbol that has printed no bar for `delist_after_missing`
   consecutive sessions (default 5) is liquidated at its last close. That rule uses only
   past data, but recovery values in real delistings are usually lower.
   `meta.delistings` counts `with_return`, `return_unknown` and `by_missing_bars`.
3. Orders decided at the previous rebalance close fill at `open[k]` (default) or
   `close[k]`, adjusted by slippage (`price * (1 ± slippage_bps/1e4)`). Commission is
   `max(commission_min, notional * commission_bps/1e4)`.
4. Sells execute before buys. If the cash left after sells cannot fund all buys at the
   fill prices, every buy is scaled by the same factor (then rounded to whole shares
   unless fractional shares are enabled) and flagged `partial`, so no symbol is favoured
   by sort order. Orders without a price on the fill date are rejected (`no_price`).
5. Mark-to-market at `close[k]` (last known close for symbols with a gap).
6. On rebalance dates (last session of the period, plus the first session) the strategy
   sees bars dated `<= k` only and returns target weights. Weights are clipped to
   `max_weight`, scaled to `max_gross_exposure`, and converted to share targets using
   `close[k]` and equity at `close[k]`.

Symbols that only list after the backtest window are excluded (recorded in
`meta.not_listed_in_window`). `meta.corporate_actions` counts the universe's actions by
placement: `on_ex_date`, `applied_next_session` (ex-date without a bar for the symbol,
applied at its next bar) and `outside_history` (before the symbol's first bar or after
its last bar; not applied).

No leverage or shorting is modelled. Cash earns nothing (the risk-free rate is used only in
risk-adjusted ratios).

**Dividends: strategy vs benchmark.** The two use different reinvestment conventions:
* The ledger credits dividends as cash, and that cash is only reinvested when the
  strategy next rebalances.
* The benchmark series is the compounded total return of the benchmark's own bars, so its
  dividends are reinvested at the ex-date close.

So a buy-and-hold of the benchmark itself trails the benchmark by exactly the
reinvestment effect. For example, $20 of dividend cash that misses a later +10% move costs
$2 (pinned in `tests/unit/test_research_correctness.py`). Excess returns and tracking error
include this cash drag, which is real for a strategy that does not reinvest.

**Invariants verified by tests**: cash equals the sum of transaction cash flows (ledger
replay); equity equals cash plus marked positions every day; cash never negative;
positions never negative; deterministic fingerprints for identical inputs; truncating
data after a decision date leaves that decision unchanged (no look-ahead).

**Reproduction.** The input hash covers the dataset version, strategy, parameters,
config and `ENGINE_VERSION`. Re-running an experiment from an older engine therefore has
different inputs even when nothing else changed. The reproducibility comparison lists
each differing input (`input_differences`, for example `engine_version 1.2.0 → 1.3.0`).
The integrity report only uses same-input re-runs to verify or fail reproducibility; it
names changed-input re-runs and leaves the check "not assessed" if they are the only ones.

## Performance statistics (`quantlab.analytics.performance`)

| Statistic | Definition |
|---|---|
| Simple return | `r_t = E_t / E_{t-1} - 1` |
| Total return | `E_T / E_0 - 1` |
| CAGR | `(E_T / E_0)^(252/N) - 1`, N return periods; `-1` if wealth reaches 0; undefined if negative |
| Volatility | `std(r, ddof=1) * sqrt(252)` |
| Risk-free per period | `(1 + rf)^(1/252) - 1` |
| Sharpe | `mean(r - rf_p) / std(r - rf_p) * sqrt(252)` |
| Sortino | `mean(r - rf_p) / sqrt(mean(min(r - rf_p, 0)^2)) * sqrt(252)` |
| Max drawdown | `min(E / cummax(E) - 1)` |
| Calmar | `CAGR / abs(max drawdown)` |
| Beta | `Cov(r_p, r_b) / Var(r_b)` (sample) |
| Alpha | Jensen, annualized arithmetic: `252 * [mean(r_p - rf_p) - beta * mean(r_b - rf_p)]` |
| Tracking error | `std(r_p - r_b) * sqrt(252)` |
| Information ratio | `252 * mean(r_p - r_b) / TE` |
| Turnover | one-way: `sum(traded notional) / 2 / mean(equity)`, annualized |
| Gross ("before costs") | equity plus cumulative commissions and slippage paid; an approximation that ignores compounding of the saved costs |

Undefined statistics (zero variance, fewer than two observations, no drawdown, no losing
trades) are reported as `null` with a reason, never as `0`. This includes beta, whose
benchmark dispersion uses the same per-period rule. "Zero variance" means a
per-period standard deviation at or below `1e-12`, so floating-point residue is never
annualized into a statistic: an exact index tracker has tracking error `0` and an
undefined information ratio. When there are fewer than two observations or the initial
wealth is not positive, *every* metric (and every benchmark metric) is undefined with
that reason. Rolling volatility and Sharpe (`analytics.summary.rolling_metrics`) use the
same risk-free and annualization conventions, and the same zero-dispersion rule per
window. A walk-forward whose every out-of-sample fold fails returns
`error: "every out-of-sample fold failed"` with all OOS statistics undefined.

Trade statistics use FIFO lot matching with commissions allocated per share; open lots
are restated at each split (shares times ratio, cost divided by ratio). Sells that match
no open lot are not counted as round trips.

Parameter selection (walk-forward in-sample scoring) and the sensitivity grid score each
run from the session before its first holding, so strategies with long look-backs are not
penalised for warm-up time spent in cash. Reported experiment statistics still cover the
whole requested window, as that is what the account experienced. The selected parameter
set is always one of the grid points; parameter sets are compared with every default
filled in.

**Probabilistic and deflated Sharpe** (`quantlab.research.stats`, Bailey and López de
Prado). The statistic is the per-period Sharpe of excess returns,
`mean(r - rf_p) / std(r - rf_p)`, which is the annualized Sharpe divided by `sqrt(252)`.
`PSR(SR*) = Φ((SR - SR*) sqrt(n - 1) / sqrt(1 - γ3 SR + (γ4 - 1)/4 SR²))`, where γ3 and
γ4 are the sample skewness and (non-excess) kurtosis. The DSR is the PSR against
`SR* = E[max SR]` of the N grid trials under the null. N counts every configuration
tried, including those whose Sharpe is undefined (no dispersion), so N is not
understated; the variance comes from the defined Sharpes. Every trial Sharpe *and* the
selected strategy's PSR/DSR use the same inputs: each run's invested window (as above)
and the configured risk-free rate. Before #14 the selected strategy used its full window,
including warm-up cash, with no risk-free rate, which overstated both probabilities.
`psr_vs_zero` is the probability that the true excess Sharpe is positive. The output
records `observations` and `selected_sharpe_per_period`, so the inputs can be audited.

Regime analysis classifies each day using trailing benchmark volatility and trend, with
volatility cut points taken from expanding quantiles, so no day's regime depends on later
data. Bootstrap Sharpe intervals use the same risk-free convention as the reported
Sharpe and, like it, the whole requested window (warm-up cash included). They describe
the account as experienced. The deflated Sharpe instead uses the invested window,
because it assesses parameter *selection* (see below), so the two can differ for
strategies with long look-backs.

## Risk (`quantlab.analytics.risk`)

* Historical VaR at confidence `c`: `-quantile(r, 1-c)` (lower interpolation); ES is the
  mean loss at or beyond that quantile. Both are one-day, positive fractions of value.
* Gaussian VaR/ES and Cornish-Fisher VaR are provided for comparison; their assumptions
  (normality, moment stability) are frequently violated by financial returns.
* Risk contributions: `MRC = Σw / σ_p`, `CRC = w * MRC`, which sum to `σ_p`.
* Monte Carlo uses a stationary bootstrap (Politis and Romano, 1994) with mean block length
  10, seeded for reproducibility. It resamples history and so cannot produce shocks
  larger than those observed.

## Factor exposures (`quantlab.analytics.factors`)

Daily strategy returns are regressed by OLS on a market factor (the benchmark's total
return) and, for each sector with at least three members, the equal-weighted sector return
minus the market. Standard errors are Newey-West (HAC) with
`floor(4 * (T/100)^(2/9))` lags. Alpha is the intercept times 252. The regression is
descriptive: it uses the whole sample.

## Optimizers (`quantlab.optimize`)

Long-only, fully invested. Minimum-variance and mean-variance use SLSQP on a rescaled
objective; risk parity uses Spinu's convex formulation. Every result is validated (finite,
sums to 1, within bounds) or an error is raised; infeasible bounds are rejected before
solving.

## ML features (`quantlab.ml.pipeline`)
* Features at `t` use only bars dated on or before `t`. Momentum and reversal are ratios
  of fully adjusted closes; volatility uses total returns; `dist_high_252` measures the
  distance to the 252-bar high; `volume_z_21` is the z-score of log volume.
* **Windows count the instrument's own bars, not calendar sessions** (#120, pipeline
  version 2). They are trade-time measures: `mom_21` is the return over the last 21 bars
  and `rev_5` over the last 5, however many sessions those took. A 21-bar window ending just after a halt spans the bars before the halt,
  so it covers more calendar sessions. The first bar after a gap carries the return over
  the whole gap. Rows exist only on sessions with a bar, so a halted security leaves the
  panel for the halt's sessions alone. Before version 2, any window containing a halt day
  was NaN for the window's length, which dropped a recently halted security, a selection
  correlated with distress, for up to 252 sessions.
* **Staleness is described, not excluded.** Trade-time windows can reach far back in
  calendar time, after a halt or for a thinly traded name. Two model inputs say how far
  (s is a bar's position on the session calendar):
  * `bars_span_21 = (s[t] − s[t−21]) / 21`: the calendar span of `mom_21`'s 21 returns,
    per return. It is 1 for a bar every session and 2 for every other session.
  * `log1p_max_gap_252 = log(1 + max_gap_252)`. `max_gap_252` is the largest number of
    sessions without a bar between consecutive bars over the last 252 returns (bars
    t−252 … t, so including the gap between t−252 and t−251). The raw count is unbounded,
    so the model sees its log; the panel keeps the raw value as `max_gap_252`, which is
    not a model input.

  A halt shows as one long gap (a 40-session halt gives `max_gap_252 = 40`). Thin trading
  shows many short ones: every other session gives `bars_span_21 = 2` with
  `max_gap_252 = 1`, and random 50% trading gives about 2.05 with a maximum gap near 9.
  An earlier rule excluded a feature whose oldest bar lay more than 2·L sessions back. It
  was retracted because it selects against illiquid names: a sparsely traded name is not
  halted, but its gaps add up over a window. Measured by
  `benchmarks/ml_liquidity_selection.py`, the share of each name's candidate rows kept:
  * Market: synthetic, `generate_market` with 30 assets over 2018-01-02 to 2022-12-30,
    seed 11, `split_probability` 0.002.
  * Thinning: random keep with `numpy.random.default_rng(1)`; the halt covers sessions
    600–639.
  * Sampling: every session (`sample_every = 1`).
  * Candidate row: a date the panel samples (at least 10 tradable names) on which the
    name has a bar, complete features apart from staleness, and a defined label.


  | name | sessions traded | 2·L cap (retracted) | covariates (pipeline 2) |
  |---|---|---|---|
  | random 80% | 81% | 100% | 100% |
  | random 60% | 59% | 67% | 100% |
  | random 50% | 50% | 10% | 100% |
  | random 45% | 46% | 0% (drops out) | 100% |
  | every other session | 50% | 100% (exactly at the bound) | 100% |
  | 40-session halt | 97% | 98% | 100% |
  | dense, never halted | 100% | 100% | 100% |

  The residual liquidity effect is the warm-up. A name enters once it has 252 bars of its
  own, so at 50% trading it needs about two years of calendar history instead of one.
* **No trading vs flat volume.** A row whose last 21 bars have zero total volume (a
  quote that never trades) is excluded. `panel.excluded_rows.untraded_21` counts the rows
  that would otherwise have been kept, on dates that make the panel.
  `dates_below_10_after_untraded` counts dates skipped because that removal left fewer
  than ten names.
  Constant non-zero volume (21-bar log-volume standard deviation at or below 1e-12)
  deviates from its mean by exactly zero, so `volume_z_21` is 0, not 0/0. Then
  `volume_flat_21 = 1`, which is a **model input**, so a model can tell "no dispersion"
  from "average volume". Results report the count as `panel.volume_flat_rows`.
* `ML_PIPELINE_VERSION` is recorded in every result (`pipeline_version`) and changes
  whenever the same dataset and config could produce a different panel. The API stamps
  it into the run's config and input hash, and a worker running a different version
  refuses the run with a clear error (#143). During a mixed-version deploy a run then
  fails instead of storing a hash that describes another panel.

## Known limitations

* No survivorship-free real data source is configured; the synthetic generator includes
  delistings to demonstrate the effect.
* Without a delisting return, a delisting exits at the last close, which is optimistic
  for bankruptcies. The same applies to ML labels: an asset that delists inside the label
  horizon exits at its last close. With an instrument master
  (`quantlab.ml.pipeline.build_panel(..., instruments=)`), the exit is the last close on
  or before the delisting date, the security is not sampled after it, and a known
  delisting return is compounded only onto labels whose horizon reaches the first session
  after the delisting date, so no label includes a return that is not public by its
  `label_available_at`. No configured vendor documents
  delisting returns (docs/MARKET_DATA_PROVIDERS.md), so until one is licensed they are
  unknown (NaN) and the optimistic exit applies.
* Dividends are credited at the ex-date rather than the pay date.
* No borrowing, shorting, margin, taxes or market impact beyond linear slippage.
* Daily bars only.
