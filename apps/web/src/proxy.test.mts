// Guard for proxy.ts's matcher (#8). Run with Node's built-in runner:
// `node --test src/proxy.test.mts` (Node >= 22.18).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

// proxy.ts imports next/server, so its source is checked instead of importing it.
const source = readFileSync(new URL("./proxy.ts", import.meta.url), "utf8");
const matcher = /matcher:\s*\[\s*"([^"]+)"\s*\]/.exec(source)?.[1];

function matches(path: string): boolean {
  assert.ok(matcher, "proxy.ts must declare exactly one matcher pattern");
  // Next matches the pattern against the whole path.
  return new RegExp(`^${matcher.replace(/\\\\/g, "\\")}$`).test(path);
}

test("the API proxy is never behind Next's proxy layer", () => {
  // Next buffers request bodies for its proxy layer and truncates them at 10 MB, which
  // would silently cut CSV uploads (up to 110 MiB). /api/ checks the edge itself.
  for (const path of ["/api/v1/workspaces/w/datasets/csv", "/api/v1/auth/login"]) {
    assert.equal(matches(path), false, path);
  }
});

test("pages, including dynamic paths that look like files, are behind it", () => {
  for (const path of ["/", "/login", "/invite", "/data/abc.png", "/experiments/x.svg"]) {
    assert.equal(matches(path), true, path);
  }
  for (const path of ["/_next/static/chunk.js", "/favicon.ico"]) {
    assert.equal(matches(path), false, path);
  }
});
