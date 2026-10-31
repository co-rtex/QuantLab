import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// #144: an ML result stored before #125 has no pipeline_version, panel.excluded_rows or
// panel.volume_flat_rows. Those show as "not recorded", never 0 (undefined is not 0). The
// run is served by intercepting the browser's API calls; workflow.spec.ts covers a real run.

const RUN_ID = "00000000-0000-4000-8000-000000000144";

const oldRun = {
  id: RUN_ID,
  name: "Before pipeline v2",
  dataset_version_id: "00000000-0000-4000-8000-000000000001",
  is_synthetic: true,
  status: "completed",
  created_at: "2026-09-01T12:00:00Z",
  config: {},
  input_hash: "f".repeat(64),
  job: null,
  result: {
    problem: "P(next 21-session total return beats the cross-sectional median)",
    panel: { rows: 1200, dates: 40, symbols: 30, hash: "a".repeat(64), positive_rate: 0.5 },
    splits: [{ fold: 1, train_dates: 20, test_start: "2024-01-02", test_end: "2024-06-28" }],
    summary: [{ model: "majority", rows: 600, auc: null, accuracy: 0.5, brier: 0.25, ic_mean: null, ic_t_stat: null, top_minus_bottom_quintile: null }],
    feature_importance: {},
    limitations: [],
  },
};

async function register(page: Page) {
  await page.goto("/register");
  await page.getByLabel("Name").fill("ML panel");
  await page.getByLabel("Email").fill(`ml-panel-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("Panel-inputs-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
}

test("an ML result from before pipeline v2 shows its panel inputs as not recorded", async ({ page }) => {
  await register(page);
  const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/api/v1/workspaces/*/ml-runs?*", (route) => route.fulfill(json([{ ...oldRun, result: undefined }])));
  await page.route(`**/api/v1/ml-runs/${RUN_ID}`, (route) => route.fulfill(json(oldRun)));
  await page.goto("/ml");

  const inputs = page.getByRole("region", { name: "Panel construction" });
  await expect(inputs).toBeVisible();
  for (const term of ["Pipeline version", "Rows excluded: no trading", "Dates skipped: too few names", "Rows with flat volume"]) {
    const item = inputs.locator("dl > div").filter({ has: page.getByRole("term").filter({ hasText: term }) });
    await expect(item.getByRole("definition").first()).toHaveText("not recorded");
  }
  await expect(inputs.getByRole("definition").filter({ hasText: /^0$/ })).toHaveCount(0);

  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
});
