import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

async function audit(page: Page, name: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const summary = results.violations.map((v) => `${v.id} (${v.impact}): ${v.nodes.length} node(s) — ${v.help}`);
  expect(summary, `${name}\n${summary.join("\n")}`).toEqual([]);
}

test("core pages have no WCAG 2.1 AA violations detected by axe", async ({ page }) => {
  await page.goto("/login");
  await audit(page, "login");
  const email = `a11y-${Date.now()}@example.com`;
  await page.goto("/register");
  await audit(page, "register");
  await page.getByLabel("Name").fill("A11y");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("Accessible-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
  await audit(page, "overview (empty)");

  const ws = (await (await page.request.get("/api/v1/workspaces")).json())[0].id;
  const job = await (await page.request.post(`/api/v1/workspaces/${ws}/datasets/synthetic`, {
    data: { slug: `a11y-${Date.now()}`, name: "A11y data", n_assets: 12, start: "2020-01-02", end: "2022-12-30" },
  })).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 30_000 }).toBe("completed");
  const dv = (await (await page.request.get(`/api/v1/workspaces/${ws}/dataset-versions`)).json())[0].id;

  for (const path of ["/data", `/data/${dv}`, "/experiments/new", "/portfolios", "/optimize", "/ml", "/compare", "/settings"]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    await audit(page, path);
  }

  const sub = await (await page.request.post(`/api/v1/workspaces/${ws}/experiments`, {
    data: { name: "A11y run", dataset_version_id: dv, strategy_key: "equal_weight", config: { start: "2021-01-04", benchmark: "SYNIDX" } },
  })).json();
  await page.goto(`/experiments/${sub.experiment.id}`);
  await expect(page.getByRole("heading", { name: "Largest drawdowns" })).toBeVisible({ timeout: 60_000 });
  await audit(page, "experiment results");
});
