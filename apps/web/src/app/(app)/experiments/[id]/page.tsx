"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { EquityChart, MonthlyReturns } from "@/components/charts";
import { PageHeader } from "@/components/Shell";
import { ErrorNote, JobProgress, Panel, Skeleton, StatusPill, SyntheticTag } from "@/components/ui";
import { IntegrityReport, ValidationPanel } from "@/components/Validation";
import { api, ApiError, newIdempotencyKey, type DatasetVersion, type Experiment, type Job, type Series } from "@/lib/api";
import { DASH, money, num, pct, shortHash, signClass, strategyLabel } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type MetricBlock = { values: Record<string, number | null>; undefined: Record<string, string> };
interface Analysis {
  performance: MetricBlock;
  gross_performance: MetricBlock;
  benchmark_performance: MetricBlock | null;
  trades: MetricBlock;
  costs: MetricBlock;
  risk: Record<string, number>;
  risk_99: Record<string, number>;
  drawdowns: { peak: string; trough: string; recovery: string | null; depth: number; decline_sessions: number; recovery_sessions: number | null; total_sessions: number }[];
  worst_periods: { start: string; end: string; return: number }[];
  monthly_returns: { year: number; month: number; return: number }[];
  exposure: Record<string, number>;
  final_concentration: Record<string, number | null>;
  contributions: { top: { symbol: string; pnl: number }[]; bottom: { symbol: string; pnl: number }[]; method?: string };
  rejections: { count: number; by_reason: Record<string, number> };
  meta: { universe: string[]; benchmark: string | null; sessions: number; first_session: string; last_session: string };
}
interface Trade {
  date: string;
  decision_date: string;
  symbol: string;
  side: string;
  quantity: number;
  reference_price: number;
  price: number;
  notional: number;
  commission: number;
  slippage_cost: number;
  partial: boolean;
  note: string | null;
}

const ACTIVE = new Set(["queued", "running"]);
// Only return measures get gain/loss color; risk measures stay neutral ink.
const SIGNED = new Set(["total_return", "cagr"]);

function Value({ m, k, fmt }: { m: MetricBlock | null | undefined; k: string; fmt: (v: number | null) => string }) {
  if (!m) return <span className="text-ink-3">{DASH}</span>;
  const v = m.values[k];
  if (v === null || v === undefined) {
    return (
      <span className="text-ink-3" title={m.undefined[k] ? `Undefined: ${m.undefined[k]}` : undefined}>
        {DASH}
      </span>
    );
  }
  return <span>{fmt(v)}</span>;
}

const ROWS: [string, string, (v: number | null) => string][] = [
  ["Total return", "total_return", (v) => pct(v, 1)],
  ["CAGR", "cagr", (v) => pct(v, 2)],
  ["Annualized volatility", "volatility", (v) => pct(v, 1)],
  ["Sharpe ratio", "sharpe", (v) => num(v)],
  ["Sortino ratio", "sortino", (v) => num(v)],
  ["Maximum drawdown", "max_drawdown", (v) => pct(v, 1)],
  ["Calmar ratio", "calmar", (v) => num(v)],
  ["Worst day", "worst_day", (v) => pct(v, 2)],
  ["Positive days", "hit_rate", (v) => pct(v, 1)],
];

