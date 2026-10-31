"use client";

import Link from "next/link";
import { useRef, useState, type FormEvent } from "react";
import { PageHeader } from "@/components/Shell";
import { ErrorNote, JobProgress, Panel, QueryError, Skeleton, StatusPill, SyntheticTag } from "@/components/ui";
import { api, ApiError, cancelJob, newIdempotencyKey, type Dataset, type DatasetVersion, type Job } from "@/lib/api";
import { compact, dateTime, shortHash } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { useCanEdit, useWorkspace } from "@/lib/workspace";

// The API's limit per uploaded file (MAX_UPLOAD_BYTES in routes/datasets.py).
const MAX_UPLOAD_BYTES = 50 * 2 ** 20;

function pollJob(j: Job | undefined): number | false {
  return j && (j.status === "queued" || j.status === "running") ? 1000 : false;
}

export default function DataPage() {
  const { workspace } = useWorkspace();
  const canEdit = useCanEdit();
  const ws = workspace?.id;
  // One job per form, so starting an import never hides a generation in progress (#114).
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useApi<Job>(jobId ? `/jobs/${jobId}` : null, { poll: pollJob });
  const [csvJobId, setCsvJobId] = useState<string | null>(null);
  const csvJob = useApi<Job>(csvJobId ? `/jobs/${csvJobId}` : null, { poll: pollJob });
  // Re-fetch the catalog once an ingestion job settles by keying the request on it.
  const settledOf = (j: Job | undefined) => (j && !["queued", "running"].includes(j.status) ? j.id : "");
  const settled = [settledOf(job.data), settledOf(csvJob.data)].filter(Boolean).join("-");
  const datasets = useApi<Dataset[]>(ws ? `/workspaces/${ws}/datasets?after=${settled}` : null);
  const versions = useApi<DatasetVersion[]>(ws ? `/workspaces/${ws}/dataset-versions?after=${settled}` : null);
  const [err, setErr] = useState<ApiError>();
  const [submitting, setSubmitting] = useState(false);
  const [importErr, setImportErr] = useState<ApiError | Error>();
  // The API's field errors (422), by field, shown at each input as well as in the alert.
  const fieldErrors = Object.fromEntries(
    importErr instanceof ApiError && Array.isArray(importErr.details)
      ? (importErr.details as { loc?: (string | number)[]; msg: string }[]).map((d) => [String((d.loc ?? []).at(-1)), d.msg])
      : [],
  ) as Record<string, string>;
  /** aria attributes tying an import input to its field error, if it has one. */
  const invalid = (field: string, hint?: string) => ({
    "aria-invalid": fieldErrors[field] ? true : undefined,
    "aria-describedby": [hint, fieldErrors[field] ? `csv-${field}-error` : undefined].filter(Boolean).join(" ") || undefined,
  });
  const fieldError = (field: string) =>
    fieldErrors[field] ? (
      <span id={`csv-${field}-error`} className="mt-0.5 block text-xs text-loss">
        {fieldErrors[field]}
      </span>
    ) : null;
  const [importing, setImporting] = useState(false);
  const importingNow = useRef(false);

  /**
   * Upload a CSV dataset to the streaming endpoint (#4, #28) as it is: the browser never
   * parses the files. Only their sizes are checked first, against the API's limit, so an
   * oversized file is refused before it is sent rather than after 50 MB went up.
   */
  async function importCsv(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!ws || importingNow.current) return;
    const form = new FormData(e.currentTarget);
    setImportErr(undefined);
    for (const field of ["bars", "actions"]) {
      const file = form.get(field);
      if (!(file instanceof File) || file.size === 0) {
        form.delete(field); // an empty optional file input
        continue;
      }
      if (file.size > MAX_UPLOAD_BYTES) {
        setImportErr(new Error(`${file.name} is ${(file.size / 2 ** 20).toFixed(1)} MB. Each file is limited to ${MAX_UPLOAD_BYTES / 2 ** 20} MB.`));
        return;
      }
    }
    if (!form.has("bars")) {
      setImportErr(new Error("Choose the daily bars CSV to import."));
      return;
    }
    importingNow.current = true;
    setImporting(true);
    try {
      const j = await api<Job>(`/workspaces/${ws}/datasets/csv`, { method: "POST", body: form });
      setCsvJobId(j.id);
    } catch (x) {
      setImportErr(x as ApiError);
    } finally {
      importingNow.current = false;
      setImporting(false);
    }
  }
  const busy = submitting || (jobId !== null && !settledOf(job.data));

  async function cancel(q: typeof job, report: (e: ApiError) => void) {
    if (!q.data) return;
    try {
      await cancelJob(q.data.id);
    } catch (e) {
      report(e as ApiError);
    }
    q.reload();
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
        <div className="space-y-6">
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
                <JobProgress job={job.data} onCancel={canEdit ? () => cancel(job, setErr) : undefined} />
              </div>
            )}
          </form>
        </Panel>
        {canEdit && (
          <Panel title="Import CSV">
            <form onSubmit={importCsv} className="space-y-3 text-sm">
              <p className="text-ink-2">
                Daily bars with the columns <code>symbol,date,open,high,low,close,volume</code>, and optionally corporate actions with{" "}
                <code>symbol,ex_date,kind,value</code> (kind is split or dividend). Each file can be up to {MAX_UPLOAD_BYTES / 2 ** 20} MB. The
                source and licence you state are kept with the dataset.
              </p>
              <div className="grid grid-cols-2 gap-3">
                <label>
                  <span className="mb-1 block">Name</span>
                  <input name="name" required maxLength={120} className="field" {...invalid("name")} />
                  {fieldError("name")}
                </label>
                <label>
                  <span className="mb-1 block">Short name</span>
                  <input name="slug" required pattern="[a-z0-9][a-z0-9_\-]{1,62}" title="Lowercase letters, digits, - and _" className="field" {...invalid("slug", "csv-slug-hint")} />
                  <span id="csv-slug-hint" className="mt-0.5 block text-xs text-ink-3">
                    Lowercase letters, digits, - and _
                  </span>
                  {fieldError("slug")}
                </label>
                <label className="col-span-2">
                  <span className="mb-1 block">Source</span>
                  <input name="source" required minLength={3} maxLength={300} placeholder="Where the data comes from" className="field" {...invalid("source")} />
                  {fieldError("source")}
                </label>
                <label className="col-span-2">
                  <span className="mb-1 block">Licence</span>
                  <input name="license" required minLength={3} maxLength={300} placeholder="The terms you may use it under" className="field" {...invalid("license")} />
                  {fieldError("license")}
                </label>
                <label className="col-span-2">
                  <span className="mb-1 block">Daily bars (CSV)</span>
                  <input name="bars" type="file" required accept=".csv,text/csv" className="field" {...invalid("bars")} />
                  {fieldError("bars")}
                </label>
                <label className="col-span-2">
                  <span className="mb-1 block">Corporate actions (CSV, optional)</span>
                  <input name="actions" type="file" accept=".csv,text/csv" className="field" {...invalid("actions")} />
                  {fieldError("actions")}
                </label>
              </div>
              <ErrorNote
                error={importErr}
                detail={
                  importErr instanceof ApiError && Array.isArray(importErr.details) ? (
                    <ul className="mt-1 list-disc pl-5">
                      {(importErr.details as { loc?: (string | number)[]; msg: string }[]).map((d, i) => (
                        <li key={i}>
                          {(d.loc ?? []).filter((l) => l !== "body").join(".") || "request"}: {d.msg}
                        </li>
                      ))}
                    </ul>
                  ) : undefined
                }
              />
              <button className="btn btn-primary" aria-busy={importing} aria-disabled={importing || !ws}>
                {importing ? "Uploading…" : "Import CSV"}
              </button>
              <QueryError q={csvJob} what="the import job's status" />
              {csvJob.data && (
                <div className="pt-2">
                  <JobProgress job={csvJob.data} onCancel={canEdit ? () => cancel(csvJob, setImportErr) : undefined} />
                </div>
              )}
            </form>
          </Panel>
        )}
        </div>
      </div>
    </>
  );
}
