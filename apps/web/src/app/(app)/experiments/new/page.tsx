"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState, type FormEvent } from "react";
import { PageHeader } from "@/components/Shell";
import { ErrorNote, Panel, QueryError, Skeleton } from "@/components/ui";
import {
  api,
  ApiError,
  newIdempotencyKey,
  type DatasetVersion,
  type Experiment,
  type Instrument,
  type JsonSchemaProp,
  type Page,
  type StrategyCatalogEntry,
} from "@/lib/api";
import { shortHash } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import { useWorkspace } from "@/lib/workspace";

function ParamField({ name, prop, value, onChange }: { name: string; prop: JsonSchemaProp; value: unknown; onChange: (v: unknown) => void }) {
  const label = prop.title ?? name.replaceAll("_", " ");
  const id = `param-${name}`;
  if (prop.enum) {
    return (
      <label htmlFor={id} className="block">
        <span className="mb-1 block capitalize">{label}</span>
        <select id={id} className="field" value={String(value ?? prop.default ?? "")} onChange={(e) => onChange(e.target.value)}>
          {prop.enum.map((o) => (
            <option key={o}>{o}</option>
          ))}
        </select>
      </label>
    );
  }
  if (prop.type === "integer" || prop.type === "number") {
    const min = prop.minimum ?? prop.exclusiveMinimum;
    return (
      <label htmlFor={id} className="block">
        <span className="mb-1 block capitalize">{label}</span>
        <input
          id={id}
          type="number"
          className="field num"
          step={prop.type === "integer" ? 1 : "any"}
          min={min}
          max={prop.maximum}
          value={value === undefined ? String(prop.default ?? "") : String(value)}
          onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
        />
        {(min !== undefined || prop.maximum !== undefined) && (
          <span className="mt-0.5 block text-xs text-ink-3">
            {min ?? "−∞"} to {prop.maximum ?? "∞"}
          </span>
        )}
      </label>
    );
  }
  return null; // complex params (e.g. explicit weights) use defaults in the builder
}

