"use client";

import { useState } from "react";
import { Legend, SERIES } from "@/components/charts";
import { ErrorNote, JobProgress, Panel, Pending, QueryError, StatusPill } from "@/components/ui";
import { api, ApiError, cancelJob, newIdempotencyKey, type Job } from "@/lib/api";
import { num, pct, signClass } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { useCanEdit } from "@/lib/workspace";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

interface Check {
  key: string;
  title: string;
  status: "verified" | "warning" | "failed" | "unassessed";
  finding: string;
}
interface Report {
  summary: Record<string, number>;
  checks: Check[];
  disclaimer: string;
}
interface Fold {
  fold: number;
  train: [string, string];
  test: [string, string];
  selected_params: Record<string, unknown>;
  in_sample_sharpe: number | null;
  oos_sharpe: number | null;
  oos_return: number | null;
  oos_benchmark_return: number | null;
}
interface ValidationRun {
  id: string;
  status: Job["status"];
  job: Job | null;
  result: {
    walk_forward: {
      error?: string;
      folds: Fold[];
      oos_equity: { dates: string[]; equity: number[] };
      mean_in_sample_sharpe: number | null;
      mean_oos_sharpe: number | null;
      method: string;
      train_sessions: number;
      test_sessions: number;
    };
    sensitivity: { rows: { label: string; is_base: boolean; sharpe: number | null; cagr: number | null; max_drawdown: number | null }[] };
    cost: { rows: { multiplier: number; round_trip_bps: number; sharpe: number | null; cagr: number | null; excess_total_return: number | null }[]; breakeven_multiplier: number | null; note: string | null };
    bootstrap: { sharpe?: { estimate: number; lower: number; upper: number; p_le_zero: number }; excess_return?: { estimate: number; lower: number; upper: number }; error?: string };
    deflated: { trials: number; psr_vs_zero: number | null; deflated_sharpe: number | null };
    regimes: { dimension: string; regime: string; sessions: number; strategy_return: number; strategy_sharpe: number | null; benchmark_return: number; benchmark_sharpe: number | null }[];
  } | null;
}

const ORDER: Check["status"][] = ["failed", "warning", "unassessed", "verified"];
const LABEL: Record<Check["status"], string> = { verified: "Verified", warning: "Warnings", failed: "Failed", unassessed: "Not assessed" };

export function IntegrityReport({ experimentId, refreshKey }: { experimentId: string; refreshKey: string }) {
  const rep = useApi<Report>(`/experiments/${experimentId}/integrity?v=${refreshKey}`);
  if (!rep.data) return <Pending q={rep} what="the integrity report" />;
  const checks = [...rep.data.checks].sort((a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status));
  return (
    <Panel title="Research integrity report" aside="How this result was produced, and what remains uncertain">
      <QueryError q={rep} what="the integrity report" />
      <dl className="mb-4 flex flex-wrap gap-x-6 gap-y-1 text-sm">
        {ORDER.map((s) => (
          <div key={s} className="flex items-center gap-2">
            <dt>
              <StatusPill status={s} />
              <span className="sr-only">{LABEL[s]}</span>
            </dt>
            <dd className="num">{rep.data!.summary[s] ?? 0}</dd>
          </div>
        ))}
      </dl>
      <ul className="divide-y divide-sunken">
        {checks.map((c) => (
          <li key={c.key} className="grid gap-1 py-2.5 sm:grid-cols-[11rem_6.5rem_1fr] sm:gap-4">
            <span className="text-sm font-medium">{c.title}</span>
            <span>
              <StatusPill status={c.status} />
            </span>
            <p className="max-w-prose text-sm text-ink-2">{c.finding}</p>
          </li>
        ))}
      </ul>
      <p className="mt-4 text-xs text-ink-3">{rep.data.disclaimer}</p>
    </Panel>
  );
}

function OosChart({ dates, equity }: { dates: string[]; equity: number[] }) {
  const data = dates.map((d, i) => ({ date: d, equity: equity[i] }));
  return (
    <figure aria-label="Stitched out-of-sample equity">
      <Legend items={[{ label: "Out-of-sample segments, stitched", color: SERIES[0] }]} />
      <div className="mt-2 h-48">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 4 }}>
            <CartesianGrid stroke="#e4e8ed" vertical={false} />
            <XAxis dataKey="date" tick={{ fill: "#5a6675", fontSize: 11 }} tickLine={false} tickFormatter={(d: string) => d.slice(0, 7)} minTickGap={48} />
            <YAxis tick={{ fill: "#5a6675", fontSize: 11 }} tickLine={false} width={64} domain={["auto", "auto"]} tickFormatter={(v: number) => `${(v / 1e6).toFixed(2)}M`} />
            <Tooltip formatter={(v) => [Number(v).toLocaleString("en-US", { maximumFractionDigits: 0 }), "Equity"]} />
            <Line dataKey="equity" stroke={SERIES[0]} strokeWidth={2} dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}

