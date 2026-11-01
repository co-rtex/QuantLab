// api(): a 503 server_busy is sent again once, after Retry-After (#223). The API refuses
// such a request before doing any of its work, so a retry is safe even for a POST.
// Run with Node's built-in runner: `node --test src/lib/api.test.mts` (Node >= 22.18).
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

type Api = typeof import("./api");
// By URL, as csp.test.mts does: Node runs the .ts file, and tsc checks the types.
const { api, ApiError } = (await import(new URL("./api.ts", import.meta.url).href)) as Api;

const real = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = real;
});

function busy(retryAfter = "0"): Response {
  return Response.json(
    { error: { code: "server_busy", message: "The server is busy with other analyses" } },
    { status: 503, headers: { "retry-after": retryAfter } },
  );
}

function respond(...responses: Response[]): { calls: RequestInit[] } {
  const calls: RequestInit[] = [];
  globalThis.fetch = (async (_url: string, init: RequestInit) => {
    calls.push(init);
    return responses.shift() ?? Response.json({ ok: true });
  }) as typeof fetch;
  return { calls };
}

test("a busy server is retried once after Retry-After, and the caller is told", async () => {
  const sent = respond(busy("0"), Response.json({ weights: 1 }));
  const told: number[] = [];
  const out = await api<{ weights: number }>("/x/optimize", { method: "POST", json: { a: 1 }, onBusy: (s) => told.push(s) });
  assert.deepEqual(out, { weights: 1 });
  assert.deepEqual(told, [0]);
  assert.equal(sent.calls.length, 2);
  assert.equal(sent.calls[1].body, JSON.stringify({ a: 1 })); // the same request again
});

test("only once: a second refusal is the caller's to show", async () => {
  const sent = respond(busy("0"), busy("0"));
  await assert.rejects(api("/x/valuation"), (e: unknown) => e instanceof ApiError && e.status === 503 && e.code === "server_busy");
  assert.equal(sent.calls.length, 2);
});

test("any other 503 is not retried", async () => {
  const sent = respond(Response.json({ error: { code: "upstream_unavailable", message: "down" } }, { status: 503 }));
  await assert.rejects(api("/x"), (e: unknown) => e instanceof ApiError && e.code === "upstream_unavailable");
  assert.equal(sent.calls.length, 1);
});

test("Retry-After is capped, so a page never waits long", async () => {
  const { busyRetrySeconds } = (await import(new URL("./api.ts", import.meta.url).href)) as Api;
  assert.equal(busyRetrySeconds(busy("3600"), "server_busy"), 10);
  assert.equal(busyRetrySeconds(busy("2"), "server_busy"), 2);
  assert.equal(busyRetrySeconds(busy("soon"), "server_busy"), 1);
  assert.equal(busyRetrySeconds(busy("2"), "other"), null);
});
