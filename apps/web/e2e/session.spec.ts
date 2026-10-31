import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// #23: E2E coverage for signing out, a session that expires mid-use, and a failed job.

const password = "Session-flows-2026";

async function audit(page: Page, name: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const summary = results.violations.map((v) => `${v.id} (${v.impact}): ${v.help} [${v.nodes.map((n) => n.target.join(" ")).join("; ")}]`);
  expect(summary, `${name}\n${summary.join("\n")}`).toEqual([]);
}

async function register(page: Page, tag: string) {
  const email = `session-${tag}-${Date.now()}@example.com`;
  await page.goto("/register");
  await page.getByLabel("Name").fill("Session");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
  return email;
}

test("signing out ends the session", async ({ page }) => {
  await register(page, "logout");
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
  expect((await page.request.get("/api/v1/auth/me")).status()).toBe(401);
  await page.goto("/experiments");
  await expect(page).toHaveURL(/\/login/);
});

test("a session that expires mid-use returns to the same page after signing in", async ({ page }) => {
  const email = await register(page, "expiry");
  await page.goto("/experiments");
  await expect(page.getByRole("heading", { level: 1, name: "Experiments" })).toBeVisible();
  // The session ends while the page is open (expiry, or a sign-out elsewhere).
  await page.context().clearCookies();
  // The list polls; its next request gets 401 and the app goes to the login page.
  await expect(page).toHaveURL(/\/login\?next=%2Fexperiments/, { timeout: 30_000 });
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL(/\/experiments$/);
  await expect(page.getByRole("heading", { level: 1, name: "Experiments" })).toBeVisible();
});

test("a failed backtest shows why", async ({ page }) => {
  test.setTimeout(120_000);
  await register(page, "failure");
  const ws = (await (await page.request.get("/api/v1/workspaces")).json())[0].id as string;
  const job = await (
    await page.request.post(`/api/v1/workspaces/${ws}/datasets/synthetic`, {
      data: { slug: `session-${Date.now()}`, name: "Session data", n_assets: 5, start: "2021-01-04", end: "2021-12-31" },
    })
  ).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 60_000 }).toBe("completed");
  const dv = (await (await page.request.get(`/api/v1/workspaces/${ws}/dataset-versions`)).json())[0].id as string;
  const sub = await (
    await page.request.post(`/api/v1/workspaces/${ws}/experiments`, {
      data: { name: "Doomed run", dataset_version_id: dv, strategy_key: "equal_weight", config: { start: "2021-03-01", benchmark: "SYNIDX" } },
    })
  ).json();
  const id = sub.experiment.id as string;

  // The worker's failure, as the API reports it: the experiment and its job both failed.
  const error = { type: "DataError", message: "No bars on or after the start date" };
  await page.route(`**/api/v1/experiments/${id}`, async (route) => {
    const e = await (await route.fetch()).json();
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...e, status: "failed" }) });
  });
  await page.route(`**/api/v1/experiments/${id}/job`, async (route) => {
    const j = await (await route.fetch()).json();
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...j, status: "failed", error, attempts: j.max_attempts, message: "failed" }) });
  });
  await page.goto(`/experiments/${id}`);
  await expect(page.getByText("Failed", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("DataError: No bars on or after the start date")).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel run" })).toHaveCount(0);
  await audit(page, "experiment, failed run");
});