export function ValidationPanel({ experimentId, onComplete }: { experimentId: string; onComplete: (runId: string) => void }) {
  const [started, setStarted] = useState(0);
  const canEdit = useCanEdit();
  const run = useApi<ValidationRun>(`/experiments/${experimentId}/validation?s=${started}`, {
    poll: (r) => {
      if (r && (r.status === "queued" || r.status === "running")) return 1500;
      if (r?.status === "completed") onComplete(r.id);
      return false;
    },
  });
  const [err, setErr] = useState<ApiError>();
  const [busy, setBusy] = useState(false);

  async function start() {
    setBusy(true);
    setErr(undefined);
    try {
      await api(`/experiments/${experimentId}/validation`, { method: "POST", headers: { "Idempotency-Key": newIdempotencyKey() } });
      setStarted((n) => n + 1);
    } catch (e) {
      setErr(e as ApiError);
    }
    setBusy(false);
  }

  const notRun = run.error?.status === 404;
  const r = run.data?.result;
  const active = run.data && (run.data.status === "queued" || run.data.status === "running");

  return (
    <Panel
      title="Out-of-sample validation"
      aside={
        notRun || run.data?.status === "failed" || run.data?.status === "cancelled" ? (
          <button className="btn btn-primary py-1 text-xs" onClick={start} disabled={busy}>
            {busy ? "Starting…" : "Run validation suite"}
          </button>
        ) : undefined
      }
    >
      <ErrorNote error={err} />
      {/* 404 means the suite has not been run, which the panel explains below. */}
      {!notRun && <QueryError q={run} what="validation results" />}
      {notRun && (
        <p className="max-w-prose text-sm text-ink-2">
          The suite re-runs this strategy many times: a walk-forward test that chooses parameters on past data and evaluates them on the
          following unseen window, a parameter sweep, a trading-cost sweep, bootstrap confidence intervals, the deflated Sharpe ratio, a
          regime breakdown and an empirical look-ahead check. Results feed the integrity report.
        </p>
      )}
      {active && run.data?.job && (
        <JobProgress
          job={run.data.job}
          onCancel={
            canEdit
              ? async () => {
                  setErr(undefined);
                  await cancelJob(run.data!.job!.id).catch((e) => setErr(e as ApiError));
                  run.reload();
                }
              : undefined
          }
        />
      )}
      {r && (
        <div className="space-y-6">
          <section>
            <h3 className="mb-2 text-sm font-semibold">Walk-forward</h3>
            {r.walk_forward.error ? (
              <p className="text-sm text-ink-2">{r.walk_forward.error}</p>
            ) : (
              <>
                <p className="mb-3 text-sm text-ink-2">
                  {r.walk_forward.folds.length} {r.walk_forward.method} folds of {r.walk_forward.train_sessions} training and {r.walk_forward.test_sessions} test sessions. Mean Sharpe in-sample{" "}
                  <span className="num font-medium text-ink">{num(r.walk_forward.mean_in_sample_sharpe)}</span>, out-of-sample{" "}
                  <span className="num font-medium text-ink">{num(r.walk_forward.mean_oos_sharpe)}</span>.
                </p>
                <div className="grid gap-6 xl:grid-cols-2">
                  <div className="overflow-x-auto" tabIndex={0}>
                    <table className="data num">
                      <thead>
                        <tr>
                          <th>Fold</th>
                          <th>Test window</th>
                          <th>Chosen parameters</th>
                          <th className="r">Sharpe in</th>
                          <th className="r">Sharpe out</th>
                          <th className="r">Return out</th>
                          <th className="r">Benchmark</th>
                        </tr>
                      </thead>
                      <tbody>
                        {r.walk_forward.folds.map((f) => (
                          <tr key={f.fold}>
                            <td>{f.fold}</td>
                            <td>
                              {f.test[0]} to {f.test[1]}
                            </td>
                            <td className="text-xs text-ink-2">
                              {Object.entries(f.selected_params)
                                .map(([k, v]) => `${k} ${v}`)
                                .join(", ") || "none"}
                            </td>
                            <td className="r">{num(f.in_sample_sharpe)}</td>
                            <td className="r font-medium">{num(f.oos_sharpe)}</td>
                            <td className={`r ${signClass(f.oos_return)}`}>{pct(f.oos_return)}</td>
                            <td className="r text-ink-2">{pct(f.oos_benchmark_return)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <OosChart dates={r.walk_forward.oos_equity.dates} equity={r.walk_forward.oos_equity.equity} />
                </div>
              </>
            )}
          </section>

          <div className="grid gap-6 xl:grid-cols-3">
            <section>
              <h3 className="mb-2 text-sm font-semibold">Uncertainty</h3>
              <dl className="space-y-1.5 text-sm">
                {r.bootstrap.sharpe && (
                  <div className="flex justify-between gap-2">
                    <dt className="text-ink-2">Sharpe, 95% interval</dt>
                    <dd className="num">
                      {num(r.bootstrap.sharpe.lower)} to {num(r.bootstrap.sharpe.upper)}
                    </dd>
                  </div>
                )}
                {r.bootstrap.excess_return && (
                  <div className="flex justify-between gap-2">
                    <dt className="text-ink-2">Excess return, 95% interval</dt>
                    <dd className="num">
                      {pct(r.bootstrap.excess_return.lower)} to {pct(r.bootstrap.excess_return.upper)}
                    </dd>
                  </div>
                )}
                <div className="flex justify-between gap-2">
                  <dt className="text-ink-2">P(Sharpe &gt; 0)</dt>
                  <dd className="num">{pct(r.deflated.psr_vs_zero, 0)}</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt className="text-ink-2">Deflated Sharpe ({r.deflated.trials} trials)</dt>
                  <dd className="num">{num(r.deflated.deflated_sharpe)}</dd>
                </div>
              </dl>
              <p className="mt-2 text-xs text-ink-3">Stationary bootstrap, 1,000 resamples. The deflated Sharpe ratio discounts the best result for the number of variants tried.</p>
            </section>
            <section>
              <h3 className="mb-2 text-sm font-semibold">Trading-cost sweep</h3>
              <table className="data num">
                <thead>
                  <tr>
                    <th>Costs</th>
                    <th className="r">Round trip</th>
                    <th className="r">CAGR</th>
                    <th className="r">vs benchmark</th>
                  </tr>
                </thead>
                <tbody>
                  {r.cost.rows.map((c) => (
                    <tr key={c.multiplier}>
                      <td>{c.multiplier}×</td>
                      <td className="r">{num(c.round_trip_bps, 0)} bps</td>
                      <td className={`r ${signClass(c.cagr)}`}>{pct(c.cagr, 2)}</td>
                      <td className={`r ${signClass(c.excess_total_return)}`}>{pct(c.excess_total_return, 1)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="mt-2 text-xs text-ink-3">
                {r.cost.breakeven_multiplier !== null
                  ? `The edge over the benchmark disappears at about ${num(r.cost.breakeven_multiplier, 1)}× the assumed costs.`
                  : r.cost.note
                    ? `The strategy ${r.cost.note}.`
                    : ""}
              </p>
            </section>
            <section>
              <h3 className="mb-2 text-sm font-semibold">Parameter sweep (in-sample)</h3>
              <div className="max-h-64 overflow-auto" tabIndex={0}>
                <table className="data num">
                  <thead>
                    <tr>
                      <th>Parameters</th>
                      <th className="r">Sharpe</th>
                      <th className="r">Max DD</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.sensitivity.rows.map((s) => (
                      <tr key={s.label} className={s.is_base ? "bg-cobalt-soft" : ""}>
                        <td className="text-xs">
                          {s.label || "defaults"}
                          {s.is_base && <span className="ml-1 text-cobalt">this run</span>}
                        </td>
                        <td className="r">{num(s.sharpe)}</td>
                        <td className="r">{pct(s.max_drawdown, 1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          </div>

          {r.regimes.length > 0 && (
            <section>
              <h3 className="mb-2 text-sm font-semibold">Performance by market regime</h3>
              <div className="overflow-x-auto" tabIndex={0}>
                <table className="data num">
                  <thead>
                    <tr>
                      <th>Regime</th>
                      <th className="r">Sessions</th>
                      <th className="r">Strategy, annualized</th>
                      <th className="r">Strategy Sharpe</th>
                      <th className="r">Benchmark, annualized</th>
                      <th className="r">Benchmark Sharpe</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.regimes.map((g) => (
                      <tr key={`${g.dimension}-${g.regime}`}>
                        <td>{g.regime}</td>
                        <td className="r">{g.sessions}</td>
                        <td className={`r ${signClass(g.strategy_return)}`}>{pct(g.strategy_return)}</td>
                        <td className="r">{num(g.strategy_sharpe)}</td>
                        <td className={`r ${signClass(g.benchmark_return)}`}>{pct(g.benchmark_return)}</td>
                        <td className="r">{num(g.benchmark_sharpe)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="mt-2 text-xs text-ink-3">Regimes are classified from the benchmark&apos;s trailing 63-session volatility and 126-session return, using only data available before each day. The low/mid/high volatility cut-offs are terciles of the whole period, so this table is an after-the-fact breakdown, not a signal a strategy could have used.</p>
            </section>
          )}
        </div>
      )}
    </Panel>
  );
}
