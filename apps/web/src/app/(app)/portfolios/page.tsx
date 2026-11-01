"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { PageHeader } from "@/components/Shell";
import { BusyNote, ErrorNote, Panel, QueryError, SyntheticLabel } from "@/components/ui";
import { api, ApiError, type DatasetVersion } from "@/lib/api";
import { dateTime, shortHash } from "@/lib/format";
import { useSynthetic } from "@/lib/synthetic";
import { useInstruments } from "@/lib/instruments";
import { useApi } from "@/lib/useApi";
import { useWorkspace } from "@/lib/workspace";

interface PortfolioRow {
  id: string;
  name: string;
  benchmark: string | null;
  dataset_version_id: string;
  is_synthetic?: boolean | null;
  transactions: number;
  created_at: string;
}

export default function PortfoliosPage() {
  const router = useRouter();
  const { workspace } = useWorkspace();
  const ws = workspace?.id;
  const list = useApi<PortfolioRow[]>(ws ? `/workspaces/${ws}/portfolios` : null);
  const synthetic = useSynthetic(list.data);
  const versions = useApi<DatasetVersion[]>(ws ? `/workspaces/${ws}/dataset-versions` : null);
  const [dv, setDv] = useState<string>("");
  const dvId = dv || versions.data?.[0]?.id || null;
  const version = versions.data?.find((v) => v.id === dvId);
  const inst = useInstruments(dvId);
  const indexes = inst.data?.items.filter((i) => i.asset_class === "index_fund") ?? [];
  const [err, setErr] = useState<ApiError>();
  // In flight (including a busy server's wait): the button is disabled, so one click
  // creates one portfolio.
  const [pending, setPending] = useState(false);
  const [busy, setBusy] = useState<number>();

  async function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (pending) return;
    const f = new FormData(e.currentTarget);
    setErr(undefined);
    setPending(true);
    try {
      const p = await api<{ id: string }>(`/workspaces/${ws}/portfolios`, {
        method: "POST",
        onBusy: setBusy, // the server is busy: api() tries once more after this wait
        json: {
          name: f.get("name"),
          dataset_version_id: dvId,
          benchmark: f.get("benchmark") || null,
          initial_deposit: Number(f.get("deposit")),
          start_date: f.get("start"),
        },
      });
      router.push(`/portfolios/${p.id}`);
    } catch (x) {
      setErr(x as ApiError);
    }
    setBusy(undefined);
    setPending(false);
  }

  return (
    <>
      <PageHeader title="Portfolios" lede="Record simulated trades and cash flows in an append-only ledger and value them against a dataset version, including dividends and splits." />
      <div className="grid gap-6 xl:grid-cols-[2fr_1fr]">
        <Panel title="Your portfolios">
          <QueryError q={list} what="your portfolios" />
          {list.data?.length === 0 ? (
            <p className="text-sm text-ink-2">No portfolios yet. Create one to start recording trades.</p>
          ) : (
            <table className="data">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Benchmark</th>
                  <th className="r">Ledger entries</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {list.data?.map((p) => (
                  <tr key={p.id}>
                    <td>
                      <Link className="font-medium text-cobalt hover:underline" href={`/portfolios/${p.id}`}>
                        {p.name}
                      </Link>
                      <SyntheticLabel inline status={synthetic(p)} />
                    </td>
                    <td>{p.benchmark ?? "—"}</td>
                    <td className="r num">{p.transactions}</td>
                    <td className="text-ink-2">{dateTime(p.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
        <Panel title="New portfolio">
          <QueryError q={versions} what="dataset versions" />
          <QueryError q={inst} what="benchmark instruments" />
          <form onSubmit={create} className="space-y-3 text-sm">
            <label className="block">
              <span className="mb-1 block">Name</span>
              <input name="name" required defaultValue="Core holdings" className="field" />
            </label>
            <label className="block">
              <span className="mb-1 block">Dataset version (prices and corporate actions)</span>
              <select className="field" value={dvId ?? ""} onChange={(e) => setDv(e.target.value)}>
                {versions.data?.map((v) => (
                  <option key={v.id} value={v.id}>
                    {shortHash(v.version_hash, 12)} · {v.start_date} to {v.end_date}
                    {v.is_synthetic ? " (synthetic)" : ""}
                  </option>
                ))}
              </select>
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <span className="mb-1 block">First session</span>
                <input name="start" type="date" required min={version?.start_date} max={version?.end_date} defaultValue={version?.start_date} key={version?.id} className="field" />
              </label>
              <label className="block">
                <span className="mb-1 block">Initial deposit (USD)</span>
                <input name="deposit" type="number" min={1} defaultValue={100000} className="field num" />
              </label>
            </div>
            <label className="block">
              <span className="mb-1 block">Benchmark</span>
              <select name="benchmark" className="field" key={indexes.map((i) => i.symbol).join()} defaultValue={indexes[0]?.symbol ?? ""}>
                <option value="">None</option>
                {indexes.map((i) => (
                  <option key={i.symbol}>{i.symbol}</option>
                ))}
              </select>
            </label>
            <ErrorNote error={err} />
            <BusyNote seconds={busy} />
            <button className="btn btn-primary" disabled={!dvId || pending}>
              {pending ? "Creating…" : "Create portfolio"}
            </button>
          </form>
        </Panel>
      </div>
    </>
  );
}
