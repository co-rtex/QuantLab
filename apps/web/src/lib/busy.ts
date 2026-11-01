// A 503 server_busy means the API refused the request before doing any of its work: its
// process had no free slot for heavy work (#223). Sending it again is therefore safe, even
// a POST, and it is sent again once, after Retry-After (capped here); a second refusal is
// the caller's to show. Kept apart from api.ts so Node's test runner can load it as is.
const BUSY_RETRY_CAP_SECONDS = 10;

/** Seconds to wait before sending the request again, or null when it is not a busy refusal. */
export function busyRetrySeconds(res: Response, code: unknown): number | null {
  if (res.status !== 503 || code !== "server_busy") return null;
  const s = Number(res.headers.get("retry-after"));
  return Number.isFinite(s) ? Math.min(Math.max(s, 0), BUSY_RETRY_CAP_SECONDS) : 1;
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
  const wait = busyRetrySeconds(res, body?.error?.code);
  if (wait === null) return res;
  onBusy?.(wait);
  await new Promise((r) => setTimeout(r, wait * 1000));
  return send();
}
