"use client";

import Link from "next/link";
import { PageHeader } from "@/components/Shell";
import { Empty, Panel, QueryError, Skeleton, StatusPill, SyntheticLabel } from "@/components/ui";
import type { DatasetVersion, Experiment, Job, Page } from "@/lib/api";
import { dateTime, num, pct, shortHash, signClass, strategyLabel } from "@/lib/format";
import { useSynthetic } from "@/lib/synthetic";
import { useApi } from "@/lib/useApi";
import { useWorkspace } from "@/lib/workspace";

export default function Overview() {
  const { workspace } = useWorkspace();
  const ws = workspace?.id;
  const exps = useApi<Page<Experiment>>(ws ? `/workspaces/${ws}/experiments?limit=8` : null, { poll: 5000 });
  const jobs = useApi<Page<Job>>(ws ? `/workspaces/${ws}/jobs?limit=5` : null, { poll: 3000 });
  const versions = useApi<DatasetVersion[]>(ws ? `/workspaces/${ws}/dataset-versions` : null);
  const noData = versions.data && versions.data.length === 0;
  const synthetic = useSynthetic(exps.data?.items);

  return (
    <>
      <PageHeader
        title={workspace?.name ?? "Overview"}
        lede="Recent experiments, background jobs and the dataset versions available in this workspace."
        actions={
          <Link href="/experiments/new" className="btn btn-primary">
            New experiment
          </Link>
        }
      />
      {noData && (
        <div className="mb-6">
          <Empty title="This workspace has no market data yet">
            Start by generating a synthetic dataset. It is deterministic, clearly labelled and contains splits,
            dividends and delistings, so every part of the research workflow can be exercised.
            <div className="mt-4">
              <Link href="/data" className="btn btn-primary">
                Add market data
              </Link>
            </div>
          </Empty>
        </div>
      )}
      <div className="grid gap-6 xl:grid-cols-[2fr_1fr]">
        <Panel title="Recent experiments" aside={<Link className="text-cobalt hover:underline" href="/experiments">View all</Link>}>
          <QueryError q={exps} what="recent experiments" />
          {exps.loading && !exps.data ? (
            <Skeleton />
          ) : exps.error && !exps.data ? null : exps.data && exps.data.items.length === 0 ? (
            <p className="text-sm text-ink-2">No experiments yet. Configure a strategy and run your first backtest.</p>
          ) : (
            <div className="overflow-x-auto" tabIndex={0}>
              <table className="data num">
                <thead>
                  <tr>
                    <th>Experiment</th>
                    <th>Strategy</th>
                    <th>Status</th>
                    <th className="r">CAGR</th>
                    <th className="r">Sharpe</th>
                    <th className="r">Max drawdown</th>
                  </tr>
                </thead>
                <tbody>
                  {exps.data?.items.map((e) => (
                    <tr key={e.id}>
                      <td>
                        <Link href={`/experiments/${e.id}`} className="font-medium text-cobalt hover:underline">
                          {e.name}
                        </Link>
                        <SyntheticLabel inline status={synthetic(e)} />
                      </td>
                      <td className="text-ink-2">{strategyLabel(e.strategy_key)}</td>
                      <td>
                        <StatusPill status={e.status} />
                      </td>
                      <td className={`r ${signClass(e.summary?.cagr)}`}>{pct(e.summary?.cagr)}</td>
                      <td className="r">{num(e.summary?.sharpe)}</td>
                      <td className="r text-loss">{pct(e.summary?.max_drawdown)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
        <div className="space-y-6">
          <Panel title="Background jobs">
            <QueryError q={jobs} what="background jobs" />
            {jobs.data && jobs.data.items.length === 0 && <p className="text-sm text-ink-2">No jobs have run yet.</p>}
            <ul className="space-y-3">
              {jobs.data?.items.map((j) => (
                <li key={j.id} className="flex items-center justify-between gap-3 text-sm">
                  <div className="min-w-0">
                    <div className="truncate">{j.kind === "backtest" ? "Backtest" : j.kind === "dataset.synthetic" ? "Dataset ingestion" : j.kind}</div>
                    <div className="truncate text-xs text-ink-2">{j.message || dateTime(j.created_at)}</div>
                  </div>
                  <StatusPill status={j.status} />
                </li>
              ))}
            </ul>
          </Panel>
          <Panel title="Dataset versions" aside={<Link className="text-cobalt hover:underline" href="/data">Manage</Link>}>
            <QueryError q={versions} what="dataset versions" />
            <ul className="space-y-2 text-sm">
              {versions.data?.slice(0, 5).map((v) => (
                <li key={v.id} className="flex items-center justify-between gap-2">
                  <Link href={`/data/${v.id}`} className="num text-cobalt hover:underline">
                    {shortHash(v.version_hash)}
                  </Link>
                  <span className="text-xs text-ink-2">
                    {v.symbol_count} symbols, {v.start_date.slice(0, 4)}–{v.end_date.slice(0, 4)}
                  </span>
                  <StatusPill status={v.quality_status} />
                </li>
              ))}
            </ul>
          </Panel>
        </div>
      </div>
    </>
  );
}
