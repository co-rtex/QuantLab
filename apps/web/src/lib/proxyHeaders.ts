// The API response headers the same-origin proxy (app/api/[...path]/route.ts) passes on; any
// other is dropped. Kept here so Node's test runner can check it (proxyHeaders.test.mts).
// * content-security-policy: the API serves HTML reports under a sandboxing CSP; dropping it
//   served them same-origin with the app and no policy.
// * retry-after: a 503 server_busy says when to try again (#223). Dropped, the browser retried
//   at once, into a second refusal.
export const FORWARD_RESPONSE = [
  "content-type",
  "x-request-id",
  "set-cookie",
  "content-disposition",
  "content-security-policy",
  "retry-after",
];
