import { NextResponse, type NextRequest } from "next/server";

// /invite handles signed-out visitors itself: it must keep the token from the URL fragment.
const PUBLIC = ["/login", "/register", "/invite"];

export function proxy(request: NextRequest) {
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
