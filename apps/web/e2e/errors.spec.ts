import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";

// #18: a failed request shows an accessible error with the request id and a Retry button,
// where panels used to stay empty or show a skeleton forever. Failures are injected by
// intercepting the browser's API calls, so the backend stays healthy.

const RID = "req-e2e-18";

function fail(status = 500) {
  return (route: Route) =>
    route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "internal_error", message: "Internal server error", request_id: RID } }),
    });
}

async function register(page: Page, tag: string) {
  await page.goto("/register");
  await page.getByLabel("Name").fill("Errors");
  await page.getByLabel("Email").fill(`errors-${tag}-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("Failure-modes-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
}

async function audit(page: Page, name: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const summary = results.violations.map((v) => `${v.id} (${v.impact}): ${v.help} [${v.nodes.map((n) => `${n.target.join(" ")}: ${n.failureSummary ?? ""}`).join("; ")}]`);
  expect(summary, `${name}\n${summary.join("\n")}`).toEqual([]);
}

/** The whole note: its live region (message and sentence) plus the request id and Retry. */
function errorNote(page: Page, text: string) {
  return page.getByRole("alert").filter({ hasText: text }).locator("..");
}

test("a failed workspace load is shown with Retry instead of an empty app", async ({ page }) => {
  await register(page, "ws");
  await page.route("**/api/v1/workspaces", fail(503));
  await page.reload();
  const note = errorNote(page, "Could not load your account and workspaces.");
  await expect(note).toBeVisible();
  await expect(note).toContainText(`Request ${RID}`);
  await audit(page, "overview, workspaces failed");

  await page.unroute("**/api/v1/workspaces");
  await note.getByRole("button", { name: "Retry" }).click();
  await expect(note).toHaveCount(0);
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
  // The Retry button is gone; focus goes to the page heading, not to the document body.
  await expect(page.locator("main h1")).toBeFocused();
});

test("list panels show the failure and recover on Retry", async ({ page }) => {
  await register(page, "lists");

  // Compare does not poll, so only Retry can bring it back.
  const completed = "**/api/v1/workspaces/*/experiments?status=completed*";
  await page.route(completed, fail());
  await page.goto("/compare");
  const note = errorNote(page, "Could not load completed experiments.");
  await expect(note).toBeVisible();
  await expect(note).toContainText(`Request ${RID}`);
  await expect(page.getByRole("status", { name: "Loading" })).toHaveCount(0); // no skeleton forever
  await audit(page, "compare, list failed");
  await page.unroute(completed);
  await note.getByRole("button", { name: "Retry" }).click();
  await expect(note).toHaveCount(0);
  await expect(page.getByText("Nothing to compare yet.")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Completed experiments" })).toBeFocused();

  await page.route("**/api/v1/workspaces/*/ml-runs*", fail());
  await page.goto("/ml");
  await expect(errorNote(page, "Could not load ML runs.")).toBeVisible();
  await audit(page, "ml, runs failed");

});

test("a polling panel announces an outage once, not on every failed poll", async ({ page }) => {
  await register(page, "poll");
  // Like the API: every failed response carries a new request id.
  let n = 0;
  await page.route("**/api/v1/workspaces/*/experiments?*", (route) =>
    route.fulfill({
      status: 502,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "bad_gateway", message: "Bad gateway", request_id: `req-poll-${++n}` } }),
    }),
  );
  await page.goto("/experiments");
  const note = errorNote(page, "Could not load experiments.");
  await expect(note).toContainText("Request req-poll-");
  const first = await note.getByText(/^Request req-poll-/).textContent();
  // Count every change inside the live region from here on.
  await page.getByRole("alert").filter({ hasText: "Could not load experiments." }).evaluate((el) => {
    const w = window as unknown as { liveChanges: number };
    w.liveChanges = 0;
    new MutationObserver((records) => (w.liveChanges += records.length)).observe(el, { subtree: true, childList: true, characterData: true, attributes: true });
  });
  // The list polls with backoff; wait for a later failure with a new request id.
  await expect.poll(async () => note.getByText(/^Request req-poll-/).textContent(), { timeout: 30_000 }).not.toBe(first);
  expect(await page.evaluate(() => (window as unknown as { liveChanges: number }).liveChanges)).toBe(0);
  await expect(page.getByRole("alert").filter({ hasText: "Could not load experiments." })).not.toContainText("req-poll-");
  await audit(page, "experiments, polling failed");
});

test("a failed analysis on a finished experiment is shown, not left as a skeleton", async ({ page }) => {
  await register(page, "exp");
  const ws = (await (await page.request.get("/api/v1/workspaces")).json())[0].id;
  const job = await (
    await page.request.post(`/api/v1/workspaces/${ws}/datasets/synthetic`, {
      data: { slug: `errors-${Date.now()}`, name: "Errors data", n_assets: 8, start: "2020-01-02", end: "2021-12-31" },
    })
  ).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 30_000 }).toBe("completed");
  const dv = (await (await page.request.get(`/api/v1/workspaces/${ws}/dataset-versions`)).json())[0].id;
  const sub = await (
    await page.request.post(`/api/v1/workspaces/${ws}/experiments`, {
      data: { name: "Errors run", dataset_version_id: dv, strategy_key: "equal_weight", config: { start: "2021-01-04", benchmark: "SYNIDX" } },
    })
  ).json();
  const id = sub.experiment.id;
  await expect.poll(async () => (await (await page.request.get(`/api/v1/experiments/${id}`)).json()).status, { timeout: 60_000 }).toBe("completed");

  // The analysis carries performance, factor exposures, tail risk and contributions.
  await page.route("**/api/v1/experiments/*/analysis", fail());
  await page.route("**/api/v1/experiments/*/equity", fail());
  await page.goto(`/experiments/${id}`);
  const analysis = errorNote(page, "Could not load performance analysis.");
  await expect(analysis).toBeVisible();
  await expect(errorNote(page, "Could not load the equity curve.")).toBeVisible();
  await audit(page, "experiment, analysis failed");

  await page.unroute("**/api/v1/experiments/*/analysis");
  await analysis.getByRole("button", { name: "Retry" }).click();
  await expect(analysis).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Performance" })).toBeFocused();
  await expect(page.getByRole("heading", { name: "Largest drawdowns" })).toBeVisible();
});
