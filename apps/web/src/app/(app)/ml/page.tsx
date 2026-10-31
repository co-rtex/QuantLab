"use client";

import { useState, type FormEvent } from "react";
import { PageHeader } from "@/components/Shell";
import { ErrorNote, JobProgress, Panel, Pending, QueryError, StatusPill, SyntheticLabel } from "@/components/ui";
import { api, ApiError, cancelJob, newIdempotencyKey, type DatasetVersion, type Job } from "@/lib/api";
import { dateTime, num, pct, shortHash } from "@/lib/format";
import { useSynthetic } from "@/lib/synthetic";
import { useApi } from "@/lib/useApi";
import { useCanEdit, useWorkspace } from "@/lib/workspace";

interface ModelRow {
  model: string;
  rows: number;
  auc: number | null;
  accuracy: number;
  brier: number;
  ic_mean: number | null;
  ic_t_stat: number | null;
  top_minus_bottom_quintile: number | null;
}
interface MLRun {
  id: string;
  name: string;
  dataset_version_id: string;
  is_synthetic?: boolean | null;
  status: Job["status"];
  created_at: string;
  config: Record<string, unknown>;
  input_hash: string;
  job: Job | null;
  summary?: ModelRow[];
  result?: {
    problem: string;
    // Results stored before #125 have no pipeline_version, volume_flat_rows or excluded_rows.
    // The keys of excluded_rows depend on the pipeline version (EXCLUSION_LABEL).
    pipeline_version?: number | null;
    panel: {
      rows: number;
      dates: number;
      symbols: number;
      hash: string;
      positive_rate: number;
      volume_flat_rows?: number | null;
      excluded_rows?: Record<string, number | null> | null;
    };
    splits: { fold: number; train_dates: number; test_start: string; test_end: string }[];
    summary: ModelRow[];
    feature_importance: Record<string, Record<string, number>>;
    limitations: string[];
  } | null;
}

const MODEL_LABEL: Record<string, string> = {
  majority: "Majority class (baseline)",
  momentum_rule: "12-1 momentum rank (baseline)",
  logistic: "Logistic regression",
  random_forest: "Random forest",
  gradient_boosting: "Gradient boosting",
};

/** A count the result may not have recorded: missing is "not recorded", never 0. */
function recorded(v: number | null | undefined): string {
  return typeof v === "number" && Number.isFinite(v) ? v.toLocaleString("en-US") : "not recorded";
}

/** Each excluded_rows key with its own label and explanation: the same words mean different
 *  counts in different pipeline versions, so the help follows the key, not the position. */
const EXCLUSION_LABEL: Record<string, [string, string]> = {
  // v2 (#125)
  untraded_21: ["Rows excluded: no trading", "Zero volume over its last 21 bars (its own trading days); counted only on dates that make the panel."],
  dates_below_10_after_untraded: ["Dates skipped: too few names", "Fewer than 10 names left on the date once untraded rows were removed."],
  // v3 (#142)
  zero_volume_bars: ["Zero-volume bars", "Dropped as no trade; features, labels and windows skip them."],
  dates_below_10_with_zero_volume: [
    "Dates skipped: too few names",
    "Fewer than 10 names on that date once zero-volume bars were dropped (a longer warm-up from dropped bars is not counted).",
  ],
};

function exclusionItems(ex: unknown): [string, string, string | null][] {
  // Absent (before #125) is "not recorded"; a present map shows exactly the keys it has.
  if (ex === null || typeof ex !== "object") return [["Rows excluded", "not recorded", "Results from before pipeline v2 did not record exclusions."]];
  const keys = Object.keys(ex);
  // Known keys in table order, then any key this page does not know, under its raw name.
  const ordered = [...Object.keys(EXCLUSION_LABEL).filter((k) => keys.includes(k)), ...keys.filter((k) => !(k in EXCLUSION_LABEL))];
  if (ordered.length === 0) return [["Rows excluded", "none", "This result lists no exclusion counts."]];
  return ordered.map((k) => {
    const v = (ex as Record<string, unknown>)[k];
    const [label, why] = EXCLUSION_LABEL[k] ?? [k, null];
    return [label, recorded(typeof v === "number" ? v : null), why];
  });
}

