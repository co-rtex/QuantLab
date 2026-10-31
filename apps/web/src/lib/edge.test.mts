// Unit tests for the web tier's edge checks (#3). Run with Node's built-in runner, which
// strips the types itself: `node --test src/lib/edge.test.mts` (Node >= 22.18).
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

type Edge = typeof import("./edge");
// A computed specifier keeps tsc from requiring allowImportingTsExtensions.
const { edgeClientAddress, fromEdge } = (await import(
  new URL("./edge.ts", import.meta.url).href
)) as Edge;

const SECRET = "s".repeat(64);

function h(entries: [string, string][]): Headers {
  const headers = new Headers();
  for (const [k, v] of entries) headers.append(k, v);
  return headers;
}

afterEach(() => {
  delete process.env.QUANTLAB_EDGE_SECRET;
});

test("without an edge every request is served but no address is forwarded", () => {
  const spoofed = h([["x-forwarded-for", "198.51.100.7"]]);
  assert.equal(fromEdge(spoofed), true);
  assert.equal(edgeClientAddress(spoofed), null);
});

test("an empty secret means no edge, not an empty password", () => {
  process.env.QUANTLAB_EDGE_SECRET = "";
  assert.equal(fromEdge(h([["x-quantlab-edge", ""]])), true);
  assert.equal(edgeClientAddress(h([["x-quantlab-edge", ""], ["x-forwarded-for", "1.2.3.4"]])), null);
});

test("behind the edge only requests with the exact secret are served", () => {
  process.env.QUANTLAB_EDGE_SECRET = SECRET;
  assert.equal(fromEdge(h([])), false);
  assert.equal(fromEdge(h([["x-quantlab-edge", ""]])), false);
  assert.equal(fromEdge(h([["x-quantlab-edge", SECRET.slice(1)]])), false);
  assert.equal(fromEdge(h([["x-quantlab-edge", SECRET + "x"]])), false);
  assert.equal(fromEdge(h([["X-QuantLab-Edge", SECRET]])), true);
  // A second (client-added) value is joined with ", " by Headers and no longer matches.
  assert.equal(fromEdge(h([["x-quantlab-edge", SECRET], ["x-quantlab-edge", "x"]])), false);
});

test("behind the edge the single address it wrote is forwarded", () => {
  process.env.QUANTLAB_EDGE_SECRET = SECRET;
  const ok = (xff: string) => edgeClientAddress(h([["x-quantlab-edge", SECRET], ["x-forwarded-for", xff]]));
  assert.equal(ok("198.51.100.7"), "198.51.100.7");
  assert.equal(ok("2001:db8::1"), "2001:db8::1");
  assert.equal(ok(" 198.51.100.7 "), "198.51.100.7");
  // Anything but exactly one address did not come from the edge.
  assert.equal(ok("6.6.6.6, 198.51.100.7"), null);
  assert.equal(ok("not-an-ip"), null);
  assert.equal(ok(""), null);
  // Without the secret, even a well-formed address is not forwarded.
  assert.equal(edgeClientAddress(h([["x-forwarded-for", "198.51.100.7"]])), null);
});
