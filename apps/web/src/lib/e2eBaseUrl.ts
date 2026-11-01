// The URL the Playwright suite runs against (#263). E2E_BASE_URL when set (make e2e-serial
// sets the checkout's own, from scripts/stack-env.sh). Unset, the main checkout keeps its
// default of http://localhost:3000; a linked git worktree refuses instead. That default
// is the main checkout's web tier, and a run from a worktree once aimed its test users and
// data at it, refused only because that stack was down.
import { existsSync, statSync } from "node:fs";
import { dirname, join } from "node:path";

export const MAIN_CHECKOUT_URL = "http://localhost:3000";

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

export function e2eBaseUrl(env: Record<string, string | undefined>, from: string): string {
  if (env.E2E_BASE_URL) return env.E2E_BASE_URL;
  if (inLinkedWorktree(from)) {
    throw new Error(
      "E2E_BASE_URL is not set, and this is a linked git worktree. Run `make e2e-serial` " +
        "(it sets this checkout's own URL) or set E2E_BASE_URL. Refusing the default " +
        `${MAIN_CHECKOUT_URL}, which is the main checkout's web tier (#263).`,
    );
  }
  return MAIN_CHECKOUT_URL;
}