function EvidenceRibbon({ exp, version, analysis }: { exp: Experiment; version?: DatasetVersion; analysis?: Analysis }) {
  const cfg = exp.config as Record<string, unknown>;
  const items: [string, React.ReactNode][] = [
    ["Dataset", version ? <Link className="text-cobalt hover:underline" href={`/data/${version.id}`}>{shortHash(version.version_hash, 12)}</Link> : DASH],
    ["Sample", analysis ? `${analysis.meta.first_session} to ${analysis.meta.last_session}` : DASH],
    ["Universe", analysis ? `${analysis.meta.universe.length} symbols` : cfg.universe ? `${(cfg.universe as string[]).length} symbols` : "All"],
    ["Fills", cfg.execution === "next_close" ? "Next close" : "Next open"],
    ["Costs (bps)", `${cfg.commission_bps} fee + ${cfg.slippage_bps} slippage`],
    ["Rebalance", String(cfg.rebalance)],
    ["Engine", `v${exp.engine_version}`],
    ["Inputs", <span key="h" className="num" title={exp.input_hash}>{shortHash(exp.input_hash, 10)}</span>],
    ["Output", <span key="f" className="num" title={exp.fingerprint ?? undefined}>{shortHash(exp.fingerprint, 10)}</span>],
  ];
  return (
    <section aria-label="Evidence" className="mb-6 rounded-[6px] border border-ink/80 bg-surface">
      <div className="flex flex-wrap items-center gap-2 border-b border-rule px-4 py-2 text-xs">
        <span className="font-semibold text-ink">Evidence</span>
        {version?.is_synthetic && <SyntheticTag />}
        <span className="text-ink-2">Results below were computed from exactly these inputs and can be regenerated from the manifest.</span>
      </div>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 px-4 py-3 text-sm sm:grid-cols-3 lg:grid-cols-5 2xl:grid-cols-9">
        {items.map(([k, v]) => (
          <div key={k} className="min-w-0">
            <dt className="text-xs text-ink-2">{k}</dt>
            <dd className="truncate">{v}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function Trades({ id }: { id: string }) {
  const [offset, setOffset] = useState(0);
  const [symbol, setSymbol] = useState("");
  const qs = new URLSearchParams({ limit: "25", offset: String(offset) });
  if (symbol) qs.set("symbol", symbol);
  const t = useApi<{ items: Trade[]; total: number }>(`/experiments/${id}/trades?${qs}`);
  return (
    <Panel title="Trade blotter" aside={t.data ? `${t.data.total.toLocaleString()} fills` : undefined}>
      <label className="mb-3 block w-48">
        <span className="sr-only">Filter by symbol</span>
        <input className="field" placeholder="Filter by symbol" value={symbol} onChange={(e) => { setSymbol(e.target.value.toUpperCase()); setOffset(0); }} />
      </label>
      <div className="overflow-x-auto">
        <table className="data num">
          <thead>
            <tr>
              <th>Decided</th>
              <th>Filled</th>
              <th>Symbol</th>
              <th>Side</th>
              <th className="r">Shares</th>
              <th className="r">Reference</th>
              <th className="r">Fill price</th>
              <th className="r">Notional</th>
              <th className="r">Commission</th>
              <th className="r">Slippage</th>
              <th>Notes</th>
            </tr>
          </thead>
          <tbody>
            {t.data?.items.map((r, i) => (
              <tr key={`${r.date}-${r.symbol}-${i}`}>
                <td className="text-ink-2">{r.decision_date}</td>
                <td>{r.date}</td>
                <td className="font-medium">{r.symbol}</td>
                <td className={r.side === "buy" ? "text-gain" : "text-loss"}>{r.side}</td>
                <td className="r">{num(Math.abs(r.quantity), 0)}</td>
                <td className="r">{num(r.reference_price)}</td>
                <td className="r">{num(r.price)}</td>
                <td className="r">{money(r.notional)}</td>
                <td className="r">{money(r.commission, 2)}</td>
                <td className="r">{money(r.slippage_cost, 2)}</td>
                <td className="text-xs text-ink-2">{[r.partial ? "partial fill" : "", r.note ?? ""].filter(Boolean).join(", ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {t.data && t.data.total > 25 && (
        <div className="mt-3 flex justify-end gap-2 text-sm">
          <button className="btn btn-quiet" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 25))}>
            Previous
          </button>
          <button className="btn btn-quiet" disabled={offset + 25 >= t.data.total} onClick={() => setOffset(offset + 25)}>
            Next
          </button>
        </div>
      )}
    </Panel>
  );
}

function Holdings({ id, dates }: { id: string; dates: string[] }) {
  const [on, setOn] = useState<string>("");
  const p = useApi<{ date: string | null; items: { symbol: string; quantity: number; price: number; value: number; weight: number }[] }>(
    `/experiments/${id}/positions${on ? `?on=${on}` : ""}`,
  );
  return (
    <Panel
      title="Holdings"
      aside={
        <label className="flex items-center gap-2">
          As of
          <input type="date" className="field w-auto py-0.5" min={dates[0]} max={dates[dates.length - 1]} value={on || p.data?.date || ""} onChange={(e) => setOn(e.target.value)} />
        </label>
      }
    >
      {p.data?.items.length === 0 ? (
        <p className="text-sm text-ink-2">Fully in cash on this date.</p>
      ) : (
        <div className="max-h-80 overflow-auto">
          <table className="data num">
            <thead>
              <tr>
                <th>Symbol</th>
                <th className="r">Shares</th>
                <th className="r">Price</th>
                <th className="r">Value</th>
                <th className="r">Weight</th>
              </tr>
            </thead>
            <tbody>
              {p.data?.items.map((h) => (
                <tr key={h.symbol}>
                  <td className="font-medium">{h.symbol}</td>
                  <td className="r">{num(h.quantity, 0)}</td>
                  <td className="r">{num(h.price)}</td>
                  <td className="r">{money(h.value)}</td>
                  <td className="r">{pct(h.weight, 1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

export default function ExperimentPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const exp = useApi<Experiment>(`/experiments/${id}`, { poll: (e) => (e && ACTIVE.has(e.status) ? 1500 : false) });
  const job = useApi<Job>(`/experiments/${id}/job`, { poll: (j) => (j && ACTIVE.has(j.status) ? 1000 : false) });
  const done = exp.data?.status === "completed";
  const analysis = useApi<Analysis>(done ? `/experiments/${id}/analysis` : null);
  const equity = useApi<Series>(done ? `/experiments/${id}/equity` : null);
  const version = useApi<DatasetVersion>(exp.data ? `/dataset-versions/${exp.data.dataset_version_id}` : null);
  const repro = useApi<{ comparisons: { experiment_id: string; name: string; fingerprint_match: boolean; same_inputs: boolean; status: string }[] }>(done ? `/experiments/${id}/reproducibility` : null);
  const [actionErr, setActionErr] = useState<ApiError>();
  const [validationRun, setValidationRun] = useState("");

  async function reproduce() {
    try {
      const r = await api<{ experiment: Experiment }>(`/experiments/${id}/reproduce`, { method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() } });
      router.push(`/experiments/${r.experiment.id}`);
    } catch (e) {
      setActionErr(e as ApiError);
    }
  }
  async function cancel() {
    if (!job.data) return;
    await api(`/jobs/${job.data.id}/cancel`, { method: "POST" }).catch((e) => setActionErr(e));
    job.reload();
    exp.reload();
  }
  async function downloadManifest() {
    const m = await api<unknown>(`/experiments/${id}/manifest`);
    const url = URL.createObjectURL(new Blob([JSON.stringify(m, null, 2)], { type: "application/json" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: `quantlab-experiment-${id}.json` });
    a.click();
    URL.revokeObjectURL(url);
  }

  if (exp.error) return <ErrorNote error={exp.error} />;
  if (!exp.data) return <Skeleton h="h-96" />;
  const e = exp.data;
  const a = analysis.data;
  const eq = equity.data;
  const benchLabel = a?.meta.benchmark ?? "Benchmark";

  return (
    <>
      <p className="mb-2 text-sm">
        <Link href="/experiments" className="text-cobalt hover:underline">
          Experiments
        </Link>
      </p>
      <PageHeader
        title={e.name}
        lede={
          <>
            {strategyLabel(e.strategy_key)}{" "}
            <span className="num text-ink-3">
              {Object.entries(e.strategy_params)
                .filter(([, v]) => v !== null && typeof v !== "object")
                .map(([k, v]) => `${k.replaceAll("_", " ")} ${v}`)
                .join(", ")}
            </span>
            {e.notes && <span className="mt-1 block text-ink-2">{e.notes}</span>}
          </>
        }
        actions={
          <>
            <StatusPill status={e.status} />
            {done && (
              <>
                <a className="btn btn-quiet" href={`/api/v1/experiments/${id}/report?format=html`} target="_blank" rel="noopener">
                  Open report
                </a>
                <a className="btn btn-quiet" href={`/api/v1/experiments/${id}/report?format=md&download=true`}>
                  Export Markdown
                </a>
                <button className="btn btn-quiet" onClick={downloadManifest}>
                  Download manifest
                </button>
                <button className="btn btn-quiet" onClick={reproduce}>
                  Reproduce
                </button>
              </>
            )}
          </>
        }
      />
      <ErrorNote error={actionErr} />
      <EvidenceRibbon exp={e} version={version.data} analysis={a} />

      {!done && job.data && ACTIVE.has(e.status) && (
        <Panel title="Run status" className="mb-6">
          <JobProgress job={job.data} onCancel={cancel} />
        </Panel>
      )}

      {done && (
        <div className="space-y-6">
          <div className="grid gap-6 2xl:grid-cols-[3fr_2fr]">
            <Panel title="Portfolio value" aside="Net of commissions and slippage">
              {eq ? (
                <EquityChart
                  dates={eq.dates}
                  equity={eq.equity as (number | null)[]}
                  benchmark={eq.benchmark as (number | null)[] | undefined}
                  drawdown={eq.drawdown as (number | null)[]}
                  benchmarkDrawdown={eq.benchmark_drawdown as (number | null)[] | undefined}
                  benchmarkLabel={`${benchLabel} (total return)`}
                />
              ) : (
                <Skeleton h="h-96" />
              )}
            </Panel>
            <Panel title="Performance">
              {a ? (
                <table className="data num">
                  <thead>
                    <tr>
                      <th>Measure</th>
                      <th className="r">Strategy</th>
                      <th className="r">Before costs</th>
                      <th className="r">{benchLabel}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {ROWS.map(([label, k, fmt]) => (
                      <tr key={k}>
                        <td>{label}</td>
                        <td className={`r font-medium ${SIGNED.has(k) ? signClass(a.performance.values[k]) : ""}`}>
                          <Value m={a.performance} k={k} fmt={fmt} />
                        </td>
                        <td className="r text-ink-2">
                          <Value m={a.gross_performance} k={k} fmt={fmt} />
                        </td>
                        <td className="r text-ink-2">
                          <Value m={a.benchmark_performance} k={k} fmt={fmt} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <Skeleton />
              )}
              {a && (
                <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                  {[
                    ["Beta", num(a.performance.values.beta)],
                    ["Alpha (annualized)", pct(a.performance.values.alpha, 2)],
                    ["Tracking error", pct(a.performance.values.tracking_error, 1)],
                    ["Information ratio", num(a.performance.values.information_ratio)],
                    ["Annual turnover", `${num(a.costs.values.annual_turnover, 2)}×`],
                    ["Cost drag on CAGR", pct(a.costs.values.cost_drag_cagr, 2)],
                    ["Total costs", money(a.costs.values.total_costs)],
                    ["Dividends received", money(a.costs.values.total_dividends)],
                  ].map(([k, v]) => (
                    <div key={k} className="flex justify-between gap-2 border-b border-sunken pb-1">
                      <dt className="text-ink-2">{k}</dt>
                      <dd className="num">{v}</dd>
                    </div>
                  ))}
                </dl>
              )}
              <p className="mt-3 text-xs text-ink-3">
                “Before costs” adds back commissions and slippage paid. A dash marks a statistic that is undefined for this
                sample; hover it for the reason.
              </p>
            </Panel>
          </div>

          {a && (
            <div className="grid gap-6 lg:grid-cols-3">
              <Panel title="Tail risk" aside="One-day horizon, historical">
                <dl className="space-y-2 text-sm">
                  {[
                    ["Value at risk, 95%", pct(a.risk.historical_var, 2)],
                    ["Expected shortfall, 95%", pct(a.risk.historical_es, 2)],
                    ["Value at risk, 99%", pct(a.risk_99.historical_var, 2)],
                    ["Expected shortfall, 99%", pct(a.risk_99.historical_es, 2)],
                    ["Gaussian VaR, 95%", pct(a.risk.gaussian_var, 2)],
                    ["Cornish-Fisher VaR, 95%", pct(a.risk.cornish_fisher_var, 2)],
                  ].map(([k, v]) => (
                    <div key={k} className="flex justify-between gap-2">
                      <dt className="text-ink-2">{k}</dt>
                      <dd className="num">{v}</dd>
                    </div>
                  ))}
                </dl>
                <p className="mt-3 text-xs text-ink-3">Losses as a share of portfolio value. Historical estimates cannot exceed the worst day in the sample.</p>
              </Panel>
              <Panel title="Largest drawdowns">
                <table className="data num">
                  <thead>
                    <tr>
                      <th>Peak</th>
                      <th className="r">Depth</th>
                      <th className="r">Sessions</th>
                      <th>Recovered</th>
                    </tr>
                  </thead>
                  <tbody>
                    {a.drawdowns.map((d) => (
                      <tr key={d.peak}>
                        <td>{d.peak.slice(0, 10)}</td>
                        <td className="r text-loss">{pct(d.depth, 1)}</td>
                        <td className="r">{d.total_sessions}</td>
                        <td className="text-ink-2">{d.recovery ? d.recovery.slice(0, 10) : "Not yet"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Panel>
              <Panel title="Contribution to P&L" aside="Realized and unrealized">
                <div className="grid grid-cols-2 gap-4 text-sm">
                  {[
                    ["Largest gains", a.contributions.top.slice(0, 6)],
                    ["Largest losses", a.contributions.bottom.slice(0, 6)],
                  ].map(([title, list]) => (
                    <div key={title as string}>
                      <h3 className="mb-1.5 text-xs text-ink-2">{title as string}</h3>
                      <ul className="num space-y-1">
                        {(list as { symbol: string; pnl: number }[]).map((c) => (
                          <li key={c.symbol} className="flex justify-between gap-2">
                            <span>{c.symbol}</span>
                            <span className={signClass(c.pnl)}>{money(c.pnl)}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              </Panel>
            </div>
          )}

          <IntegrityReport experimentId={id} refreshKey={`${validationRun}-${repro.data?.comparisons.length ?? 0}`} />
          <ValidationPanel experimentId={id} onComplete={setValidationRun} />

          {a && (
            <Panel title="Monthly returns" aside="Net of costs; cells shaded by sign and size">
              <MonthlyReturns data={a.monthly_returns} />
            </Panel>
          )}

          <div className="grid gap-6 2xl:grid-cols-[3fr_2fr]">
            <Trades id={id} />
            {eq && <Holdings id={id} dates={eq.dates} />}
          </div>

          {a && (a.rejections.count > 0 || repro.data?.comparisons.length) ? (
            <Panel title="Execution and reproducibility">
              {a.rejections.count > 0 && (
                <p className="text-sm">
                  {a.rejections.count} orders were rejected:{" "}
                  {Object.entries(a.rejections.by_reason)
                    .map(([r, n]) => `${n} ${r.replaceAll("_", " ")}`)
                    .join(", ")}
                  .
                </p>
              )}
              {repro.data?.comparisons.map((c) => (
                <p key={c.experiment_id} className="mt-2 text-sm">
                  <Link href={`/experiments/${c.experiment_id}`} className="text-cobalt hover:underline">
                    {c.name}
                  </Link>
                  :{" "}
                  {c.status !== "completed"
                    ? `run is ${c.status}`
                    : c.fingerprint_match
                      ? "outputs match this run exactly"
                      : "outputs differ from this run"}
                  {!c.same_inputs && " (different inputs)"}
                </p>
              ))}
            </Panel>
          ) : null}
        </div>
      )}
      {(e.status === "failed" || e.status === "timed_out" || e.status === "cancelled") && job.data && (
        <Panel title="Run status">
          <JobProgress job={job.data} />
          <p className="mt-3 text-sm text-ink-2">No results were stored for this run. Adjust the configuration and submit a new experiment.</p>
        </Panel>
      )}
    </>
  );
}
