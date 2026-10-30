/** Return a same-origin path for a post-login redirect, or "/" if `next` is unsafe. */
export function safeNext(next: string | null, origin: string): string {
  if (!next || /[\\\u0000-\u001f]/.test(next)) return "/";
  try {
    const url = new URL(next, origin);
    if (url.origin !== origin) return "/";
    const path = url.pathname + url.search + url.hash;
    // Normalization can yield "//host" (e.g. from "/.//host"), which a browser would treat
    // as protocol-relative. Accept only paths with exactly one leading slash.
    if (!path.startsWith("/") || path.startsWith("//")) return "/";
    return path;
  } catch {
    return "/";
  }
}
