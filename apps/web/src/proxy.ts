import { NextResponse, type NextRequest } from "next/server";

import { contentSecurityPolicy, newNonce } from "@/lib/csp";
import { edgeRequiredResponse, edgeSecret, fromEdge } from "@/lib/edge";

// /invite handles signed-out visitors itself: it must keep the token from the URL fragment.
const PUBLIC = ["/login", "/register", "/invite"];

export function proxy(request: NextRequest) {
  // Behind the TLS edge, pages too are served only through it (the API proxy checks itself,
  // since this matcher skips api/: Next buffers proxied bodies here, which would truncate uploads).
  if (!fromEdge(request.headers)) return edgeRequiredResponse();
  const { pathname } = request.nextUrl;
  const hasSession = request.cookies.has("quantlab_session");
  if (!hasSession && !PUBLIC.includes(pathname)) {
    const url = new URL("/login", request.url);
    if (pathname !== "/") url.searchParams.set("next", pathname);
    return NextResponse.redirect(url);
  }
  // A fresh nonce for every page (#166, lib/csp.ts). Set on the request, it is what Next
  // puts on the scripts it renders; any policy the client sent is replaced.
  const csp = contentSecurityPolicy(newNonce(), {
    dev: process.env.NODE_ENV === "development",
    tls: edgeSecret() !== null,
  });
  const headers = new Headers(request.headers);
  headers.set("content-security-policy", csp);
  const response = NextResponse.next({ request: { headers } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  // Only framework paths (_next/ is never an app route) and real static files are skipped.
  // A pattern on file extensions also skipped dynamic routes ending in ".png" and the like,
  // and with them the edge check (#39). There is no public/ directory; add its files here
  // explicitly if one appears.
  matcher: ["/((?!api/|_next/|favicon\\.ico$).*)"],
};
