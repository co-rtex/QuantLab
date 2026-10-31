import { NextResponse, type NextRequest } from "next/server";

import { edgeRequiredResponse, fromEdge } from "@/lib/edge";

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
  return NextResponse.next();
}

export const config = {
  // Only framework paths (_next/ is never an app route) and real static files are skipped.
  // A pattern on file extensions also skipped dynamic routes ending in ".png" and the like,
  // and with them the edge check (#39). There is no public/ directory; add its files here
  // explicitly if one appears.
  matcher: ["/((?!api/|_next/|favicon\\.ico$).*)"],
};
