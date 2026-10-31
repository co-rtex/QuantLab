import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// #23: save a strategy with its parameters, start an experiment from it, delete it.

async function audit(page: Page, name: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const summary = results.violations.map((v) => `${v.id} (${v.impact}): ${v.help} [${v.nodes.map((n) => n.target.join(" ")).join("; ")}]`);
  expect(summary, `${name}\n${summary.join("\n")}`).toEqual([]);
}

test("a saved strategy keeps its parameters and can be loaded and deleted", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/register");
  await page.getByLabel("Name").fill("Strategist");
  await page.getByLabel("Email").fill(`strategies-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("Saved-strategies-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
  const ws = (await (await page.request.get("/api/v1/workspaces")).json())[0].id as string;
  const job = await (
    await page.request.post(`/api/v1/workspaces/${ws}/datasets/synthetic`, {
      data: { slug: `strategies-${Date.now()}`, name: "Strategy data", n_assets: 5, start: "2021-01-04", end: "2021-12-31" },
    })
  ).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 60_000 }).toBe("completed");

  await page.goto("/experiments/new");
  await page.getByText("Cross-Sectional Momentum", { exact: true }).click();
  await page.getByRole("spinbutton", { name: /^Lookback/ }).fill("120");
  const saveAs = page.getByLabel("Save this strategy and its parameters as");
  await saveAs.fill("Short momentum");
  await saveAs.press("Enter"); // saves; must not run the backtest
  await expect(page.getByRole("status").filter({ hasText: "Saved as Short momentum." })).toBeVisible();
  await expect(page).toHaveURL(/\/experiments\/new/);
  const stored = await (await page.request.get(`/api/v1/workspaces/${ws}/strategies`)).json();
  expect(stored).toHaveLength(1);
  expect(stored[0]).toMatchObject({ name: "Short momentum", strategy_key: "momentum" });
  expect(stored[0].params.lookback).toBe(120);
  expect((await (await page.request.get(`/api/v1/workspaces/${ws}/experiments`)).json()).total).toBe(0);
  await audit(page, "new experiment, saved strategies");

  await page.reload();
  await page.getByLabel("Start from a saved strategy").selectOption({ label: "Short momentum · Cross-Sectional Momentum" });
  await expect(page.getByRole("status").filter({ hasText: "Loaded Short momentum." })).toBeVisible();
  await expect(page.getByRole("spinbutton", { name: /^Lookback/ })).toHaveValue("120");

  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Delete saved strategy" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Deleted Short momentum." })).toBeVisible();
  expect(await (await page.request.get(`/api/v1/workspaces/${ws}/strategies`)).json()).toEqual([]);
});
