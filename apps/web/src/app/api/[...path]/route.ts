import type { NextRequest } from "next/server";

import { edgeClientAddress, edgeRequiredResponse, fromEdge } from "@/lib/edge";

// Same-origin proxy to the QuantLab API so the httpOnly session cookie is first-party.
// The upstream URL is read at request time, which keeps container images portable.
const FORWARD_REQUEST = ["content-type", "authorization", "idempotency-key", "x-request-id", "cookie"];
// content-security-policy: the API serves HTML reports under a sandboxing CSP; dropping it here served
// them same-origin with the app and no policy.
const FORWARD_RESPONSE = [
  "content-type",
  "x-request-id",
  "set-cookie",
  "content-disposition",
  "content-security-policy",
];
// The API's interactive docs and their schema (#201): never proxied, see forward().
const API_DOCS = new Set(["docs", "openapi.json", "redoc"]);
// Largest body the API accepts (CSV import, services/api/src/quantlab_api/limits.py).
const MAX_BODY_BYTES = 110 * 1024 * 1024;

class BodyTooLarge extends Error {}

/**
 * The request body as a stream that errors once it passes MAX_BODY_BYTES. Streaming instead
 * of buffering (#8): every request used to be held in memory, up to 110 MiB each, before
 * the API was even asked, so anonymous clients could make this server hold that much per
 * connection. The API applies its own, tighter limits to what it reads.
 */
function cappedBody(req: NextRequest): ReadableStream<Uint8Array> | null | undefined {
  const declared = Number(req.headers.get("content-length") ?? "0");
  if (declared > MAX_BODY_BYTES) return null;
  if (!req.body) return undefined;
  let size = 0;
  return req.body.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        size += chunk.byteLength;
        if (size > MAX_BODY_BYTES) controller.error(new BodyTooLarge());
        else controller.enqueue(chunk);
      },
    }),
  );
}

function tooLarge(): Response {
  return Response.json(
    { error: { code: "payload_too_large", message: "Request body is too large." } },
    { status: 413 },
  );
}

async function forward(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  if (!fromEdge(req.headers)) return edgeRequiredResponse();
  const { path } = await ctx.params;
  // Only proxy the versioned API; never let dot segments or separators escape it.
  if (path[0] !== "v1" || path.some((seg) => seg === "." || seg === ".." || /[\\/]/.test(seg))) {
    return Response.json({ error: { code: "not_found", message: "Not found" } }, { status: 404 });
  }
  // Never the API's interactive docs (#201): Swagger UI loads a CDN script and runs an
  // inline one, which here would be on the app's origin, outside the pages' CSP. The API
  // serves them only in development, and then on its own port. No API route lives under
  // these names, so nothing below them is proxied either.
  if (API_DOCS.has(path[1])) {
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
  // The client address goes to the API only when the TLS edge wrote it (lib/edge.ts,
  // ADR-0008). Otherwise none is sent and the API skips its per-address limit: forwarding
  // whatever arrived would let a client choose the bucket it is throttled in.
  const client = edgeClientAddress(req.headers);
  if (client) headers.set("x-forwarded-for", client);
  const hasBody = !["GET", "HEAD"].includes(req.method);
  const body = hasBody ? cappedBody(req) : undefined;
  if (body === null) return tooLarge();
  let res: Response;
  try {
    res = await fetch(url, {
      method: req.method,
      headers,
      body,
      // Required by Node's fetch to send a stream body.
      duplex: "half",
      // A client that disconnects aborts the API request too, instead of leaving it running.
      signal: req.signal,
      cache: "no-store",
      redirect: "manual",
    } as RequestInit & { duplex: "half" });
  } catch (e) {
    if (e instanceof BodyTooLarge || (e instanceof Error && e.cause instanceof BodyTooLarge)) {
      return tooLarge();
    }
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
