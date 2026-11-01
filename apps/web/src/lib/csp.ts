// Content-Security-Policy for pages (#166). proxy.ts sends it with every page, with a fresh
// nonce, and sets it on the request too: Next.js reads the nonce from the request's policy
// while rendering and puts it on its own scripts and style elements. That works only when
// pages render per request, so the app renders dynamically (layout.tsx), and Partial
// Prerendering stays off: a prerendered shell's scripts could not carry the nonce.
//
// * Scripts: the nonce and 'strict-dynamic', so only the scripts Next renders run, plus
//   what they load. No 'unsafe-inline'. 'unsafe-eval' only in development, where React
//   uses eval to rebuild server error stacks.
// * Style elements: the nonce too. Style attributes stay allowed (style-src-attr
//   'unsafe-inline'), and only they:
//   - Needed: style attributes in server-rendered HTML, which no nonce can cover. Next's
//     built-in global-error page renders them, and so does every style={…} of ours that
//     is server-rendered (chart legends, the monthly-returns heatmap in
//     components/charts.tsx). Only a style={…} set after hydration goes through the
//     CSSOM, which CSP does not govern.
//   - Next's built-in error pages also carry a <style> element without the nonce, which
//     is refused: the 404 page is therefore our own (app/not-found.tsx).
//   - Narrow: an attribute cannot run script, load a stylesheet or @import; url() in it
//     is still held to img-src and font-src. A <style> element, which could carry
//     selectors that probe the page, still needs the nonce.
//   - Precondition for abuse: HTML injection, which React's escaping already prevents.
// * Everything else from this origin only: the API is reached through /api, and fonts
//   are self-hosted by next/font. No frames of this app anywhere (frame-ancestors).

export interface CspOptions {
  /** next dev: React needs eval, and the dev server injects styles without the nonce. */
  dev: boolean;
  /** Served through the TLS edge: subresources over http are upgraded. */
  tls: boolean;
}

/** 128 random bits, base64: what a page's nonce is (a new one for every response). */
export function newNonce(): string {
  return Buffer.from(crypto.getRandomValues(new Uint8Array(16))).toString("base64");
}

export function contentSecurityPolicy(nonce: string, { dev, tls }: CspOptions): string {
  const n = `'nonce-${nonce}'`;
  const directives: string[][] = [
    ["default-src", "'self'"],
    ["script-src", "'self'", n, "'strict-dynamic'", ...(dev ? ["'unsafe-eval'"] : [])],
    // style-src is the fallback for browsers without the -elem/-attr split.
    ["style-src", "'self'", n],
    ["style-src-elem", "'self'", ...(dev ? ["'unsafe-inline'"] : [n])],
    ["style-src-attr", "'unsafe-inline'"],
    ["img-src", "'self'", "data:", "blob:"],
    ["font-src", "'self'"],
    ["connect-src", "'self'"],
    ["object-src", "'none'"],
    ["base-uri", "'self'"],
    ["form-action", "'self'"],
    ["frame-ancestors", "'none'"],
    ...(tls ? [["upgrade-insecure-requests"]] : []),
  ];
  return directives.map((d) => d.join(" ")).join("; ");
}
