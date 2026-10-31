"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState, type FormEvent } from "react";
import { EquityChart } from "@/components/charts";
import { PageHeader } from "@/components/Shell";
import { ErrorNote, Panel, QueryError, Skeleton, SyntheticLabel, syntheticStatus } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { money, num, pct, signClass } from "@/lib/format";
import { useApi } from "@/lib/useApi";

interface Tx {
  seq: number;
  trade_date: string;
  kind: string;
  symbol: string | null;
  quantity: number;
  price: number;
  commission: number;
  amount: number;
  note: string;
}
interface Valuation {
  portfolio: { name: string; benchmark: string | null; is_synthetic: boolean };
  summary: { equity: number; cash: number; net_contributions: number; time_weighted_return: number; sessions: number };
  series: { dates: string[]; index: number[]; benchmark: number[] | null; drawdown: number[] };
  holdings: { symbol: string; quantity: number; price: number; value: number; weight: number }[];
  performance: { values: Record<string, number | null> };
  risk: {
    summary: Record<string, number>;
    concentration: Record<string, number | null>;
    contributions?: { symbol: string; weight: number; component: number; pct: number }[];
    stress?: { scenario: string; portfolio_return: number }[];
    monte_carlo?: { terminal_quantiles: Record<string, number>; probability_of_loss: number };
  };
}

