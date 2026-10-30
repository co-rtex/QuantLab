/** Return a same-origin path for a post-login redirect, or "/" if `next` is unsafe. */
export function safeNext(next: string | null, origin: string): string {
  if (!next || /[\\\u0000-\u001f]/.test(next)) return "/";
  try {
    const url = new URL(next, origin);
    if (url.origin !== origin) return "/";
    return url.pathname + url.search + url.hash;
  } catch {
    return "/";
  }
}