function PanelInputs({ res }: { res: NonNullable<MLRun["result"]> }) {
  const items: [string, string, string | null][] = [
    ["Pipeline version", res.pipeline_version == null ? "not recorded" : `v${res.pipeline_version}`, "How the panel was built; it changes whenever the same data and config could give a different panel."],
    ...exclusionItems(res.panel.excluded_rows),
    ["Rows with flat volume", recorded(res.panel.volume_flat_rows), "Kept: constant non-zero volume over its last 21 bars, flagged to the models as an input."],
  ];
  return (
    <section aria-labelledby="panel-construction">
      <h3 id="panel-construction" className="mb-2 text-sm font-semibold">
        Panel construction
      </h3>
      <dl className="grid grid-cols-1 gap-x-8 gap-y-3 rounded border border-rule bg-surface px-4 py-3 text-sm sm:grid-cols-2 xl:grid-cols-4">
        {items.map(([k, v, why], i) => (
          <div key={i}>
            <dt className="text-xs text-ink-2">{k}</dt>
            <dd className={v === "not recorded" ? "text-ink-2" : "num font-medium"}>{v}</dd>
            {why && <dd className="text-xs text-ink-2">{why}</dd>}
          </div>
        ))}
      </dl>
    </section>
  );
}

function RunDetail({ id }: { id: string }) {
  const run = useApi<MLRun>(`/ml-runs/${id}`, { poll: (r) => (r && (r.status === "queued" || r.status === "running") ? 1500 : false) });
  const [cancelErr, setCancelErr] = useState<ApiError>();
  const canEdit = useCanEdit();
  if (!run.data) return <Pending q={run} what="this ML run" />;
  const r = run.data;
  if (!r.result)
    return (
      <>
        <QueryError q={run} what="this ML run's status" />
        <ErrorNote error={cancelErr} />
        {r.job ? (
          <JobProgress
            job={r.job}
            onCancel={
              canEdit
                ? async () => {
                    setCancelErr(undefined);
                    await cancelJob(r.job!.id).catch((e) => setCancelErr(e as ApiError));
                    run.reload();
                  }
                : undefined
            }
          />
        ) : (
          <StatusPill status={r.status} />
        )}
      </>
    );
  const res = r.result;
  const base = res.summary.find((s) => s.model === "momentum_rule")?.auc ?? null;
  const fi = res.feature_importance.random_forest ?? res.feature_importance.logistic ?? {};
  const fiMax = Math.max(...Object.values(fi), 0.0001);
  return (
    <div className="space-y-5">
      <p className="text-sm text-ink-2">
        Target: {res.problem}. Panel of {res.panel.rows.toLocaleString()} observations on {res.panel.dates} dates and {res.panel.symbols} symbols (positive rate{" "}
        {pct(res.panel.positive_rate, 1)}), hash <span className="num">{shortHash(res.panel.hash, 12)}</span>.
      </p>
      <PanelInputs res={res} />
      <div className="overflow-x-auto" tabIndex={0}>
        <table className="data num">
          <thead>
            <tr>
              <th>Model</th>
              <th className="r">Out-of-sample AUC</th>
              <th className="r">vs momentum rule</th>
              <th className="r">Accuracy</th>
              <th className="r">Brier</th>
              <th className="r">Mean rank IC</th>
              <th className="r">IC t-stat</th>
              <th className="r">Top − bottom quintile</th>
            </tr>
          </thead>
          <tbody>
            {res.summary.map((s) => (
              <tr key={s.model}>
                <td>{MODEL_LABEL[s.model] ?? s.model}</td>
                <td className="r font-medium">{num(s.auc, 3)}</td>
                <td className="r">{s.auc !== null && base !== null && s.model !== "momentum_rule" ? num(s.auc - base, 3) : "—"}</td>
                <td className="r">{pct(s.accuracy, 1)}</td>
                <td className="r">{num(s.brier, 3)}</td>
                <td className="r">{num(s.ic_mean, 3)}</td>
                <td className="r">{num(s.ic_t_stat, 2)}</td>
                <td className="r">{pct(s.top_minus_bottom_quintile, 2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <section>
          <h3 className="mb-2 text-sm font-semibold">Chronological folds (purged, embargoed)</h3>
          <table className="data num">
            <thead>
              <tr>
                <th>Fold</th>
                <th className="r">Training dates</th>
                <th>Test window</th>
              </tr>
            </thead>
            <tbody>
              {res.splits.map((f) => (
                <tr key={f.fold}>
                  <td>{f.fold}</td>
                  <td className="r">{f.train_dates}</td>
                  <td>
                    {f.test_start} to {f.test_end}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
        <section>
          <h3 className="mb-2 text-sm font-semibold">Feature importance ({res.feature_importance.random_forest ? "random forest" : "logistic"})</h3>
          <ul className="space-y-1 text-sm">
            {Object.entries(fi)
              .sort((a, b) => b[1] - a[1])
              .map(([k, v]) => (
                <li key={k} className="grid grid-cols-[8rem_1fr_3.5rem] items-center gap-2">
                  <span className="text-ink-2">{k}</span>
                  <span className="h-2 rounded-sm bg-cobalt" style={{ width: `${(v / fiMax) * 100}%` }} aria-hidden />
                  <span className="num text-right">{num(v, 3)}</span>
                </li>
              ))}
          </ul>
        </section>
      </div>
      <ul className="list-disc space-y-1 pl-5 text-xs text-ink-3">
        {res.limitations.map((l) => (
          <li key={l}>{l}</li>
        ))}
      </ul>
    </div>
  );
}

export default function MLPage() {
  const { workspace } = useWorkspace();
  const ws = workspace?.id;
  const versions = useApi<DatasetVersion[]>(ws ? `/workspaces/${ws}/dataset-versions` : null);
  const [nonce, setNonce] = useState(0);
  const runs = useApi<MLRun[]>(ws ? `/workspaces/${ws}/ml-runs?n=${nonce}` : null, { poll: 4000 });
  const synthetic = useSynthetic(runs.data);
  const [selected, setSelected] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError>();
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!ws) return;
    const f = new FormData(e.currentTarget);
    setBusy(true);
    setErr(undefined);
    try {
      const r = await api<MLRun>(`/workspaces/${ws}/ml-runs`, {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey() },
        json: {
          name: f.get("name"),
          dataset_version_id: f.get("dataset"),
          config: { horizon: Number(f.get("horizon")), folds: Number(f.get("folds")), models: f.getAll("models") },
        },
      });
      setSelected(r.id);
      setNonce((n) => n + 1);
    } catch (x) {
      setErr(x as ApiError);
    }
    setBusy(false);
  }

  return (
    <>
      <PageHeader
        title="Machine-learning research"
        lede="Can features computed from past prices rank which assets will beat the cross-sectional median? Models are compared with simple baselines out of sample, using purged chronological folds."
      />
      <div className="grid gap-6 xl:grid-cols-[1fr_2.5fr]">
        <div className="space-y-6">
          <Panel title="New ML run">
            <QueryError q={versions} what="dataset versions" />
            <form onSubmit={submit} className="space-y-3 text-sm">
              <label className="block">
                <span className="mb-1 block">Name</span>
                <input name="name" required defaultValue="Cross-sectional direction" className="field" />
              </label>
              <label className="block">
                <span className="mb-1 block">Dataset version</span>
                <select name="dataset" className="field">
                  {versions.data?.map((v) => (
                    <option key={v.id} value={v.id}>
                      {shortHash(v.version_hash, 12)} · {v.symbol_count} symbols{v.is_synthetic ? " (synthetic)" : ""}
                    </option>
                  ))}
                </select>
              </label>
              <div className="grid grid-cols-2 gap-3">
                <label className="block">
                  <span className="mb-1 block">Horizon (sessions)</span>
                  <input name="horizon" type="number" min={5} max={126} defaultValue={21} className="field num" />
                </label>
                <label className="block">
                  <span className="mb-1 block">Folds</span>
                  <input name="folds" type="number" min={2} max={10} defaultValue={4} className="field num" />
                </label>
              </div>
              <fieldset>
                <legend className="mb-1">Models</legend>
                {Object.entries(MODEL_LABEL).map(([k, label]) => (
                  <label key={k} className="flex items-center gap-2">
                    <input type="checkbox" name="models" value={k} defaultChecked />
                    {label}
                  </label>
                ))}
              </fieldset>
              <ErrorNote error={err} />
              <button className="btn btn-primary" disabled={busy || !versions.data?.length}>
                {busy ? "Submitting…" : "Train and evaluate"}
              </button>
            </form>
          </Panel>
          <Panel title="Runs">
            <QueryError q={runs} what="ML runs" />
            {runs.data?.length === 0 && <p className="text-sm text-ink-2">No ML runs yet.</p>}
            <ul className="space-y-1 text-sm">
              {runs.data?.map((r) => (
                <li key={r.id}>
                  <button onClick={() => setSelected(r.id)} className={`flex w-full items-center justify-between gap-2 rounded px-2 py-1.5 text-left ${selected === r.id ? "bg-cobalt-soft" : "hover:bg-sunken"}`}>
                    <span>
                      <span className="block">
                        {r.name}
                        <SyntheticLabel inline status={synthetic(r)} />
                      </span>
                      <span className="block text-xs text-ink-2">{dateTime(r.created_at)}</span>
                    </span>
                    <StatusPill status={r.status} />
                  </button>
                </li>
              ))}
            </ul>
          </Panel>
        </div>
        <Panel
          title="Results"
          aside={<SyntheticLabel status={synthetic(runs.data?.find((r) => r.id === (selected ?? runs.data?.[0]?.id)))} />}
        >
          {selected ?? runs.data?.[0]?.id ? <RunDetail id={(selected ?? runs.data?.[0]?.id) as string} /> : <p className="text-sm text-ink-2">Train a model to see out-of-sample results here.</p>}
        </Panel>
      </div>
    </>
  );
}
