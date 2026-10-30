"use client";

import {
  Area,
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { compact, money, num, pct } from "@/lib/format";

// Categorical order validated for CVD separation (see docs/FRONTEND.md). Fixed order:
// an experiment keeps its slot for the life of a comparison.
export const SERIES = ["#2747c8", "#e06a2c", "#1596a8", "#c98a00", "#c4508a", "#6b4fb0"];
const INK2 = "#5a6675";
const GRID = "#e4e8ed";
const BENCH = "#8a94a3";
const LOSS = "#b4372f";

type Row = Record<string, number | string | null>;

function rows(dates: string[], cols: Record<string, (number | null)[] | undefined>): Row[] {
  return dates.map((d, i) => {
    const r: Row = { date: d };
    for (const [k, v] of Object.entries(cols)) r[k] = v ? v[i] ?? null : null;
    return r;
  });
}

const axisProps = {
  tick: { fill: INK2, fontSize: 11 },
  tickLine: false,
  axisLine: { stroke: GRID },
} as const;

function yearTick(d: string) {
  return d.slice(0, 7);
}

function TooltipBox({ active, payload, label, fmt }: { active?: boolean; payload?: { name: string; value: number; color: string }[]; label?: string; fmt: (v: number) => string }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded border border-rule bg-surface px-2.5 py-1.5 text-xs shadow-sm">
      <div className="mb-1 text-ink-2">{label}</div>
      {payload.map((p) => (
        <div key={p.name} className="flex items-center gap-2">
          <span className="inline-block h-0.5 w-3" style={{ background: p.color }} aria-hidden />
          <span className="text-ink-2">{p.name}</span>
          <span className="num ml-auto pl-3 font-medium text-ink">{fmt(p.value)}</span>
        </div>
      ))}
    </div>
  );
}

export function Legend({ items }: { items: { label: string; color: string; dashed?: boolean }[] }) {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-2">
      {items.map((i) => (
        <li key={i.label} className="flex items-center gap-1.5">
          <svg width="16" height="6" aria-hidden>
            <line x1="0" y1="3" x2="16" y2="3" stroke={i.color} strokeWidth="2" strokeDasharray={i.dashed ? "4 3" : undefined} />
          </svg>
          {i.label}
        </li>
      ))}
    </ul>
  );
}

export function EquityChart({
  dates,
  equity,
  benchmark,
  drawdown,
  benchmarkDrawdown,
  benchmarkLabel = "Benchmark",
}: {
  dates: string[];
  equity: (number | null)[];
  benchmark?: (number | null)[];
  drawdown: (number | null)[];
  benchmarkDrawdown?: (number | null)[];
  benchmarkLabel?: string;
}) {
  const data = rows(dates, { equity, benchmark, drawdown, benchmarkDrawdown });
  const hasBench = !!benchmark?.some((v) => v !== null);
  return (
    <figure aria-label="Portfolio value and drawdown over time">
      <Legend
        items={[
          { label: "Strategy (net of costs)", color: SERIES[0] },
          ...(hasBench ? [{ label: benchmarkLabel, color: BENCH, dashed: true }] : []),
        ]}
      />
      <div className="mt-2 h-64">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} syncId="equity" margin={{ top: 4, right: 8, bottom: 0, left: 4 }}>
            <CartesianGrid stroke={GRID} vertical={false} />
            <XAxis dataKey="date" {...axisProps} tickFormatter={yearTick} minTickGap={48} hide />
            <YAxis {...axisProps} width={64} tickFormatter={(v) => compact(v)} domain={["auto", "auto"]} />
            <Tooltip content={<TooltipBox fmt={(v) => money(v)} />} />
            {hasBench && (
              <Line dataKey="benchmark" name={benchmarkLabel} stroke={BENCH} strokeWidth={1.5} strokeDasharray="4 3" dot={false} isAnimationActive={false} />
            )}
            <Line dataKey="equity" name="Strategy" stroke={SERIES[0]} strokeWidth={2} dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <figcaption className="mt-3 text-xs text-ink-2">Drawdown from running peak</figcaption>
      <div className="h-28">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data} syncId="equity" margin={{ top: 4, right: 8, bottom: 0, left: 4 }}>
            <CartesianGrid stroke={GRID} vertical={false} />
            <XAxis dataKey="date" {...axisProps} tickFormatter={yearTick} minTickGap={48} />
            <YAxis {...axisProps} width={64} tickFormatter={(v) => pct(v, 0)} domain={["dataMin", 0]} />
            <Tooltip content={<TooltipBox fmt={(v) => pct(v, 1)} />} />
            <Area dataKey="drawdown" name="Strategy drawdown" stroke={LOSS} strokeWidth={1.5} fill={LOSS} fillOpacity={0.12} isAnimationActive={false} />
            {hasBench && (
              <Line dataKey="benchmarkDrawdown" name="Benchmark drawdown" stroke={BENCH} strokeWidth={1} strokeDasharray="4 3" dot={false} isAnimationActive={false} />
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}

export function PriceChart({ dates, close, volume, label }: { dates: string[]; close: (number | null)[]; volume: (number | null)[]; label: string }) {
  const data = rows(dates, { close, volume });
  return (
    <figure aria-label={`${label} price history`}>
      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} syncId="price" margin={{ top: 4, right: 8, bottom: 0, left: 4 }}>
            <CartesianGrid stroke={GRID} vertical={false} />
            <XAxis dataKey="date" {...axisProps} hide />
            <YAxis {...axisProps} width={56} domain={["auto", "auto"]} tickFormatter={(v) => num(v, 0)} />
            <Tooltip content={<TooltipBox fmt={(v) => num(v, 2)} />} />
            <Line dataKey="close" name={label} stroke={SERIES[0]} strokeWidth={1.5} dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <figcaption className="mt-2 text-xs text-ink-2">Volume</figcaption>
      <div className="h-20">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data} syncId="price" margin={{ top: 4, right: 8, bottom: 0, left: 4 }}>
            <XAxis dataKey="date" {...axisProps} tickFormatter={yearTick} minTickGap={48} />
            <YAxis {...axisProps} width={56} tickFormatter={(v) => compact(v)} />
            <Tooltip content={<TooltipBox fmt={(v) => compact(v)} />} />
            <Bar dataKey="volume" name="Volume" fill={BENCH} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}

