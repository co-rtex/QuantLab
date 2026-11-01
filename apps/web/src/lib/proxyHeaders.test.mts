// The proxy passes Retry-After on (#223 review B1): without it the browser saw a 503
// server_busy with no wait, and retried at once into a second refusal.
// Run with Node's built-in runner: `node --test src/lib/proxyHeaders.test.mts` (Node >= 22.18).
import assert from "node:assert/strict";
import { test } from "node:test";

type Headers = typeof import("./proxyHeaders");
const { FORWARD_RESPONSE } = (await import(new URL("./proxyHeaders.ts", import.meta.url).href)) as Headers;

test("the proxy forwards Retry-After, and still the headers it did", () => {
  for (const h of ["retry-after", "content-type", "x-request-id", "set-cookie", "content-disposition", "content-security-policy"]) {
    assert.ok(FORWARD_RESPONSE.includes(h), h);
  }
});

test("header names are lower-case, as the proxy reads them", () => {
  for (const h of FORWARD_RESPONSE) assert.equal(h, h.toLowerCase());
});
