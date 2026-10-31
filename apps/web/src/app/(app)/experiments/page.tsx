"use client";

import Link from "next/link";
import { useState } from "react";
import { PageHeader } from "@/components/Shell";
import { Panel, QueryError, Skeleton, StatusPill, SyntheticTag } from "@/components/ui";
import type { Experiment, Page } from "@/lib/api";
import { dateTime, multiple, num, pct, signClass, strategyLabel } from "@/lib/format";
import { useSynthetic } from "@/lib/synthetic";
import { useApi } from "@/lib/useApi";
import { useWorkspace } from "@/lib/workspace";

const PAGE = 25;

export default function ExperimentsPage() {
  const { workspace } = useWorkspace();
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const qs = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
  if (status) qs.set("status", status);
  if (q) qs.set("q", q);
  const synthetic = useSynthetic();
  const exps = useApi<Page<Experiment>>(workspace ? `/workspaces/${workspace.id}/experiments?${qs}` : null, { poll: 4000 });

  return (
    <>
      <PageHeader
        title="Experiments"
        lede="Every backtest records its dataset version, strategy parameters, execution assumptions and engine version."
        actions={
          <>
            <Link href="/compare" className="btn btn-quiet">
              Compare
            </Link>
            <Link href="/experiments/new" className="btn btn-primary">
              New experiment
            </Link>
          </>
        }
      />
      <Panel>
        <div className="mb-4 flex flex-wrap gap-3">
          <label className="w-64">
            <span className="sr-only">Search by name</span>
            <input className="field" placeholder="Search by name" value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} />
          </label>
          <label>
            <span className="sr-only">Status</span>
            <select className="field" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
              <option value="">All statuses</option>
              {["completed", "running", "queued", "failed", "cancelled", "timed_out"].map((s) => (
                <option key={s} value={s}>
                  {s.replace("_", " ")}
                </option>
              ))}
            </select>
          </label>
        </div>
        <QueryError q={exps} what="experiments" />
        {exps.loading && !exps.data ? (
          <Skeleton />
        ) : exps.error && !exps.data ? null : exps.data?.total === 0 ? (
          <p className="py-6 text-sm text-ink-2">No experiments match. Adjust the filters or create a new experiment.</p>
        ) : (
          <>
            <div className="overflow-x-auto" tabIndex={0}>
              <table className="data num">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Strategy</th>
                    <th>Status</th>
                    <th className="r">Total return</th>
                    <th className="r">CAGR</th>
                    <th className="r">Volatility</th>
                    <th className="r">Sharpe</th>
                    <th className="r">Max drawdown</th>
                    <th className="r">Turnover</th>
                    <th>Created</th>
                  </tr>
                </thead>
                <tbody>
                  {exps.data?.items.map((e) => (
                    <tr key={e.id}>
                      <td>
                        <Link href={`/experiments/${e.id}`} className="font-medium text-cobalt hover:underline">
                          {e.name}
                        </Link>
                        {e.parent_id && <span className="ml-1.5 text-xs text-ink-3">reproduction</span>}
                        {synthetic(e.dataset_version_id) && (
                          <span className="ml-1.5 align-middle">
                            <SyntheticTag />
                          </span>
                        )}
                      </td>
                      <td className="text-ink-2">{strategyLabel(e.strategy_key)}</td>
                      <td>
                        <StatusPill status={e.status} />
                      </td>
                      <td className={`r ${signClass(e.summary?.total_return)}`}>{pct(e.summary?.total_return)}</td>
                      <td className={`r ${signClass(e.summary?.cagr)}`}>{pct(e.summary?.cagr)}</td>
                      <td className="r">{pct(e.summary?.volatility)}</td>
                      <td className="r">{num(e.summary?.sharpe)}</td>
                      <td className="r">{pct(e.summary?.max_drawdown)}</td>
                      <td className="r">{multiple(e.summary?.annual_turnover, 1)}</td>
                      <td className="text-ink-2">{dateTime(e.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {exps.data && exps.data.total > PAGE && (
              <div className="mt-4 flex items-center justify-between text-sm">
                <span className="text-ink-2">
                  {offset + 1}–{Math.min(offset + PAGE, exps.data.total)} of {exps.data.total}
                </span>
                <div className="flex gap-2">
                  <button className="btn btn-quiet" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                    Previous
                  </button>
                  <button className="btn btn-quiet" disabled={offset + PAGE >= exps.data.total} onClick={() => setOffset(offset + PAGE)}>
                    Next
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </Panel>
    </>
  );
}