export function CompareChart({ series }: { series: { name: string; dates: string[]; values: (number | null)[] }[] }) {
  const byDate = new Map<string, Row>();
  series.forEach((s, i) =>
    s.dates.forEach((d, j) => {
      const r = byDate.get(d) ?? { date: d };
      r[`s${i}`] = s.values[j];
      byDate.set(d, r);
    }),
  );
  const data = [...byDate.values()].sort((a, b) => String(a.date).localeCompare(String(b.date)));
  return (
    <figure aria-label="Growth of one dollar for each experiment">
      <Legend items={series.map((s, i) => ({ label: s.name, color: SERIES[i % SERIES.length] }))} />
      <div className="mt-2 h-80">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 4 }}>
            <CartesianGrid stroke={GRID} vertical={false} />
            <XAxis dataKey="date" {...axisProps} tickFormatter={yearTick} minTickGap={48} />
            <YAxis {...axisProps} width={48} domain={["auto", "auto"]} tickFormatter={(v) => num(v, 2)} />
            <Tooltip content={<TooltipBox fmt={(v) => num(v, 3)} />} />
            {series.map((s, i) => (
              <Line key={i} dataKey={`s${i}`} name={s.name} stroke={SERIES[i % SERIES.length]} strokeWidth={2} dot={false} connectNulls isAnimationActive={false} />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}

/** Diverging cell fill: loss pole <- neutral -> gain pole, clipped at +/- `cap`. */
function divergingFill(v: number | null, cap = 0.08): string {
  if (v === null || !Number.isFinite(v)) return "transparent";
  const t = Math.min(1, Math.abs(v) / cap);
  const [r, g, b] = v >= 0 ? [31, 122, 90] : [180, 55, 47];
  return `rgba(${r}, ${g}, ${b}, ${0.08 + 0.55 * t})`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function MonthlyReturns({ data }: { data: { year: number; month: number; return: number }[] }) {
  const years = [...new Set(data.map((d) => d.year))].sort();
  const get = (y: number, m: number) => data.find((d) => d.year === y && d.month === m)?.return ?? null;
  const yearly = (y: number) => {
    const rs = data.filter((d) => d.year === y).map((d) => d.return);
    return rs.length ? rs.reduce((acc, r) => acc * (1 + r), 1) - 1 : null;
  };
  return (
    <div className="overflow-x-auto">
      <table className="data num" aria-label="Monthly returns">
        <thead>
          <tr>
            <th scope="col">Year</th>
            {MONTHS.map((m) => (
              <th key={m} scope="col" className="r">
                {m}
              </th>
            ))}
            <th scope="col" className="r">
              Full year
            </th>
          </tr>
        </thead>
        <tbody>
          {years.map((y) => (
            <tr key={y}>
              <th scope="row" className="font-medium">
                {y}
              </th>
              {MONTHS.map((_, i) => {
                const v = get(y, i + 1);
                return (
                  <td key={i} className="r" style={{ background: divergingFill(v) }} title={v === null ? "No data" : pct(v, 2)}>
                    {v === null ? "" : pct(v, 1)}
                  </td>
                );
              })}
              <td className="r font-medium">{pct(yearly(y), 1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
