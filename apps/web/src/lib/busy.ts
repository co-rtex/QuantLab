// A 503 server_busy means the API refused the request before doing any of its work: its
// process had no free slot for heavy work (#223). Sending it again is therefore safe, even
// a POST, and it is sent again once, after Retry-After (capped here); a second refusal is
// the caller's to show. Kept apart from api.ts so Node's test runner can load it as is.
const BUSY_RETRY_CAP_SECONDS = 10;
const BUSY_RETRY_DEFAULT_SECONDS = 2; // the API's own (limits.HEAVY_RETRY_AFTER_SECONDS)

/** Seconds to wait before sending the request again, or null when it is not a busy
 * refusal. The Retry-After header first, then the body's details.retry_after_seconds (a
 * proxy may drop the header), then the API's default. Never 0 for a missing header:
 * Number(null) is 0, and an immediate retry only meets a second refusal. */
export function busyRetrySeconds(res: Response, body: unknown): number | null {
  const error = (body as { error?: { code?: unknown; details?: { retry_after_seconds?: unknown } } } | null)?.error;
  if (res.status !== 503 || error?.code !== "server_busy") return null;
  const header = res.headers.get("retry-after");
  const candidates = [header === null || header.trim() === "" ? NaN : Number(header), Number(error.details?.retry_after_seconds ?? NaN)];
  const s = candidates.find((n) => Number.isFinite(n) && n >= 0) ?? BUSY_RETRY_DEFAULT_SECONDS;
  return Math.min(s, BUSY_RETRY_CAP_SECONDS);
}

/** ``send()``, and once more after Retry-After if the server answered busy. */
export async function sendRetryingBusy(
  send: () => Promise<Response>,
  onBusy?: (seconds: number) => void,
): Promise<Response> {
  const res = await send();
  if (res.status !== 503) return res;
  const body = await res
    .clone()
    .json()
    .catch(() => null);
  const wait = busyRetrySeconds(res, body);
  if (wait === null) return res;
  onBusy?.(wait);
  await new Promise((r) => setTimeout(r, wait * 1000));
  return send();
}
