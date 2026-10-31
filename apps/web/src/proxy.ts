import { NextResponse, type NextRequest } from "next/server";

import { edgeRequiredResponse, fromEdge } from "@/lib/edge";

const PUBLIC = ["/login", "/register"];

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
  matcher: ["/((?!api/|_next/|favicon.ico|.*\\.(?:png|svg|ico|txt)$).*)"],
};
