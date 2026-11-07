// The URL the Playwright suite runs against (#263). E2E_BASE_URL when set (make e2e-serial
// sets the checkout's own, from scripts/stack-env.sh). Unset, the main checkout keeps its
// default of http://localhost:3000; a linked git worktree refuses instead. That default
// is the main checkout's web tier, and a run from a worktree once aimed its test users and
// data at it, refused only because that stack was down.
//
// A linked worktree also refuses an explicit URL on a loopback host at port 3000 unless
// E2E_ALLOW_MAIN_CHECKOUT=1. stack-env.sh keeps a variable already set in the environment,
// so an E2E_BASE_URL left exported from the main checkout would otherwise pass straight
// through `make e2e-serial` in a worktree.
import { existsSync, statSync } from "node:fs";
import { dirname, join } from "node:path";

export const MAIN_CHECKOUT_URL = "http://localhost:3000";
const MAIN_CHECKOUT_PORT = "3000";
const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]", "0.0.0.0"]);

/** The nearest `.git` entry at or above `from`: the checkout's top, as `git rev-parse
 * --show-toplevel` would give it. Null outside any checkout (an exported tarball). */
export function gitEntry(from: string): string | null {
  for (let dir = from; ; dir = dirname(dir)) {
    const entry = join(dir, ".git");
    if (existsSync(entry)) return entry;
    if (dirname(dir) === dir) return null;
  }
}

/** A linked worktree's `.git` is a file (a directory in the main checkout), the same
 * test scripts/stack-env.sh uses. */
export function inLinkedWorktree(from: string): boolean {
  const entry = gitEntry(from);
  return entry !== null && statSync(entry).isFile();
}

/** Whether `url` names the main checkout's default web tier: a loopback host on :3000. */
export function isMainCheckoutUrl(url: URL): boolean {
  const port = url.port || (url.protocol === "https:" ? "443" : "80");
  return LOOPBACK.has(url.hostname) && port === MAIN_CHECKOUT_PORT;
}

export function e2eBaseUrl(env: Record<string, string | undefined>, from: string): string {
  const linked = inLinkedWorktree(from);
  const raw = env.E2E_BASE_URL;
  if (!raw) {
    if (linked) {
      throw new Error(
        "E2E_BASE_URL is not set, and this is a linked git worktree. Run `make e2e-serial` " +
          "(it sets this checkout's own URL) or set E2E_BASE_URL. Refusing the default " +
          `${MAIN_CHECKOUT_URL}, which is the main checkout's web tier (#263).`,
      );
    }
    return MAIN_CHECKOUT_URL;
  }
  let url: URL | null = null;
  try {
    url = new URL(raw);
  } catch {
    // reported below
  }
  // `new URL("localhost:20421")` parses, with "localhost:" as its scheme.
  if (url === null || (url.protocol !== "http:" && url.protocol !== "https:")) {
    throw new Error(`E2E_BASE_URL is not a valid http(s) URL: ${JSON.stringify(raw)} (#263).`);
  }
  if (linked && isMainCheckoutUrl(url) && env.E2E_ALLOW_MAIN_CHECKOUT !== "1") {
    throw new Error(
      `E2E_BASE_URL is ${raw}, the main checkout's web tier, and this is a linked git ` +
        "worktree. It may be left exported from the main checkout (stack-env.sh keeps a " +
        "variable already set). Unset it and run `make e2e-serial`, or set " +
        "E2E_ALLOW_MAIN_CHECKOUT=1 if that stack is really the target (#263).",
    );
  }
  return raw;
}
