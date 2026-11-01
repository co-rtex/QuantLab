"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";
import { SERIES } from "@/components/charts";
import { PageHeader } from "@/components/Shell";
import { ErrorNote, JobProgress, Panel, QueryError, Skeleton, SyntheticTag } from "@/components/ui";
import { api, ApiError, cancelJob, newIdempotencyKey, type DatasetVersion, type Job } from "@/lib/api";
import { num, pct, shortHash } from "@/lib/format";
import { useInstruments } from "@/lib/instruments";
import { useApi } from "@/lib/useApi";
import { useCanEdit, useWorkspace } from "@/lib/workspace";

interface Result {
  method: string;
  error?: string;
  weights?: Record<string, number>;
  expected_volatility?: number;
  historical_mean_return?: number;
  beta?: number | null;
  risk_share?: Record<string, number>;
  concentration?: { effective_n: number | null; top1: number | null };
}
interface Response {
  as_of: string;
  window_start: string;
  observations: number;
  symbols: string[];
  excluded_for_history: string[];
  is_synthetic: boolean;
  results: Result[];
  notes: string[];
}
/** A queued or finished optimization (#224): `result` once its job has completed. */
interface OptimizationRun {
  id: string;
  dataset_version_id: string;
  status: Job["status"];
  result: Response | null;
  job: Job | null;
}

const LABEL: Record<string, string> = {
  equal_weight: "Equal weight",
  min_variance: "Minimum variance",
  mean_variance: "Mean-variance",
  risk_parity: "Risk parity",
};

function pollRun(r: OptimizationRun | undefined): number | false {
  return r && (r.status === "queued" || r.status === "running") ? 1000 : false;
}

export default function OptimizePage() {
  return (
    <>
      <PageHeader
        title="Portfolio construction"
        lede="Compare allocation methods on the same assets using only returns available up to a chosen date. Expected volatility is estimated from that history and is usually lower than what follows."
      />
      <Suspense fallback={<Skeleton h="h-96" />}>
        <Optimizer />
      </Suspense>
    </>
  );
}

