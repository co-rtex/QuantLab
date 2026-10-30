import type { NextRequest } from "next/server";

// Same-origin proxy to the QuantLab API so the httpOnly session cookie is first-party.
// The upstream URL is read at request time, which keeps container images portable.
const FORWARD_REQUEST = ["content-type", "authorization", "idempotency-key", "x-request-id", "cookie"];
// content-security-policy: the API sandboxes HTML reports with a CSP; dropping it here served
// them same-origin with the app and no policy.
const FORWARD_RESPONSE = [
  "content-type",
  "x-request-id",
  "set-cookie",
  "content-disposition",
  "content-security-policy",
];
// Largest body the API accepts (CSV import, services/api/src/quantlab_api/limits.py).
const MAX_BODY_BYTES = 110 * 1024 * 1024;

/** Read the request body, refusing anything larger than MAX_BODY_BYTES without buffering it. */
async function readCapped(req: NextRequest): Promise<ArrayBuffer | null> {
  const declared = Number(req.headers.get("content-length") ?? "0");
  if (declared > MAX_BODY_BYTES) return null;
  if (!req.body) return new ArrayBuffer(0);
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BODY_BYTES) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const out = new Uint8Array(size);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.byteLength;
  }
  return out.buffer;
}

async function forward(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  // Only proxy the versioned API; never let dot segments or separators escape it.
  if (path[0] !== "v1" || path.some((seg) => seg === "." || seg === ".." || /[\\/]/.test(seg))) {
    return Response.json({ error: { code: "not_found", message: "Not found" } }, { status: 404 });
  }
  const upstream = process.env.API_INTERNAL_URL ?? "http://localhost:8000";
  const url = new URL(`/api/${path.map(encodeURIComponent).join("/")}`, upstream);
  if (!url.pathname.startsWith("/api/v1/")) {
    return Response.json({ error: { code: "not_found", message: "Not found" } }, { status: 404 });
  }
  url.search = req.nextUrl.search;
  const headers = new Headers();
  for (const h of FORWARD_REQUEST) {
    const v = req.headers.get(h);
    if (v) headers.set(h, v);
  }
  // Tell the API who the client is, or every browser shares this server's address and ten
  // failed logins anywhere lock everyone out (ADR-0008). Next fills X-Forwarded-For with
  // the socket address when the request has none; an edge in front appends its peer. Only
  // the rightmost hop is forwarded: entries to its left were written by the client.
  const hops = (req.headers.get("x-forwarded-for") ?? "").split(",").map((h) => h.trim()).filter(Boolean);
  if (hops.length) headers.set("x-forwarded-for", hops[hops.length - 1]);
  const hasBody = !["GET", "HEAD"].includes(req.method);
  const body = hasBody ? await readCapped(req) : undefined;
  if (body === null) {
    return Response.json(
      { error: { code: "payload_too_large", message: "Request body is too large." } },
      { status: 413 },
    );
  }
  let res: Response;
  try {
    res = await fetch(url, {
      method: req.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
    });
  } catch {
    return Response.json(
      { error: { code: "upstream_unavailable", message: "The QuantLab API is not reachable." } },
      { status: 502 },
    );
  }
  const out = new Headers();
  for (const h of FORWARD_RESPONSE) {
    if (h === "set-cookie") {
      for (const c of res.headers.getSetCookie()) out.append("set-cookie", c);
    } else {
      const v = res.headers.get(h);
      if (v) out.set(h, v);
    }
  }
  out.set("cache-control", "no-store");
  return new Response(res.status === 204 ? null : res.body, { status: res.status, headers: out });
}

export const GET = forward;
export const POST = forward;
export const PATCH = forward;
export const DELETE = forward;
