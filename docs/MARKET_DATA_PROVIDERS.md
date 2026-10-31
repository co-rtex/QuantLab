# Market-data providers: evaluation and architecture

_Researched 2026-10-30 by Agent 3 for #11 and #12. No account was opened and no service was
purchased: everything below comes from public documentation, cited inline. **Nothing here
has been verified against live data.** Re-check each field and each price before buying._

## What QuantLab needs from a vendor

Credible historical research on US equities requires:

1. **Survivorship-free coverage:** delisted securities with full price history.
2. **A permanent instrument identifier** that survives renames, plus a **point-in-time
   ticker history**, so data can be keyed by `instrument_id` (#9).
3. **Corporate actions with dates:** splits, and cash dividends with declaration, ex,
   record and pay dates (#10). Delisting events with a reason, ideally a delisting return.
4. **Raw (unadjusted) prices.** QuantLab adjusts point-in-time itself, so vendor-adjusted
   series alone are not enough.
5. **A licence** that permits storing data locally for research and, if QuantLab is ever
   offered to other users, permits that use. Redistribution is a separate question.
6. **A documented API** with pagination and rate limits we can implement and test against
   fixtures.

## Candidates

| | Massive (formerly Polygon.io) | Sharadar (via Nasdaq Data Link) | Tiingo | EODHD |
|---|---|---|---|---|
| Delisted securities | `active=false` on `/v3/reference/tickers`, `delisted_utc` = last trade date (optional on some records) [1][2] | Yes, survivorship-free; `isdelisted` [5][6] | Unclear from docs; anecdotal yes [7] | `delisted=1` symbol list; delisted before 2018: EOD prices only [8] |
| Permanent identifier | `composite_figi`, `share_class_figi` (OpenFIGI); vendor recommends share-class FIGI [1][2] | `permaticker` [6] | Not found in docs | None documented; reused tickers get an `_old` suffix [8] |
| Ticker history | `/vX/reference/tickers/{id}/events`, `ticker_change` events since 2003-09-10 (vendor-stated, unverified); **experimental** [3] | ACTIONS table ticker changes [6]; TICKERS reportedly holds only the current ticker (unverified community report [9]) | Not documented | Separate symbol-rename API [10] |
| Point-in-time reference | `date` parameter on the tickers endpoint [1] | Via ACTIONS chain | No | No |
| Dividends with dates | `/stocks/v1/dividends`: `declaration_date`, `ex_dividend_date`, `record_date`, `pay_date`, `cash_amount`, `distribution_type` (recurring, special, ...), since 2000-01-15 (vendor-stated, unverified) [4] | ACTIONS covers dividends, splits, spin-offs, acquisitions, delist reasons [6] | `divCash` on ex-date only [7] | Declaration/record/payment dates only for "major US tickers", JSON only [8] |
| Delisting reason or return | No reason or return field documented | Delist reasons in ACTIONS [6]; return not documented | No | No |
| Pagination | `next_url` cursor; `limit` max 1000 (tickers), 5000 (dividends) [1][4] | Bulk export or table API | Per-ticker | Per-ticker |
| History depth by plan | Basic: 2 years; paid plans: full [1][3][4] | 20+ years [5] | | |
| Indicative price | Individual non-professional plans reported at $0 to $199/month; business use and redistribution are negotiated separately (third-party reports, conflicting) [11] | Professional users must buy through Nasdaq Data Link [5] | | |

Sources:
[1] https://massive.com/docs/rest/stocks/tickers/all-tickers.md ·
[2] https://massive.com/knowledge-base/article/does-massive-have-a-downloadable-security-master ·
[3] https://massive.com/docs/rest/stocks/corporate-actions/ticker-events ·
[4] https://massive.com/docs/rest/stocks/corporate-actions/dividends.md ·
[5] https://www.quantrocket.com/pricing/data/sharadar ·
[6] https://sharadar.com/sample ·
[7] https://www.tiingo.com/documentation/end-of-day ·
[8] https://eodhd.com/financial-apis/api-splits-dividends and https://eodhd.com/financial-apis/delisted-stock-companies-data ·
[9] https://github.com/ZhaorongDai/quantlab/issues/229 (an unrelated project's issue: weak evidence, not relied on) ·
[10] https://eodhd.com/financial-apis/us-stock-symbol-rename-history-api ·
[11] https://apicostcalc.com/blog/polygon-massive-rebrand-api-pricing.html (third party; confirm on massive.com/pricing)

## Assessment

* **Sharadar** fits QuantLab's model most directly: a survivorship-free security master
  with a permanent id, and one actions table holding ticker changes, delisting reasons,
  splits and dividends. Its exact ACTIONS column schema could not be confirmed from a
  primary public page (the documentation is rendered client-side), so no fixture should
  claim to follow it until we see the schema.
* **Massive** has the best *publicly documented* API for our needs: FIGI identifiers,
  point-in-time reference queries, dividends with all four dates and a distribution type,
  and cursor pagination. Weak points: the ticker-events endpoint is experimental; no
  delisting reason or return is documented; history is limited on the free plan.
* **Tiingo** and **EODHD** lack a documented permanent id or point-in-time ticker history,
  so they would need a separate identity source. EODHD's dated dividends cover only major
  tickers.

## Recommendation (needs the user's decision: licence and spend)

1. **Build the adapter contract against Massive's documented schema first.** Fixtures can
   be synthetic but schema-faithful, labelled synthetic, and checked field by field
   against [1], [3] and [4]. Use `share_class_figi` as the vendor's permanent id, mapped
   into QuantLab's `instrument_id`.
2. **Treat Sharadar as the preferred production source** for survivorship-free research,
   if the user licenses it. Add its adapter once its schema can be confirmed from the
   vendor's documentation or a licensed sample.
3. **Use the second vendor as the reconciliation source (#12).** Two independent vendors
   are the only way to tell an unrecorded split from a genuine −50% day (see the
   `split_like_returns` warning in METHODOLOGY).
4. **Delisting returns** are not documented by either API. CRSP (via WRDS, academic
   licence) is the standard source. Without it, QuantLab must keep labelling
   last-close-exit returns as optimistic (METHODOLOGY, known limitations).

## Massive endpoints used by the adapter (schema as documented)

| Purpose | Endpoint | Fields mapped | Notes |
|---|---|---|---|
| Daily bars | `GET /v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}?adjusted=false&sort=asc` [12] | `o h l c v t` → open, high, low, close, volume, session date (`t` = Unix ms of the window start in New York time) | `limit` max 50000 base aggregates; `next_url` paging. **Aggregates include pre-market and after-hours trades** [12], so `o`/`c` may differ from the official opening and closing auction prices. Unverified: must be reconciled against a second source before research use. |
| Splits | `GET /stocks/v1/splits` [13] | `ticker, execution_date` (ex-date), `split_to / split_from` (ratio), `adjustment_type` (`forward_split`, `reverse_split`, `stock_dividend`), `id` | Since 1978-10-25 (vendor-stated, unverified); `limit` max 5000. A stock dividend is a share-count change, so it is mapped as a split. |
| Dividends | `GET /stocks/v1/dividends` [4] | `ticker, ex_dividend_date, cash_amount, declaration_date, record_date, pay_date, distribution_type, currency, id` | Non-USD amounts are rejected rather than converted. |
| Security master | `GET /v3/reference/tickers?active=…&date=…` [1] | `ticker, name, active, delisted_utc, share_class_figi, composite_figi, cik` | `limit` max 1000. |
| Ticker history | `GET /vX/reference/tickers/{id}/events?types=ticker_change` [3] | `events[].date, events[].ticker_change.ticker` | Experimental. `id` is documented as a ticker, CUSIP or **composite** FIGI, so the adapter passes `composite_figi` (a security without one is skipped, not guessed). |

[12] https://massive.com/docs/rest/stocks/aggregates/custom-bars.md ·
[13] https://massive.com/docs/rest/stocks/corporate-actions/splits.md

## What is implemented (`quantlab.data.vendors`)

* `http.JsonApi`:
  * HTTPS only; redirects are never followed (the key is in the query string).
  * Bodies are capped at 64 MiB, and read-time failures (truncated body, TLS error,
    reset) are retried like connection failures.
  * The key is redacted in raw and URL-encoded forms.
  * `next_url` must stay on the vendor's host.
  * Retries 429, 5xx and network errors with exponential backoff and jitter, honouring
    `Retry-After` as a floor.
  * 401/403 is `VendorAuthError` and is not retried.
  * A non-JSON or mis-shaped body is `VendorSchemaError`.
  * Uses the existing `TokenBucket` limiter. Set its rate from the purchased plan's
    documented limit; plan limits were not verified here.
  * The key is redacted from every error and from `repr`.
* `massive.MassiveProvider`:
  * The security master, ticker events and the identity built from them: `share_class_figi`
    becomes the `instrument_id`; listing date, delisting reason and delisting return stay
    unknown because the vendor does not document them.
  * Raw daily bars and actions per ticker, with field-by-field validation.
  * Per-security fetches that request each ticker only inside its validity interval.
  * `by_instrument(history)`: a `MarketDataProvider` keyed by id, so the existing
    resumable `ingest()` checkpoints per security. Its output then goes through the #10
    ingestion gates.
* **Tests** (`tests/unit/test_vendor_massive.py`) run against a scripted fake that serves
  *synthetic* data in the documented response shapes. They cover renames, delisting,
  ticker reuse, pagination, a failed page mid-backfill, 429/5xx/401, bounded retries,
  malformed payloads, an off-host `next_url`, a real redirect on a loopback server,
  limiter pacing, resumed ingestion and a misreported split ratio blocking ingestion.
  **No test touches the network, and live behaviour is unverified.**
* **Fail-closed checks:**
  * Aggregates must say `adjusted: false` (a missing flag is rejected).
  * Dividends must state a currency (USD only).
  * A security counts as delisted only when none of its reference rows is active, so a
    rename's inactive old-ticker row does not truncate it.
  * `identity()` needs the *current* `active` flags: build its input from
    `reference(active=True)` and `reference(active=False)` without `date=`. A reference as
    of a past date marks later delistings active, which loses them and biases the universe
    toward survivors.
  * Network failures: a truncated body (unread `Content-Length`) and connection errors are
    retried; an invalid URL and a failed certificate check are not. Errors are raised
    outside the `except` block with the URL removed, so neither the message nor the
    chained `__context__` carries the key.
* **Wired but inert (#11):** `quantlab.data.vendors.registry` registers Massive (bearer
  auth, documented in the quickstart); the `dataset.vendor` job runs only when provider,
  key, licence and plan are all configured (docs/RUNBOOK.md). It identifies securities
  with the current `active` flags, ingests by permanent id, fails rather than store a
  partial universe, runs the #10 gates, and stores the symbol history and master. No API
  route or UI offers it yet, and the vendor and licence remain the user's choice.
  Optional `tickers` select *securities*: every reference row of each matching
  `share_class_figi` is kept, so naming a renamed security's old ticker does not make it
  look delisted. A ticker the vendor does not know fails the job. The asset limit is
  checked on distinct securities before `identity()`, which makes one request per
  security. Provenance records the full counts of unidentified and skipped securities.
  Provenance (`fetched_at`, request counts) is not part of the version hash, so an
  identical re-fetch reuses the existing version and keeps its first provenance.

## Licensing rules for this repository

* No vendor data is committed. Fixtures are synthetic, labelled synthetic, and mimic the
  documented response *shape* only.
* API keys come from settings or secrets only, are never logged, and are never used in
  CI.
* A dataset ingested from a vendor records `provider`, `license` and the plan in its
  provenance, exactly as the CSV provider does today.
