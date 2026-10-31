"use client";

import type { ReactNode } from "react";
import type { ApiError, Job } from "@/lib/api";
import { DASH } from "@/lib/format";

const STATUS_STYLE: Record<string, string> = {
  completed: "bg-gain-soft text-gain",
  pass: "bg-gain-soft text-gain",
  verified: "bg-gain-soft text-gain",
  running: "bg-cobalt-soft text-cobalt",
  queued: "bg-sunken text-ink-2",
  warn: "bg-caution-soft text-caution",
  warning: "bg-caution-soft text-caution",
  unassessed: "bg-sunken text-ink-2",
  failed: "bg-loss-soft text-loss",
  fail: "bg-loss-soft text-loss",
  timed_out: "bg-loss-soft text-loss",
  cancelled: "bg-sunken text-ink-2",
};

const STATUS_TEXT: Record<string, string> = {
  timed_out: "Timed out",
  pass: "Passed",
  fail: "Failed",
  warn: "Warnings",
};

export function StatusPill({ status }: { status: string }) {
  const label = STATUS_TEXT[status] ?? status.charAt(0).toUpperCase() + status.slice(1).replace("_", " ");
  return (
    <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${STATUS_STYLE[status] ?? "bg-sunken"}`}>
      {status === "running" && <span className="mr-1 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-cobalt" aria-hidden />}
      {label}
    </span>
  );
}

export function Panel({ title, aside, children, className = "" }: { title?: ReactNode; aside?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`panel ${className}`}>
      {(title || aside) && (
        <div className="flex items-baseline justify-between gap-3 border-b border-rule px-4 py-2.5">
          {title && <h2 className="text-sm font-semibold">{title}</h2>}
          {aside && <div className="text-xs text-ink-2">{aside}</div>}
        </div>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function ErrorNote({ error, action }: { error: ApiError | Error | undefined; action?: ReactNode }) {
  if (!error) return null;
  const rid = (error as ApiError).requestId;
  return (
    <div role="alert" className="rounded border border-loss/30 bg-loss-soft px-3 py-2 text-sm text-loss">
      <p>{error.message}</p>
      {rid && <p className="mt-0.5 text-xs">Request {rid}</p>}
      {action}
    </div>
  );
}

/** The parts of a useApi() result the error components need. */
export interface Query {
  data?: unknown;
  error?: ApiError;
  reload: () => void;
}

/**
 * A failed request, shown where its data would be, with a way to try again (#18). Panels
 * used to render nothing, or a skeleton forever, when the API answered 5xx. If an earlier
 * response is still on screen, the note says so instead of hiding the failure.
 */
export function QueryError({ q, what = "this data" }: { q: Query; what?: string }) {
  if (!q.error) return null;
  const stale = q.data !== undefined;
  return (
    <div className="mb-3">
      <ErrorNote
        error={q.error}
        action={
          <p className="mt-1 flex flex-wrap items-center gap-2">
            <span>{stale ? `Showing the last ${what} that loaded.` : `Could not load ${what}.`}</span>
            <button type="button" className="btn btn-quiet py-0.5" onClick={q.reload}>
              Retry
            </button>
          </p>
        }
      />
    </div>
  );
}

/** The placeholder for data that has not arrived: a skeleton while loading, an error with Retry once it failed. */
export function Pending({ q, h, what }: { q: Query; h?: string; what?: string }) {
  return q.error ? <QueryError q={q} what={what} /> : <Skeleton h={h} />;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded border border-dashed border-rule px-6 py-10 text-center">
      <p className="font-medium">{title}</p>
      {children && <div className="mx-auto mt-2 max-w-md text-sm text-ink-2">{children}</div>}
    </div>
  );
}

export function Skeleton({ h = "h-40" }: { h?: string }) {
  return <div className={`${h} animate-pulse rounded bg-sunken`} role="status" aria-busy="true" aria-label="Loading" />;
}

export function JobProgress({ job, onCancel }: { job: Job; onCancel?: () => void }) {
  const active = job.status === "queued" || job.status === "running";
  return (
    <div className="space-y-2" aria-live="polite">
      <div className="flex items-center justify-between gap-3 text-sm">
        <div className="flex items-center gap-2">
          <StatusPill status={job.status} />
          <span className="text-ink-2">{job.message || DASH}</span>
        </div>
        {active && onCancel && (
          <button className="btn btn-quiet py-1 text-xs" onClick={onCancel}>
            Cancel run
          </button>
        )}
      </div>
      <div
        className="h-1.5 overflow-hidden rounded bg-sunken"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(job.progress * 100)}
      >
        <div className={`h-full transition-[width] duration-500 ${job.status === "failed" || job.status === "timed_out" ? "bg-loss" : "bg-cobalt"}`} style={{ width: `${Math.max(2, job.progress * 100)}%` }} />
      </div>
      {job.error && (
        <p className="text-xs text-loss">
          {job.error.type}: {job.error.message}
        </p>
      )}
      <p className="text-xs text-ink-3">
        Attempt {job.attempts} of {job.max_attempts}
      </p>
    </div>
  );
}

export function SyntheticTag() {
  return (
    <span
      className="inline-flex items-center rounded border border-caution/40 bg-caution-soft px-1.5 py-0.5 text-xs font-medium text-caution"
      title="Generated by the deterministic synthetic provider. Not real market observations."
    >
      Synthetic data
    </span>
  );
}
