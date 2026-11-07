// Playwright's base URL never defaults to the main checkout's stack from a linked worktree
// (#263). Run with Node's built-in runner: `node --test src/lib/e2eBaseUrl.test.mts`.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

type Mod = typeof import("./e2eBaseUrl");
// By URL, as csp.test.mts does: Node runs the .ts file, and tsc checks the types.
const { e2eBaseUrl, inLinkedWorktree, MAIN_CHECKOUT_URL } = (await import(new URL("./e2eBaseUrl.ts", import.meta.url).href)) as Mod;

/** A throwaway checkout whose top has `.git` as given, and its apps/web directory. */
function checkout(git: "dir" | "file" | "none"): { web: string; done: () => void } {
  const top = mkdtempSync(join(tmpdir(), "e2e-url-"));
  if (git === "dir") mkdirSync(join(top, ".git"));
  if (git === "file") writeFileSync(join(top, ".git"), "gitdir: /elsewhere/.git/worktrees/probe\n");
  const web = join(top, "apps", "web");
  mkdirSync(web, { recursive: true });
  return { web, done: () => rmSync(top, { recursive: true, force: true }) };
}

test("the main checkout keeps its default of :3000", () => {
  const c = checkout("dir");
  try {
    assert.equal(inLinkedWorktree(c.web), false);
    assert.equal(e2eBaseUrl({}, c.web), MAIN_CHECKOUT_URL);
    assert.equal(MAIN_CHECKOUT_URL, "http://localhost:3000");
  } finally {
    c.done();
  }
});

test("a linked worktree without E2E_BASE_URL refuses to run", () => {
  const c = checkout("file");
  try {
    assert.equal(inLinkedWorktree(c.web), true);
    assert.throws(() => e2eBaseUrl({}, c.web), /make e2e-serial[\s\S]*main checkout's web tier/);
    assert.throws(() => e2eBaseUrl({ E2E_BASE_URL: "" }, c.web), /E2E_BASE_URL is not set/);
  } finally {
    c.done();
  }
});

test("an explicit E2E_BASE_URL is used in either checkout", () => {
  for (const git of ["dir", "file"] as const) {
    const c = checkout(git);
    try {
      assert.equal(e2eBaseUrl({ E2E_BASE_URL: "http://localhost:20421" }, c.web), "http://localhost:20421");
    } finally {
      c.done();
    }
  }
});

test("a linked worktree refuses an explicit URL at the main checkout's :3000", () => {
  // stack-env.sh keeps a variable already exported, so a leftover main-checkout URL would
  // otherwise reach `make e2e-serial` in a worktree.
  const c = checkout("file");
  try {
    for (const url of [
      "http://localhost:3000",
      "http://localhost:3000/",
      "http://127.0.0.1:3000/login",
      "http://[::1]:3000",
      "http://0.0.0.0:3000",
    ]) {
      assert.throws(() => e2eBaseUrl({ E2E_BASE_URL: url }, c.web), /E2E_ALLOW_MAIN_CHECKOUT=1/, url);
    }
    // An explicit opt-in is honoured; any other port or host is the worktree's own choice.
    const allow = { E2E_BASE_URL: "http://localhost:3000", E2E_ALLOW_MAIN_CHECKOUT: "1" };
    assert.equal(e2eBaseUrl(allow, c.web), "http://localhost:3000");
    assert.throws(() => e2eBaseUrl({ ...allow, E2E_ALLOW_MAIN_CHECKOUT: "yes" }, c.web));
    assert.equal(e2eBaseUrl({ E2E_BASE_URL: "http://localhost:30001" }, c.web), "http://localhost:30001");
    assert.equal(e2eBaseUrl({ E2E_BASE_URL: "https://edge.test:3000" }, c.web), "https://edge.test:3000");
  } finally {
    c.done();
  }
});

test("the main checkout may name its own :3000 explicitly", () => {
  const c = checkout("dir");
  try {
    assert.equal(e2eBaseUrl({ E2E_BASE_URL: "http://127.0.0.1:3000" }, c.web), "http://127.0.0.1:3000");
  } finally {
    c.done();
  }
});

test("a malformed E2E_BASE_URL fails at config load in either checkout", () => {
  for (const git of ["dir", "file"] as const) {
    const c = checkout(git);
    try {
      assert.throws(() => e2eBaseUrl({ E2E_BASE_URL: "localhost:20421" }, c.web), /not a valid http\(s\) URL/);
    } finally {
      c.done();
    }
  }
});

test("outside any checkout the default stands, as stack-env falls back", () => {
  const c = checkout("none");
  try {
    // No .git anywhere above a fresh temp dir (unless the temp dir itself sits in one).
    if (!inLinkedWorktree(c.web)) assert.equal(e2eBaseUrl({}, c.web), MAIN_CHECKOUT_URL);
  } finally {
    c.done();
  }
});

test("this repository's own linked worktree is detected", () => {
  // The suite runs in this checkout: from a linked worktree, the guard must engage there.
  const here = new URL(".", import.meta.url).pathname;
  const linked = inLinkedWorktree(here);
  if (linked) assert.throws(() => e2eBaseUrl({}, here));
  else assert.equal(e2eBaseUrl({}, here), MAIN_CHECKOUT_URL);
});
