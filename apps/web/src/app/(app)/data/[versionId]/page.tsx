"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { PriceChart } from "@/components/charts";
import { PageHeader } from "@/components/Shell";
import { ErrorNote, Panel, Skeleton, StatusPill, SyntheticTag } from "@/components/ui";
import type { DatasetVersion, Instrument, Page } from "@/lib/api";
import { compact, pct } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface Bars {
  symbol: string;
  adjusted: boolean;
  dates: string[];
  close: (number | null)[];
  volume: (number | null)[];
  cache: string;
}

export default function VersionPage() {
  const { versionId } = useParams<{ versionId: string }>();
  const v = useApi<DatasetVersion>(`/dataset-versions/${versionId}`);
  const [q, setQ] = useState("");
  const inst = useApi<Page<Instrument>>(`/dataset-versions/${versionId}/instruments?limit=500${q ? `&q=${encodeURIComponent(q)}` : ""}`);
  const [symbol, setSymbol] = useState<string | null>(null);
  const [adjusted, setAdjusted] = useState(true);
  const active = symbol ?? inst.data?.items.find((i) => i.asset_class !== "index_fund")?.symbol ?? null;
  const bars = useApi<Bars>(active ? `/dataset-versions/${versionId}/bars/${active}?adjusted=${adjusted}` : null);

  if (v.error) return <ErrorNote error={v.error} />;
  if (!v.data) return <Skeleton h="h-96" />;
  const d = v.data;
  const prov = (d.provenance ?? {}) as Record<string, unknown>;

  return (
    <>
      <p className="mb-2 text-sm">
        <Link href="/data" className="text-cobalt hover:underline">
          Market data
        </Link>
      </p>
      <PageHeader
        title={`Dataset version ${d.version_hash.slice(0, 12)}`}
        lede={`${d.symbol_count} symbols, ${compact(d.rows)} daily bars from ${d.start_date} to ${d.end_date}.`}
        actions={
          <Link href={`/experiments/new?dataset=${d.id}`} className="btn btn-primary">
            Backtest on this version
          </Link>
        }
      />
      <div className="mb-6 flex flex-wrap items-center gap-3 rounded border border-rule bg-surface px-4 py-3 text-sm">
        {d.is_synthetic && <SyntheticTag />}
        <span>
          Provider <strong className="font-medium">{String(prov.provider ?? "unknown")}</strong>
        </span>
        <span className="text-ink-2">Quality</span>
        <StatusPill status={d.quality_status} />
        <span className="num truncate text-xs text-ink-2" title={d.version_hash}>
          SHA-256 {d.version_hash}
        </span>
        {typeof prov.disclaimer === "string" && <p className="w-full text-xs text-caution">{prov.disclaimer}</p>}
      </div>

      <div className="grid gap-6 xl:grid-cols-[3fr_2fr]">
        <Panel
          title={active ? `${active} price history` : "Price history"}
          aside={
            <label className="flex items-center gap-1.5">
              <input type="checkbox" checked={adjusted} onChange={(e) => setAdjusted(e.target.checked)} />
              Adjusted for splits and dividends
            </label>
          }
        >
          {bars.loading && !bars.data ? (
            <Skeleton h="h-80" />
          ) : bars.error ? (
            <ErrorNote error={bars.error} />
          ) : bars.data ? (
            <>
              <PriceChart dates={bars.data.dates} close={bars.data.close} volume={bars.data.volume} label={bars.data.symbol} />
              <p className="mt-2 text-xs text-ink-3">
                {bars.data.adjusted
                  ? "Total-return adjusted as of the last bar: earlier prices are scaled for later splits and dividends."
                  : "Raw prices as traded. Splits appear as discontinuities."}{" "}
                Served from cache: {bars.data.cache}.
              </p>
            </>
          ) : null}
        </Panel>
        <Panel title="Instruments" aside={inst.data ? `${inst.data.total} total` : undefined}>
          <label className="mb-3 block">
            <span className="sr-only">Filter symbols</span>
            <input className="field" placeholder="Filter by symbol prefix" value={q} onChange={(e) => setQ(e.target.value)} />
          </label>
          <div className="max-h-[22rem] overflow-auto" tabIndex={0}>
            <table className="data num">
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Sector</th>
                  <th>Listed</th>
                  <th>Last bar</th>
                </tr>
              </thead>
              <tbody>
                {inst.data?.items.map((i) => (
                  <tr key={i.symbol} className={i.symbol === active ? "bg-cobalt-soft" : ""}>
                    <td>
                      <button className="text-cobalt hover:underline" onClick={() => setSymbol(i.symbol)}>
                        {i.symbol}
                      </button>
                    </td>
                    <td className="text-ink-2">{i.sector}</td>
                    <td>{i.first_date}</td>
                    <td>
                      {i.last_date}
                      {i.delisted && <span className="ml-1.5 text-xs text-loss">delisted</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>

      <Panel title="Quality checks" className="mt-6">
        <div className="overflow-x-auto" tabIndex={0}>
          <table className="data">
            <thead>
              <tr>
                <th>Check</th>
                <th>Result</th>
                <th className="r">Findings</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              {d.quality?.checks.map((c) => (
                <tr key={c.name}>
                  <td className="font-medium">{c.name.replaceAll("_", " ")}</td>
                  <td>
                    <StatusPill status={c.status} />
                  </td>
                  <td className="r num">{c.count}</td>
                  <td className="text-ink-2">{c.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {d.quality?.per_symbol_worst && d.quality.per_symbol_worst.some((p) => p.coverage < 1) && (
          <div className="mt-4">
            <h3 className="mb-2 text-sm font-medium">Lowest session coverage</h3>
            <ul className="num grid gap-1 text-sm sm:grid-cols-3">
              {d.quality.per_symbol_worst
                .filter((p) => p.coverage < 1)
                .map((p) => (
                  <li key={p.symbol}>
                    {p.symbol}: {pct(p.coverage, 2)} ({p.missing_sessions} missing)
                  </li>
                ))}
            </ul>
          </div>
        )}
        <p className="mt-4 text-xs text-ink-3">
          Survivorship: this version includes symbols that were delisted during the sample. Universes built only from
          symbols alive at the end date would overstate historical returns.
        </p>
      </Panel>
    </>
  );
}