function Optimizer() {
  const router = useRouter();
  // The run is in the URL, so a reload or a shared link finds it again.
  const runId = useSearchParams().get("run");
  const { workspace } = useWorkspace();
  const canEdit = useCanEdit();
  const ws = workspace?.id;
  const versions = useApi<DatasetVersion[]>(ws ? `/workspaces/${ws}/dataset-versions` : null);
  const [dv, setDv] = useState("");
  const dvId = dv || versions.data?.[0]?.id || null;
  const version = versions.data?.find((v) => v.id === dvId);
  const inst = useInstruments(dvId);
  const assets = inst.data?.items.filter((i) => i.asset_class !== "index_fund") ?? [];
  const index = inst.data?.items.find((i) => i.asset_class === "index_fund")?.symbol;
  const run = useApi<OptimizationRun>(runId ? `/optimizations/${runId}` : null, { poll: pollRun });
  const res = run.data?.status === "completed" ? (run.data.result ?? undefined) : undefined;
  const active = run.data?.status === "queued" || run.data?.status === "running";
  // An error belongs to the run it came from: none is carried to another ?run=.
  const [failure, setFailure] = useState<{ run: string | null; error: ApiError }>();
  const err = failure && failure.run === runId ? failure.error : undefined;
  const setErr = (error: ApiError) => setFailure({ run: runId, error });
  // A submission is pending from its click until the URL leaves the run it was made from
  // (null: none). The button then stays disabled through the new run's first load, and no
  // second click queues a second job; opening any other run ends the wait.
  const [submittedFrom, setSubmittedFrom] = useState<string | null>();
  // Ended once the URL has moved on (React's adjust-state-while-rendering pattern), so
  // going back to that run later doesn't disable the button again.
  if (submittedFrom !== undefined && submittedFrom !== runId) setSubmittedFrom(undefined);
  const pending = submittedFrom !== undefined || run.loading || active;

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const text = String(f.get("symbols") ?? "").trim();
    const symbols = text ? text.split(/[\s,]+/).filter(Boolean) : assets.slice(0, 12).map((a) => a.symbol);
    setSubmittedFrom(runId);
    setFailure(undefined);
    try {
      const queued = await api<OptimizationRun>(`/dataset-versions/${dvId}/optimizations`, {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey() },
        json: {
          symbols,
          as_of: f.get("as_of") || null,
          lookback: Number(f.get("lookback")),
          max_weight: Number(f.get("max_weight")) / 100,
          covariance: f.get("covariance"),
          benchmark: index ?? null,
        },
      });
      router.replace(`/optimize?run=${queued.id}`);
    } catch (x) {
      setErr(x as ApiError);
      setSubmittedFrom(undefined);
    }
  }

  async function cancel() {
    if (!run.data?.job) return;
    try {
      await cancelJob(run.data.job.id);
    } catch (x) {
      setErr(x as ApiError);
    }
    run.reload();
  }

  const ok = res?.results.filter((r) => r.weights) ?? [];
  return (
    <div className="grid gap-6 xl:grid-cols-[1fr_2.5fr]">
      <Panel title="Inputs">
        <QueryError q={versions} what="dataset versions" />
        <QueryError q={inst} what="instruments" />
        <form onSubmit={submit} className="space-y-3 text-sm">
          <label className="block">
            <span className="mb-1 block">Dataset version</span>
            <select className="field" value={dvId ?? ""} onChange={(e) => setDv(e.target.value)}>
              {versions.data?.map((v) => (
                <option key={v.id} value={v.id}>
                  {shortHash(v.version_hash, 12)}
                  {v.is_synthetic ? " (synthetic)" : ""}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="mb-1 block">Symbols</span>
            <textarea name="symbols" rows={3} className="field" placeholder={`First 12: ${assets.slice(0, 12).map((a) => a.symbol).join(", ")}`} />
          </label>
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="mb-1 block">As of</span>
              <input name="as_of" type="date" min={version?.start_date} max={version?.end_date} className="field" />
            </label>
            <label className="block">
              <span className="mb-1 block">Look-back (sessions)</span>
              <input name="lookback" type="number" min={40} max={1260} defaultValue={252} className="field num" />
            </label>
            <label className="block">
              <span className="mb-1 block">Max weight (%)</span>
              <input name="max_weight" type="number" min={1} max={100} defaultValue={30} className="field num" />
            </label>
            <label className="block">
              <span className="mb-1 block">Covariance</span>
              <select name="covariance" className="field" defaultValue="ledoit_wolf">
                <option value="ledoit_wolf">Ledoit-Wolf shrinkage</option>
                <option value="sample">Sample</option>
              </select>
            </label>
          </div>
          <ErrorNote error={err} />
          {/* Disabled while a submission or a run is pending (a new run's first load
              included, or a second click queued a second job), and before the instruments
              load: the default symbols are their first 12, and a click before then sent
              "symbols": [] and got 422 "Request validation failed" (seen in #232's CI).
              Viewers read results only: a run writes a job and its result (#224). */}
          <button
            className="btn btn-primary"
            disabled={!canEdit || pending || !dvId || !inst.data}
            aria-describedby={workspace && !canEdit ? "optimize-viewer-reason" : undefined}
          >
            {pending ? "Optimizing…" : "Compare allocations"}
          </button>
          {workspace && !canEdit && (
            <p id="optimize-viewer-reason" className="text-xs text-ink-2">
              Viewers can read optimization results. Running one needs the editor role; ask a workspace owner.
            </p>
          )}
        </form>
      </Panel>
      <Panel title="Allocations" aside={res ? `${res.window_start} to ${res.as_of}, ${res.observations} sessions` : undefined}>
        <QueryError q={run} what="the optimization" />
        {/* Its progress, and a failed run's message (JobProgress shows the job's error). */}
        {run.data?.job && run.data.status !== "completed" && (
          <div className="mb-4">
            <JobProgress job={run.data.job} onCancel={canEdit ? cancel : undefined} />
          </div>
        )}
        {!res ? (
          !runId && <p className="text-sm text-ink-2">Choose assets and a date to compare equal weight, minimum variance, mean-variance and risk parity.</p>
        ) : (
          <div className="space-y-4">
            {res.is_synthetic && <SyntheticTag />}
            <div className="overflow-x-auto" tabIndex={0}>
              <table className="data num">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    {ok.map((r, i) => (
                      <th key={r.method} className="r">
                        <span className="mr-1 inline-block h-2 w-2 rounded-full align-middle" style={{ background: SERIES[i] }} aria-hidden />
                        {LABEL[r.method]}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {res.symbols.map((s) => (
                    <tr key={s}>
                      <td className="font-medium">{s}</td>
                      {ok.map((r) => (
                        <td key={r.method} className="r">
                          {pct(r.weights?.[s] ?? 0, 1)}
                          <span className="ml-1 text-xs text-ink-3">({pct(r.risk_share?.[s] ?? 0, 0)} risk)</span>
                        </td>
                      ))}
                    </tr>
                  ))}
                  {[
                    ["Expected volatility", (r: Result) => pct(r.expected_volatility, 1)],
                    ["Historical mean return", (r: Result) => pct(r.historical_mean_return, 1)],
                    ["Beta to benchmark", (r: Result) => num(r.beta)],
                    ["Effective number of assets", (r: Result) => num(r.concentration?.effective_n, 1)],
                  ].map(([label, f]) => (
                    <tr key={label as string} className="font-medium">
                      <td>{label as string}</td>
                      {ok.map((r) => (
                        <td key={r.method} className="r">
                          {(f as (r: Result) => string)(r)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {res.results
              .filter((r) => r.error)
              .map((r) => (
                <p key={r.method} className="text-sm text-loss">
                  {LABEL[r.method]}: {r.error}
                </p>
              ))}
            {res.excluded_for_history.length > 0 && <p className="text-xs text-ink-2">Excluded for insufficient history: {res.excluded_for_history.join(", ")}.</p>}
            <ul className="list-disc space-y-1 pl-5 text-xs text-ink-3">
              {res.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          </div>
        )}
      </Panel>
    </div>
  );
}