function Builder() {
  const router = useRouter();
  const params = useSearchParams();
  const { workspace } = useWorkspace();
  const ws = workspace?.id;
  const catalog = useApi<StrategyCatalogEntry[]>("/strategies/catalog");
  const versions = useApi<DatasetVersion[]>(ws ? `/workspaces/${ws}/dataset-versions` : null);
  const [dv, setDv] = useState<string | null>(params.get("dataset"));
  const dvId = dv ?? versions.data?.[0]?.id ?? null;
  const inst = useApi<Page<Instrument>>(dvId ? `/dataset-versions/${dvId}/instruments?limit=2000` : null);
  const [key, setKey] = useState(params.get("strategy") ?? "momentum");
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [err, setErr] = useState<ApiError>();
  const [busy, setBusy] = useState(false);
  const entry = catalog.data?.find((c) => c.key === key);
  const indexSymbols = useMemo(() => inst.data?.items.filter((i) => i.asset_class === "index_fund").map((i) => i.symbol) ?? [], [inst.data]);
  const version = versions.data?.find((v) => v.id === dvId);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!ws || !dvId) return;
    const f = new FormData(e.currentTarget);
    const universeText = String(f.get("universe") ?? "").trim();
    const universe = universeText
      ? universeText.split(/[\s,]+/).filter(Boolean).map((s) => s.toUpperCase())
      : inst.data?.items.filter((i) => i.asset_class !== "index_fund").map((i) => i.symbol);
    const benchmark = String(f.get("benchmark") ?? "");
    const config = {
      start: f.get("start") || null,
      end: f.get("end") || null,
      initial_capital: Number(f.get("initial_capital")),
      rebalance: f.get("rebalance"),
      execution: f.get("execution"),
      commission_bps: Number(f.get("commission_bps")),
      slippage_bps: Number(f.get("slippage_bps")),
      max_weight: Number(f.get("max_weight")) / 100,
      benchmark: benchmark || null,
      universe,
      risk_free_rate: Number(f.get("risk_free_rate")) / 100,
    };
    const strategy_params = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== undefined));
    setBusy(true);
    setErr(undefined);
    try {
      const res = await api<{ experiment: Experiment }>(`/workspaces/${ws}/experiments`, {
        method: "POST",
        headers: { "Idempotency-Key": newIdempotencyKey() },
        json: { name: f.get("name"), dataset_version_id: dvId, strategy_key: key, strategy_params, config, notes: f.get("notes") ?? "" },
      });
      router.push(`/experiments/${res.experiment.id}`);
    } catch (e) {
      setErr(e as ApiError);
      setBusy(false);
    }
  }

  if (catalog.loading || versions.loading) return <Skeleton h="h-96" />;
  // Without the strategy catalog or the dataset versions there is nothing to configure.
  if (!catalog.data || !versions.data) {
    if (!catalog.error && !versions.error) return <Skeleton h="h-96" />;
    return (
      <>
        <QueryError q={catalog} what="the strategy catalog" />
        <QueryError q={versions} what="dataset versions" />
      </>
    );
  }
  if (versions.data?.length === 0) {
    return <ErrorNote error={new Error("Add a dataset version under Market data before creating an experiment.")} />;
  }
  const details = err?.details as { loc?: (string | number)[]; msg: string }[] | undefined;

  return (
    <form onSubmit={submit} className="grid gap-6 text-sm xl:grid-cols-[3fr_2fr]">
      <div className="space-y-6">
        <QueryError q={inst} what="the instrument list" />
        <Panel title="Strategy">
          <fieldset>
            <legend className="sr-only">Strategy</legend>
            <div className="grid gap-2 sm:grid-cols-2">
              {catalog.data?.map((c) => (
                <label key={c.key} className={`cursor-pointer rounded border px-3 py-2.5 ${c.key === key ? "border-cobalt bg-cobalt-soft" : "border-rule hover:bg-sunken"}`}>
                  <input type="radio" name="strategy" value={c.key} checked={c.key === key} onChange={() => { setKey(c.key); setValues({}); }} className="sr-only" />
                  <span className="block font-medium">{c.name}</span>
                  <span className="mt-0.5 block text-xs leading-snug text-ink-2">{c.description}</span>
                </label>
              ))}
            </div>
          </fieldset>
          {entry && Object.keys(entry.params_schema.properties ?? {}).length > 0 && (
            <div className="mt-5 grid gap-4 sm:grid-cols-3">
              {Object.entries(entry.params_schema.properties ?? {}).map(([name, prop]) => (
                <ParamField key={name} name={name} prop={prop} value={values[name]} onChange={(v) => setValues((s) => ({ ...s, [name]: v }))} />
              ))}
            </div>
          )}
        </Panel>
        <Panel title="Execution assumptions">
          <div className="grid gap-4 sm:grid-cols-3">
            <label className="block">
              <span className="mb-1 block">Rebalance</span>
              <select name="rebalance" defaultValue="monthly" className="field">
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
                <option value="quarterly">Quarterly</option>
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block">Fill timing</span>
              <select name="execution" defaultValue="next_open" className="field">
                <option value="next_open">Next session open</option>
                <option value="next_close">Next session close</option>
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block">Initial capital (USD)</span>
              <input name="initial_capital" type="number" min={1000} step={1000} defaultValue={1000000} className="field num" />
            </label>
            <label className="block">
              <span className="mb-1 block">Commission (bps)</span>
              <input name="commission_bps" type="number" min={0} max={500} step="0.1" defaultValue={1} className="field num" />
            </label>
            <label className="block">
              <span className="mb-1 block">Slippage (bps)</span>
              <input name="slippage_bps" type="number" min={0} max={500} step="0.1" defaultValue={5} className="field num" />
            </label>
            <label className="block">
              <span className="mb-1 block">Max position (%)</span>
              <input name="max_weight" type="number" min={1} max={100} defaultValue={20} className="field num" />
            </label>
          </div>
          <p className="mt-3 text-xs text-ink-3">
            Signals use data up to each rebalance close; orders fill in the following session. Buys that exceed available cash are reduced, and orders without a price on the fill date are rejected.
          </p>
        </Panel>
      </div>
      <div className="space-y-6">
        <Panel title="Data and universe">
          <div className="space-y-4">
            <label className="block">
              <span className="mb-1 block">Dataset version</span>
              <select className="field" value={dvId ?? ""} onChange={(e) => setDv(e.target.value)}>
                {versions.data?.map((v) => (
                  <option key={v.id} value={v.id}>
                    {shortHash(v.version_hash, 12)} · {v.symbol_count} symbols{v.is_synthetic ? " (synthetic)" : ""}
                  </option>
                ))}
              </select>
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <span className="mb-1 block">First session</span>
                <input name="start" type="date" min={version?.start_date} max={version?.end_date} defaultValue={version ? `${Number(version.start_date.slice(0, 4)) + 1}-01-02` : undefined} key={version?.id} className="field" />
              </label>
              <label className="block">
                <span className="mb-1 block">Last session</span>
                <input name="end" type="date" min={version?.start_date} max={version?.end_date} className="field" />
              </label>
            </div>
            <p className="text-xs text-ink-3">History before the first session is available for indicator warm-up but is never traded.</p>
            <label className="block">
              <span className="mb-1 block">Benchmark</span>
              <select name="benchmark" className="field" defaultValue={indexSymbols[0] ?? ""} key={indexSymbols.join()}>
                <option value="">None</option>
                {indexSymbols.map((s) => (
                  <option key={s}>{s}</option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block">Universe</span>
              <textarea name="universe" rows={2} className="field" placeholder={`All ${inst.data ? inst.data.total - indexSymbols.length : ""} tradable symbols`} />
              <span className="mt-0.5 block text-xs text-ink-3">Leave empty for every non-index symbol, including those delisted during the sample.</span>
            </label>
            <label className="block">
              <span className="mb-1 block">Risk-free rate (% per year)</span>
              <input name="risk_free_rate" type="number" min={-5} max={25} step="0.05" defaultValue={0} className="field num" />
            </label>
          </div>
        </Panel>
        <Panel title="Record">
          <div className="space-y-4">
            <label className="block">
              <span className="mb-1 block">Experiment name</span>
              <input name="name" required maxLength={200} defaultValue={entry ? `${entry.name}` : ""} key={key} className="field" />
            </label>
            <label className="block">
              <span className="mb-1 block">Hypothesis and notes</span>
              <textarea name="notes" rows={3} maxLength={5000} className="field" placeholder="What do you expect, and why?" />
            </label>
            {err && (
              <ErrorNote
                error={err}
                action={
                  details && (
                    <ul className="mt-1 list-disc pl-5 text-xs">
                      {details.map((d, i) => (
                        <li key={i}>
                          {d.loc?.filter((l) => l !== "body").join(".")}: {d.msg}
                        </li>
                      ))}
                    </ul>
                  )
                }
              />
            )}
            <button type="submit" className="btn btn-primary" disabled={busy || !dvId}>
              {busy ? "Submitting…" : "Run backtest"}
            </button>
          </div>
        </Panel>
      </div>
    </form>
  );
}

export default function NewExperimentPage() {
  return (
    <>
      <PageHeader title="New experiment" lede="Choose a strategy, state your execution assumptions and run the backtest in the background." />
      <Suspense fallback={<Skeleton h="h-96" />}>
        <Builder />
      </Suspense>
    </>
  );
}