export default function PortfolioPage() {
  const { id } = useParams<{ id: string }>();
  const [nonce, setNonce] = useState(0);
  const p = useApi<{ name: string; transactions: Tx[] }>(`/portfolios/${id}?n=${nonce}`);
  const v = useApi<Valuation>(`/portfolios/${id}/valuation?n=${nonce}`);
  const [err, setErr] = useState<ApiError>();
  const [kind, setKind] = useState("buy");

  async function add(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget; // capture before await: React clears currentTarget
    const f = new FormData(form);
    setErr(undefined);
    const trade = kind === "buy" || kind === "sell";
    try {
      await api(`/portfolios/${id}/transactions`, {
        method: "POST",
        json: {
          trade_date: f.get("date"),
          kind,
          symbol: trade ? f.get("symbol") : null,
          quantity: trade ? Number(f.get("quantity")) : 0,
          price: trade && f.get("price") ? Number(f.get("price")) : null,
          commission: trade ? Number(f.get("commission") || 0) : 0,
          amount: trade ? 0 : Number(f.get("amount")),
        },
      });
      form.reset();
      setNonce((n) => n + 1);
    } catch (x) {
      setErr(x as ApiError);
    }
  }

  if (p.error && !p.data) return <QueryError q={p} what="this portfolio" />;
  const val = v.data;
  return (
    <>
      <p className="mb-2 text-sm">
        <Link href="/portfolios" className="text-cobalt hover:underline">
          Portfolios
        </Link>
      </p>
      <PageHeader title={p.data?.name ?? "Portfolio"} lede="Returns are time-weighted, so deposits and withdrawals do not count as performance." actions={<SyntheticLabel status={syntheticStatus(val?.portfolio.is_synthetic)} />} />
      <QueryError q={p} what="this portfolio" />
      <QueryError q={v} what="the valuation" />
      {val && (
        <dl className="mb-6 grid grid-cols-2 gap-x-8 gap-y-2 rounded border border-rule bg-surface px-4 py-3 text-sm sm:grid-cols-4">
          {[
            ["Value", money(val.summary.equity)],
            ["Cash", money(val.summary.cash)],
            ["Net contributions", money(val.summary.net_contributions)],
            ["Time-weighted return", pct(val.summary.time_weighted_return, 2)],
          ].map(([k, x]) => (
            <div key={k}>
              <dt className="text-xs text-ink-2">{k}</dt>
              <dd className="num font-medium">{x}</dd>
            </div>
          ))}
        </dl>
      )}
      <div className="grid gap-6 2xl:grid-cols-[3fr_2fr]">
        <Panel title="Growth of 100" aside="Time-weighted, versus benchmark total return">
          {val ? (
            <EquityChart dates={val.series.dates} equity={val.series.index} benchmark={val.series.benchmark ?? undefined} drawdown={val.series.drawdown} benchmarkLabel={val.portfolio.benchmark ?? "Benchmark"} />
          ) : (
            !v.error && <Skeleton h="h-80" /> // the error is shown above the summary
          )}
        </Panel>
        <Panel title="Record a transaction">
          <form onSubmit={add} className="grid grid-cols-2 gap-3 text-sm">
            <label className="block">
              <span className="mb-1 block">Type</span>
              <select className="field" value={kind} onChange={(e) => setKind(e.target.value)}>
                <option value="buy">Buy</option>
                <option value="sell">Sell</option>
                <option value="deposit">Deposit</option>
                <option value="withdrawal">Withdrawal</option>
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block">Session</span>
              <input name="date" type="date" required className="field" />
            </label>
            {kind === "buy" || kind === "sell" ? (
              <>
                <label className="block">
                  <span className="mb-1 block">Symbol</span>
                  <input name="symbol" required className="field" />
                </label>
                <label className="block">
                  <span className="mb-1 block">Shares</span>
                  <input name="quantity" type="number" min={0} step="any" required className="field num" />
                </label>
                <label className="block">
                  <span className="mb-1 block">Price (blank = session close)</span>
                  <input name="price" type="number" min={0} step="any" className="field num" />
                </label>
                <label className="block">
                  <span className="mb-1 block">Commission</span>
                  <input name="commission" type="number" min={0} step="any" defaultValue={0} className="field num" />
                </label>
              </>
            ) : (
              <label className="col-span-2 block">
                <span className="mb-1 block">Amount (USD)</span>
                <input name="amount" type="number" min={0} step="any" required className="field num" />
              </label>
            )}
            <div className="col-span-2">
              <ErrorNote error={err} />
              <button className="btn btn-primary mt-2">Record transaction</button>
              <p className="mt-2 text-xs text-ink-3">The ledger is append-only. A transaction that would overdraw cash or sell more than you hold is rejected and nothing is stored.</p>
            </div>
          </form>
        </Panel>
      </div>
      {val && (
        <div className="mt-6 grid gap-6 xl:grid-cols-3">
          <Panel title="Holdings">
            {val.holdings.length === 0 ? (
              <p className="text-sm text-ink-2">All cash.</p>
            ) : (
              <table className="data num">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th className="r">Shares</th>
                    <th className="r">Value</th>
                    <th className="r">Weight</th>
                  </tr>
                </thead>
                <tbody>
                  {val.holdings.map((h) => (
                    <tr key={h.symbol}>
                      <td className="font-medium">{h.symbol}</td>
                      <td className="r">{num(h.quantity, 2)}</td>
                      <td className="r">{money(h.value)}</td>
                      <td className="r">{pct(h.weight, 1)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>
          <Panel title="Risk">
            <dl className="space-y-1.5 text-sm">
              {[
                ["Annualized volatility", pct(val.risk.summary.annualized_volatility, 1)],
                ["One-day VaR, 95%", pct(val.risk.summary.historical_var, 2)],
                ["Expected shortfall, 95%", pct(val.risk.summary.historical_es, 2)],
                ["Effective number of holdings", num(val.risk.concentration.effective_n, 1)],
                ["Largest position", pct(val.risk.concentration.top1, 1)],
                ["Maximum drawdown", pct(val.performance.values.max_drawdown, 1)],
                ["Beta to benchmark", num(val.performance.values.beta)],
              ].map(([k, x]) => (
                <div key={k} className="flex justify-between gap-2">
                  <dt className="text-ink-2">{k}</dt>
                  <dd className="num">{x}</dd>
                </div>
              ))}
            </dl>
            {val.risk.contributions && (
              <table className="data num mt-4">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th className="r">Weight</th>
                    <th className="r">Share of risk</th>
                  </tr>
                </thead>
                <tbody>
                  {val.risk.contributions.map((c) => (
                    <tr key={c.symbol}>
                      <td>{c.symbol}</td>
                      <td className="r">{pct(c.weight, 1)}</td>
                      <td className="r">{pct(c.pct, 1)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>
          <Panel title="Scenarios">
            <ul className="space-y-1.5 text-sm">
              {val.risk.stress?.map((s) => (
                <li key={s.scenario} className="flex justify-between gap-2">
                  <span className="text-ink-2">{s.scenario} (beta-scaled)</span>
                  <span className={`num ${signClass(s.portfolio_return)}`}>{pct(s.portfolio_return, 1)}</span>
                </li>
              ))}
            </ul>
            {val.risk.monte_carlo && (
              <div className="mt-4 text-sm">
                <h3 className="mb-1 font-medium">One year ahead, resampled history</h3>
                <p className="text-ink-2">
                  5th to 95th percentile of value per $1: <span className="num text-ink">{num(val.risk.monte_carlo.terminal_quantiles["0.05"])}</span> to{" "}
                  <span className="num text-ink">{num(val.risk.monte_carlo.terminal_quantiles["0.95"])}</span>; probability of a loss{" "}
                  <span className="num text-ink">{pct(val.risk.monte_carlo.probability_of_loss, 0)}</span>.
                </p>
                <p className="mt-1 text-xs text-ink-3">Stationary bootstrap of this portfolio&apos;s own daily returns; it cannot produce shocks larger than those already observed.</p>
              </div>
            )}
          </Panel>
        </div>
      )}
      <Panel title="Ledger" className="mt-6">
        <table className="data num">
          <thead>
            <tr>
              <th>#</th>
              <th>Session</th>
              <th>Type</th>
              <th>Symbol</th>
              <th className="r">Shares</th>
              <th className="r">Price</th>
              <th className="r">Commission</th>
              <th className="r">Amount</th>
            </tr>
          </thead>
          <tbody>
            {p.data?.transactions.map((t) => (
              <tr key={t.seq}>
                <td>{t.seq}</td>
                <td>{t.trade_date}</td>
                <td>{t.kind}</td>
                <td>{t.symbol ?? ""}</td>
                <td className="r">{t.quantity ? num(t.quantity, 2) : ""}</td>
                <td className="r">{t.price ? num(t.price) : ""}</td>
                <td className="r">{t.commission ? money(t.commission, 2) : ""}</td>
                <td className="r">{t.amount ? money(t.amount) : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </>
  );
}
