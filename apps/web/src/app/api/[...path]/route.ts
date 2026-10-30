import type { NextRequest } from "next/server";

// Same-origin proxy to the QuantLab API so the httpOnly session cookie is first-party.
// The upstream URL is read at request time, which keeps container images portable.
const FORWARD_REQUEST = ["content-type", "authorization", "idempotency-key", "x-request-id", "cookie"];
const FORWARD_RESPONSE = ["content-type", "x-request-id", "set-cookie", "content-disposition"];

async function forward(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const upstream = process.env.API_INTERNAL_URL ?? "http://localhost:8000";
  const url = new URL(`/api/${path.map(encodeURIComponent).join("/")}`, upstream);
  url.search = req.nextUrl.search;
  const headers = new Headers();
  for (const h of FORWARD_REQUEST) {
    const v = req.headers.get(h);
    if (v) headers.set(h, v);
  }
  const forwardedFor = req.headers.get("x-forwarded-for");
  if (forwardedFor) headers.set("x-forwarded-for", forwardedFor);
  const hasBody = !["GET", "HEAD"].includes(req.method);
  let res: Response;
  try {
    res = await fetch(url, {
      method: req.method,
      headers,
      body: hasBody ? await req.arrayBuffer() : undefined,
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
