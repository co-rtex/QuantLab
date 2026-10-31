"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { PageHeader } from "@/components/Shell";
import { ErrorNote, JobProgress, Panel, QueryError, Skeleton, StatusPill, SyntheticTag } from "@/components/ui";
import { api, ApiError, cancelJob, newIdempotencyKey, type Dataset, type DatasetVersion, type Job } from "@/lib/api";
import { compact, dateTime, shortHash } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { useWorkspace } from "@/lib/workspace";

export default function DataPage() {
  const { workspace } = useWorkspace();
  const ws = workspace?.id;
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useApi<Job>(jobId ? `/jobs/${jobId}` : null, {
    poll: (j) => (j && (j.status === "queued" || j.status === "running") ? 1000 : false),
  });
  // Re-fetch the catalog once the ingestion job settles by keying the request on it.
  const settled = job.data && !["queued", "running"].includes(job.data.status) ? job.data.id : "";
  const datasets = useApi<Dataset[]>(ws ? `/workspaces/${ws}/datasets?after=${settled}` : null);
  const versions = useApi<DatasetVersion[]>(ws ? `/workspaces/${ws}/dataset-versions?after=${settled}` : null);
  const [err, setErr] = useState<ApiError>();
  const [submitting, setSubmitting] = useState(false);
  const busy = submitting || (jobId !== null && !settled);

  async function cancel() {
    if (!job.data) return;
    setErr(undefined);
    try {
      await cancelJob(job.data.id);
    } catch (e) {
      setErr(e as ApiError);
    }
    job.reload();
  }

  async function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!ws) return;
    const f = new FormData(e.currentTarget);
    setErr(undefined);
    setSubmitting(true);
    try {
      const j = await api<Job>(`/workspaces/${ws}/datasets/synthetic`, {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey() },
        json: {
          slug: f.get("slug"),
          name: f.get("name"),
          n_assets: Number(f.get("n_assets")),
          start: f.get("start"),
          end: f.get("end"),
          seed: Number(f.get("seed")),
        },
      });
      setJobId(j.id);
    } catch (e) {
      setErr(e as ApiError);
    }
    setSubmitting(false);
  }

  const byDataset = (id: string) => versions.data?.filter((v) => v.dataset_id === id) ?? [];

  return (
    <>
      <PageHeader
        title="Market data"
        lede="Each ingestion produces an immutable, content-addressed dataset version with a quality report. Experiments always reference an exact version."
      />
      <div className="grid gap-6 xl:grid-cols-[2fr_1fr]">
        <Panel title="Datasets">
          <QueryError q={datasets} what="datasets" />
          <QueryError q={versions} what="dataset versions" />
          {datasets.loading && !datasets.data ? (
            <Skeleton />
          ) : datasets.error && !datasets.data ? null : datasets.data?.length === 0 ? (
            <p className="text-sm text-ink-2">No datasets yet. Generate one with the form on this page.</p>
          ) : (
            <div className="space-y-6">
              {datasets.data?.map((d) => (
                <div key={d.id}>
                  <div className="flex flex-wrap items-baseline gap-2">
                    <h3 className="font-serif text-lg font-semibold">{d.name}</h3>
                    <span className="text-xs text-ink-2">{d.slug}</span>
                    {d.provider === "synthetic" && <SyntheticTag />}
                  </div>
                  <p className="mt-0.5 max-w-prose text-sm text-ink-2">{d.description}</p>
                  <div className="mt-3 overflow-x-auto" tabIndex={0}>
                    <table className="data num">
                      <thead>
                        <tr>
                          <th>Version</th>
                          <th className="r">Rows</th>
                          <th className="r">Symbols</th>
                          <th>Coverage</th>
                          <th>Quality</th>
                          <th>Created</th>
                        </tr>
                      </thead>
                      <tbody>
                        {byDataset(d.id).map((v) => (
                          <tr key={v.id}>
                            <td>
                              <Link href={`/data/${v.id}`} className="text-cobalt hover:underline">
                                {shortHash(v.version_hash, 12)}
                              </Link>
                            </td>
                            <td className="r">{compact(v.rows)}</td>
                            <td className="r">{v.symbol_count}</td>
                            <td>
                              {v.start_date} to {v.end_date}
                            </td>
                            <td>
                              <StatusPill status={v.quality_status} />
                            </td>
                            <td className="text-ink-2">{dateTime(v.created_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Panel>
        <Panel title="Generate synthetic data">
          <form onSubmit={create} className="space-y-3 text-sm">
            <p className="text-ink-2">
              Deterministic for a given seed. Includes market regimes, sector structure, splits, dividends, late
              listings and delistings. Always labelled synthetic.
            </p>
            <div className="grid grid-cols-2 gap-3">
              <label className="col-span-2">
                <span className="mb-1 block">Name</span>
                <input name="name" required defaultValue="Synthetic US equities" className="field" />
              </label>
              <label>
                <span className="mb-1 block">Identifier</span>
                <input name="slug" required pattern="[a-z0-9][a-z0-9_\-]{1,62}" defaultValue="synthetic-us" className="field" />
              </label>
              <label>
                <span className="mb-1 block">Assets</span>
                <input name="n_assets" type="number" min={2} max={2000} defaultValue={100} className="field" />
              </label>
              <label>
                <span className="mb-1 block">First session</span>
                <input name="start" type="date" defaultValue="2018-01-02" className="field" />
              </label>
              <label>
                <span className="mb-1 block">Last session</span>
                <input name="end" type="date" defaultValue="2023-12-29" className="field" />
              </label>
              <label>
                <span className="mb-1 block">Seed</span>
                <input name="seed" type="number" min={0} defaultValue={7} className="field" />
              </label>
            </div>
            <ErrorNote error={err} />
            <button className="btn btn-primary" disabled={busy || !ws}>
              {busy ? "Generating…" : "Generate dataset version"}
            </button>
            <QueryError q={job} what="the ingestion job's status" />
            {job.data && (
              <div className="pt-2">
                <JobProgress job={job.data} onCancel={cancel} />
              </div>
            )}
          </form>
        </Panel>
      </div>
    </>
  );
}
