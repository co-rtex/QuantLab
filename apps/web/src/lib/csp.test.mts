// The pages' Content-Security-Policy (#166). Run with Node's built-in runner:
// `node --test src/lib/csp.test.mts` (Node >= 22.18). e2e/csp.spec.ts checks it in a browser.
import assert from "node:assert/strict";
import { test } from "node:test";

type Csp = typeof import("./csp");
// By URL, as edge.test.mts does: Node runs the .ts file, and tsc checks the types.
const { contentSecurityPolicy, newNonce } = (await import(
  new URL("./csp.ts", import.meta.url).href
)) as Csp;

function directives(policy: string): Map<string, string[]> {
  return new Map(
    policy.split(";").map((d) => {
      const [name, ...values] = d.trim().split(/\s+/);
      return [name, values] as [string, string[]];
    }),
  );
}

const PROD = { dev: false, tls: false };

test("scripts need the nonce: no 'unsafe-inline' and, in production, no 'unsafe-eval'", () => {
  const d = directives(contentSecurityPolicy("abc", PROD));
  assert.deepEqual(d.get("script-src"), ["'self'", "'nonce-abc'", "'strict-dynamic'"]);
  for (const name of ["script-src", "style-src", "style-src-elem", "default-src"]) {
    assert.ok(!d.get(name)?.includes("'unsafe-inline'"), name);
    assert.ok(!d.get(name)?.includes("'unsafe-eval'"), name);
  }
});

test("style elements need the nonce; only style attributes are allowed inline", () => {
  const d = directives(contentSecurityPolicy("abc", PROD));
  assert.deepEqual(d.get("style-src-elem"), ["'self'", "'nonce-abc'"]);
  assert.deepEqual(d.get("style-src-attr"), ["'unsafe-inline'"]);
});

test("nothing frames the app, and nothing loads from elsewhere", () => {
  const d = directives(contentSecurityPolicy("abc", PROD));
  assert.deepEqual(d.get("frame-ancestors"), ["'none'"]);
  assert.deepEqual(d.get("object-src"), ["'none'"]);
  for (const name of ["default-src", "connect-src", "font-src", "base-uri", "form-action"]) {
    assert.deepEqual(d.get(name), ["'self'"], name);
  }
  assert.deepEqual(d.get("img-src"), ["'self'", "data:", "blob:"]);
});

test("only development allows eval and inline style elements", () => {
  const d = directives(contentSecurityPolicy("abc", { dev: true, tls: false }));
  assert.ok(d.get("script-src")?.includes("'unsafe-eval'"));
  assert.deepEqual(d.get("style-src-elem"), ["'self'", "'unsafe-inline'"]);
});

test("insecure requests are upgraded only behind the TLS edge", () => {
  assert.ok(!directives(contentSecurityPolicy("abc", PROD)).has("upgrade-insecure-requests"));
  assert.ok(directives(contentSecurityPolicy("abc", { dev: false, tls: true })).has("upgrade-insecure-requests"));
});

test("a nonce is 128 random bits, and never repeats", () => {
  const seen = new Set(Array.from({ length: 1000 }, newNonce));
  assert.equal(seen.size, 1000);
  for (const n of seen) {
    assert.equal(Buffer.from(n, "base64").length, 16);
    assert.match(n, /^[A-Za-z0-9+/]{22}==$/); // safe inside 'nonce-…'
  }
});
