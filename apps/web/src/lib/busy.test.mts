// A 503 server_busy is sent again once, after Retry-After (#223). The API refuses such a
// request before doing any of its work, so a retry is safe even for a POST; api() sends
// every request through sendRetryingBusy. Run with Node's built-in runner:
// `node --test src/lib/busy.test.mts` (Node >= 22.18).
import assert from "node:assert/strict";
import { test } from "node:test";

type Busy = typeof import("./busy");
// By URL, as csp.test.mts does: Node runs the .ts file, and tsc checks the types.
const { busyRetrySeconds, sendRetryingBusy } = (await import(new URL("./busy.ts", import.meta.url).href)) as Busy;

function busy(retryAfter = "0"): Response {
  return Response.json(
    { error: { code: "server_busy", message: "The server is busy with other analyses" } },
    { status: 503, headers: { "retry-after": retryAfter } },
  );
}

function sender(...responses: Response[]): { send: () => Promise<Response>; calls: () => number } {
  let n = 0;
  return {
    send: async () => {
      n += 1;
      return responses.shift() ?? Response.json({ ok: true });
    },
    calls: () => n,
  };
}

test("a busy server is sent the request again once, after Retry-After, and the caller is told", async () => {
  const s = sender(busy("0"), Response.json({ weights: 1 }));
  const told: number[] = [];
  const res = await sendRetryingBusy(s.send, (seconds) => told.push(seconds));
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { weights: 1 });
  assert.deepEqual(told, [0]);
  assert.equal(s.calls(), 2);
});

test("only once: a second refusal is returned for the caller to show", async () => {
  const s = sender(busy("0"), busy("0"));
  const res = await sendRetryingBusy(s.send);
  assert.equal(res.status, 503);
  assert.equal((await res.json()).error.code, "server_busy");
  assert.equal(s.calls(), 2);
});

test("any other 503, and any other status, is not sent again", async () => {
  for (const r of [
    Response.json({ error: { code: "upstream_unavailable", message: "down" } }, { status: 503 }),
    Response.json({ error: { code: "rate_limited", message: "slow down" } }, { status: 429 }),
  ]) {
    const s = sender(r);
    await sendRetryingBusy(s.send);
    assert.equal(s.calls(), 1);
  }
});

test("Retry-After is capped, so a page never waits long", () => {
  assert.equal(busyRetrySeconds(busy("3600"), "server_busy"), 10);
  assert.equal(busyRetrySeconds(busy("2"), "server_busy"), 2);
  assert.equal(busyRetrySeconds(busy("soon"), "server_busy"), 1);
  assert.equal(busyRetrySeconds(busy("2"), "other"), null);
});
