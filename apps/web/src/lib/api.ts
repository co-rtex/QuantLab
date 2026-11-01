import { sendRetryingBusy } from "./busy";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: unknown,
    public requestId?: string,
  ) {
    super(message);
  }
}

export interface ApiInit extends RequestInit {
  json?: unknown;
  /** Told when the server is busy (503 server_busy, #223) and the request will be sent
   * again, once, in `seconds` (busy.ts). */
  onBusy?: (seconds: number) => void;
}

export async function api<T>(path: string, init: ApiInit = {}): Promise<T> {
  const { onBusy, json, ...rest } = init;
  const headers = new Headers(rest.headers);
  let body = rest.body;
  if (json !== undefined) {
    headers.set("content-type", "application/json");
    body = JSON.stringify(json);
  }
  const res = await sendRetryingBusy(
    () => fetch(`/api/v1${path}`, { ...rest, headers, body, credentials: "same-origin" }),
    onBusy,
  );
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const e = data?.error ?? {};
    if (res.status === 401 && typeof window !== "undefined" && !path.startsWith("/auth/")) {
      // Full navigation (not router.push): the session is gone and app state must reset.
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    }
    throw new ApiError(res.status, e.code ?? "error", e.message ?? res.statusText, e.details, e.request_id);
  }
  return data as T;
}

/** Ask the API to stop a queued or running job; the worker stops at its next checkpoint. */
export function cancelJob(id: string): Promise<Job> {
  return api<Job>(`/jobs/${id}/cancel`, { method: "POST" });
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

// ---- types mirrored from the API (subset used by the UI)
export type Metrics = Record<string, number | null>;

export interface User {
  id: string;
  email: string;
  display_name: string;
}
export interface Workspace {
  id: string;
  name: string;
  description: string;
  role: string;
}
export interface Dataset {
  id: string;
  slug: string;
  name: string;
  description: string;
  provider: string;
  created_at: string;
}
export interface DatasetVersion {
  id: string;
  dataset_id: string;
  version_hash: string;
  rows: number;
  symbol_count: number;
  start_date: string;
  end_date: string;
  is_synthetic: boolean;
  quality_status: "pass" | "warn" | "fail";
  created_at: string;
  provenance?: Record<string, unknown>;
  quality?: {
    status: string;
    checks: { name: string; status: string; count: number; description: string; sample: Record<string, unknown>[] }[];
    per_symbol_worst: { symbol: string; rows: number; coverage: number; missing_sessions: number }[];
  };
}
export interface Instrument {
  symbol: string;
  name: string;
  sector: string | null;
  asset_class: string;
  first_date: string;
  last_date: string;
  delisted: boolean;
  rows: number;
  // One catalog row per (ticker, instrument): a reused ticker appears once per issuer.
  instrument_id?: string | null;
}
export interface Job {
  id: string;
  kind: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled" | "timed_out";
  progress: number;
  message: string;
  attempts: number;
  max_attempts: number;
  error: { type: string; message: string } | null;
  // Set when someone asked to cancel; the worker stops at its next checkpoint.
  cancel_requested?: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  payload: Record<string, unknown>;
}
export interface Experiment {
  // Whether its dataset is synthetic (#106); null when that could not be checked.
  is_synthetic?: boolean | null;
  id: string;
  name: string;
  kind: string;
  dataset_version_id: string;
  strategy_key: string;
  strategy_params: Record<string, unknown>;
  config: Record<string, unknown>;
  input_hash: string;
  code_version: string;
  engine_version: string;
  parent_id: string | null;
  status: Job["status"];
  notes: string;
  created_at: string;
  summary: Metrics | null;
  fingerprint: string | null;
}
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
export interface StrategyCatalogEntry {
  key: string;
  name: string;
  description: string;
  params_schema: {
    properties?: Record<string, JsonSchemaProp>;
    required?: string[];
  };
}
export interface JsonSchemaProp {
  type?: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  enum?: string[];
  description?: string;
  title?: string;
  anyOf?: JsonSchemaProp[];
}
export interface Series {
  dates: string[];
  [k: string]: (number | null)[] | string[];
}
