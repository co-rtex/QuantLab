import { expect, test, type Page, type Response } from "@playwright/test";

// #166: every page is sent with a Content-Security-Policy whose script nonce is new each
// time, and the browser refuses nothing on any page: every script the page needs carries
// that nonce. Unit rules for the policy itself: src/lib/csp.test.mts.

declare global {
  interface Window {
    __cspViolations: string[];
  }
}

async function watchViolations(page: Page) {
  await page.addInitScript(() => {
    window.__cspViolations = [];
    document.addEventListener("securitypolicyviolation", (e) => {
      window.__cspViolations.push(`${e.violatedDirective} ${e.blockedURI || "inline"} ${e.sourceFile}:${e.lineNumber}`);
    });
  });
}

async function violations(page: Page): Promise<string[]> {
  return page.evaluate(() => window.__cspViolations ?? []);
}

function directive(policy: string, name: string): string[] {
  const d = policy.split(";").map((s) => s.trim().split(/\s+/)).find(([n]) => n === name);
  return d ? d.slice(1) : [];
}

/** The page response's policy, and that every script the server sent carries its nonce. */
async function checkPage(response: Response | null, path: string): Promise<string> {
  expect(response, path).not.toBeNull();
  const policy = response!.headers()["content-security-policy"];
  expect(policy, `${path}: no Content-Security-Policy`).toBeTruthy();
  const scripts = directive(policy, "script-src");
  const nonce = scripts.find((s) => s.startsWith("'nonce-"))?.slice("'nonce-".length, -1);
  expect(nonce, `${path}: no nonce in ${policy}`).toBeTruthy();
  expect(scripts, path).not.toContain("'unsafe-inline'");
  expect(scripts, path).not.toContain("'unsafe-eval'");
  expect(directive(policy, "frame-ancestors"), path).toEqual(["'none'"]);
  // Never cached: a cached page would replay its nonce, which then no longer proves that
  // a script came from this response.
  expect(response!.headers()["cache-control"] ?? "", `${path}: cacheable`).toContain("no-store");
  const html = await response!.text();
  const tags = html.match(/<script\b[^>]*>/g) ?? [];
  expect(tags.length, `${path}: no scripts?`).toBeGreaterThan(0);
  for (const tag of tags) {
    expect(tag, `${path}: a script without this response's nonce`).toContain(`nonce="${nonce}"`);
  }
  return nonce!;
}

test("pages are sent with a nonce policy and the browser refuses nothing on them", async ({ page }) => {
  test.setTimeout(120_000);
  await watchViolations(page);

  const first = await checkPage(await page.goto("/login"), "/login");
  const second = await checkPage(await page.goto("/login"), "/login again");
  expect(second, "the nonce must be new for every response").not.toBe(first);
  await checkPage(await page.goto("/register"), "/register");
  await checkPage(await page.goto("/invite"), "/invite");
  expect(await violations(page)).toEqual([]);

  await page.goto("/register");
  await page.getByLabel("Name").fill("CSP");
  await page.getByLabel("Email").fill(`csp-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("Content-Policy-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
  expect(await violations(page), "registering and the client-side move to /").toEqual([]);

  const ws = (await (await page.request.get("/api/v1/workspaces")).json())[0].id;
  const job = await (await page.request.post(`/api/v1/workspaces/${ws}/datasets/synthetic`, {
    data: { slug: `csp-${Date.now()}`, name: "CSP data", n_assets: 12, start: "2020-01-02", end: "2022-12-30" },
  })).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 60_000 }).toBe("completed");
  const dv = (await (await page.request.get(`/api/v1/workspaces/${ws}/dataset-versions`)).json())[0].id;

  for (const path of ["/", "/data", `/data/${dv}`, "/experiments", "/experiments/new", "/portfolios", "/optimize", "/ml", "/compare", "/settings"]) {
    await checkPage(await page.goto(path), path);
    await expect(page.locator("main")).toBeVisible();
    expect(await violations(page), path).toEqual([]);
  }

  // Next's not-found page, signed in (signed out it is a redirect to /login): it renders
  // style attributes in its HTML, which style-src-attr allows (lib/csp.ts).
  const missing = await page.goto("/no-such-page");
  expect(missing?.status()).toBe(404);
  await checkPage(missing, "/no-such-page");
  expect(await violations(page), "/no-such-page").toEqual([]);

  // The chart pages, once their charts have rendered: an experiment and a portfolio.
  const run = await (await page.request.post(`/api/v1/workspaces/${ws}/experiments`, {
    data: { name: "CSP run", dataset_version_id: dv, strategy_key: "equal_weight", config: { start: "2020-03-02", benchmark: "SYNIDX" } },
  })).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/jobs/${run.job.id}`)).json()).status, { timeout: 90_000 }).toBe("completed");
  const pf = await (await page.request.post(`/api/v1/workspaces/${ws}/portfolios`, {
    data: { name: "CSP portfolio", dataset_version_id: dv, initial_deposit: 100_000, start_date: "2020-01-02" },
  })).json();
  expect((await page.request.post(`/api/v1/portfolios/${pf.id}/transactions`, {
    data: { trade_date: "2020-01-03", kind: "buy", symbol: "SYN001", quantity: 100 },
  })).ok()).toBe(true);
  for (const path of [`/experiments/${run.experiment.id}`, `/portfolios/${pf.id}`]) {
    await checkPage(await page.goto(path), path);
    await expect(page.getByRole("figure", { name: "Portfolio value and drawdown over time" })).toBeVisible({ timeout: 30_000 });
    expect(await violations(page), `${path} after its chart rendered`).toEqual([]);
  }

  // A client-side navigation loads further chunks: allowed through 'strict-dynamic'.
  await page.goto("/");
  await page.getByRole("link", { name: "Experiments" }).first().click();
  await expect(page).toHaveURL(/\/experiments$/);
  expect(await violations(page), "client-side navigation").toEqual([]);
});

test("an API response is not a page and carries no page policy", async ({ page }) => {
  const r = await page.request.get("/api/v1/health");
  expect(r.ok()).toBeTruthy();
  expect(r.headers()["content-security-policy"]).toBeUndefined();
});
