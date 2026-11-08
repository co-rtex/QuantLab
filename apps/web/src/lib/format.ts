export const DASH = "—";

/** `v` rounded to `digits` places, with a value that rounds to zero made +0: "-0.00" reads
 * as a negative result that isn't there (audit A10). */
function unsignedZero(v: number, digits: number): number {
  const r = Number(v.toFixed(digits));
  return r === 0 ? 0 : v;
}

export function pct(v: number | null | undefined, digits = 1, signed = false): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  const x = unsignedZero(v * 100, digits);
  const s = x.toFixed(digits) + "%";
  return signed && x > 0 ? "+" + s : s;
}

export function num(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  return unsignedZero(v, digits).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** A multiple such as turnover ("3.2×"); undefined renders as a bare dash, not "—×". */
export function multiple(v: number | null | undefined, digits = 2): string {
  const s = num(v, digits);
  return s === DASH ? DASH : `${s}×`;
}

export function money(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  return v.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: digits, minimumFractionDigits: digits });
}

export function compact(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  return Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(v);
}

export function signClass(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v) || v === 0) return "";
  return v > 0 ? "text-gain" : "text-loss";
}

export function shortHash(h: string | null | undefined, n = 10): string {
  return h ? h.slice(0, n) : DASH;
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function strategyLabel(key: string): string {
  const map: Record<string, string> = {
    buy_and_hold: "Buy and hold",
    equal_weight: "Equal weight",
    sma_crossover: "Moving-average crossover",
    momentum: "Cross-sectional momentum",
    mean_reversion: "Short-term mean reversion",
    low_volatility: "Low volatility",
    min_variance: "Minimum variance",
    risk_parity: "Risk parity",
    vol_target: "Volatility target",
  };
  return map[key] ?? key;
}
