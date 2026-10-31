"use client";

import Link from "next/link";
import { useState } from "react";
import { CompareChart, SERIES } from "@/components/charts";
import { PageHeader } from "@/components/Shell";
import { ErrorNote, Panel, QueryError, Skeleton, SyntheticLabel } from "@/components/ui";
import { api, ApiError, type Experiment, type Metrics, type Page, type Series } from "@/lib/api";
import { num, pct, signClass, strategyLabel } from "@/lib/format";
import { combined, useSynthetic } from "@/lib/synthetic";
import { useApi } from "@/lib/useApi";
import { useWorkspace } from "@/lib/workspace";

interface Compared {
  id: string;
  name: string;
  strategy_key: string;
  summary: Metrics;
  series: Series;
}

const COLS: [string, string, (v: number | null) => string][] = [
  ["Total return", "total_return", (v) => pct(v)],
  ["CAGR", "cagr", (v) => pct(v, 2)],
  ["Volatility", "volatility", (v) => pct(v)],
  ["Sharpe", "sharpe", (v) => num(v)],
  ["Sortino", "sortino", (v) => num(v)],
  ["Max drawdown", "max_drawdown", (v) => pct(v)],
  ["Beta", "beta", (v) => num(v)],
  ["Information ratio", "information_ratio", (v) => num(v)],
  ["Turnover", "annual_turnover", (v) => (v === null ? "—" : `${num(v, 1)}×`)],
  ["Cost drag", "cost_drag_cagr", (v) => pct(v, 2)],
];

export default function ComparePage() {
  const { workspace } = useWorkspace();
  const done = useApi<Page<Experiment>>(workspace ? `/workspaces/${workspace.id}/experiments?status=completed&limit=100` : null);
  const [selected, setSelected] = useState<string[]>([]);
  const [result, setResult] = useState<Compared[]>();
  const [err, setErr] = useState<ApiError>();
  const [busy, setBusy] = useState(false);
  const synthetic = useSynthetic();
  const versionOf = new Map(done.data?.items.map((e) => [e.id, e.dataset_version_id]));
  const isSynthetic = (id: string) => synthetic(versionOf.get(id));

  function toggle(id: string) {
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : s.length >= 6 ? s : [...s, id]));
  }

  async function run() {
    if (!workspace) return;
    setBusy(true);
    setErr(undefined);
    try {
      const r = await api<{ experiments: Compared[] }>(`/workspaces/${workspace.id}/experiments/compare`, {
        method: "POST",
        json: { experiment_ids: selected },
      });
      setResult(r.experiments);
    } catch (e) {
      setErr(e as ApiError);
    }
    setBusy(false);
  }

  return (
    <>
      <PageHeader
        title="Compare experiments"
        lede="Growth of one dollar on a common scale, with the headline statistics side by side. Select up to six completed experiments."
      />
      <div className="grid gap-6 xl:grid-cols-[1fr_2.5fr]">
        <Panel title="Completed experiments" aside={`${selected.length} of 6 selected`}>
          <QueryError q={done} what="completed experiments" />
          {done.loading && !done.data ? (
            <Skeleton />
          ) : done.error && !done.data ? null : done.data?.items.length === 0 ? (
            <p className="text-sm text-ink-2">
              Nothing to compare yet. <Link className="text-cobalt underline underline-offset-2" href="/experiments/new">Run an experiment</Link> first.
            </p>
          ) : (
            <ul className="max-h-[28rem] space-y-1 overflow-auto text-sm" tabIndex={0}>
              {done.data?.items.map((e) => (
                <li key={e.id}>
                  <label className="flex cursor-pointer items-start gap-2 rounded px-1.5 py-1 hover:bg-sunken">
                    <input type="checkbox" className="mt-0.5" checked={selected.includes(e.id)} onChange={() => toggle(e.id)} />
                    <span>
                      <span className="block">
                        {e.name}
                        <SyntheticLabel inline status={synthetic(e.dataset_version_id)} />
                      </span>
                      <span className="block text-xs text-ink-2">{strategyLabel(e.strategy_key)}</span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
          <button className="btn btn-primary mt-4" disabled={selected.length === 0 || busy} onClick={run}>
            {busy ? "Loading…" : "Compare selected"}
          </button>
          <div className="mt-3">
            <ErrorNote error={err} />
          </div>
        </Panel>
        <div className="space-y-6">
          {result ? (
            <>
              <Panel title="Growth of $1" aside={<SyntheticLabel status={combined(result.map((r) => isSynthetic(r.id)))} />}>
                <CompareChart series={result.map((r) => ({ name: r.name, dates: r.series.dates, values: r.series.normalized as (number | null)[] }))} />
              </Panel>
              <Panel title="Statistics">
                <div className="overflow-x-auto" tabIndex={0}>
                  <table className="data num">
                    <thead>
                      <tr>
                        <th>Experiment</th>
                        {COLS.map(([l]) => (
                          <th key={l} className="r">
                            {l}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {result.map((r, i) => (
                        <tr key={r.id}>
                          <td>
                            <span className="mr-2 inline-block h-2 w-2 rounded-full align-middle" style={{ background: SERIES[i % SERIES.length] }} aria-hidden />
                            <Link href={`/experiments/${r.id}`} className="text-cobalt hover:underline">
                              {r.name}
                            </Link>
                            <SyntheticLabel inline status={isSynthetic(r.id)} />
                          </td>
                          {COLS.map(([l, k, f]) => (
                            <td key={l} className={`r ${k === "total_return" || k === "cagr" ? signClass(r.summary[k]) : ""}`}>
                              {f(r.summary[k] ?? null)}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="mt-3 text-xs text-ink-3">
                  Comparing many variants on the same data inflates the best result by chance. Treat the top row as a hypothesis to validate out of sample.
                </p>
              </Panel>
            </>
          ) : (
            <div className="rounded border border-dashed border-rule px-6 py-16 text-center text-sm text-ink-2">Select experiments to compare.</div>
          )}
        </div>
      </div>
    </>
  );
}
