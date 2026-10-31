import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// #144: how an ML result's panel was built. A result stored before #125 has no
// pipeline_version, panel.excluded_rows or panel.volume_flat_rows: those show as "not
// recorded", never 0 (undefined is not 0). The keys of excluded_rows depend on the pipeline
// version (v2 #125, v3 #142), and each is shown with its own explanation. Runs are served by
// intercepting the browser's API calls; workflow.spec.ts covers a real run.

const RUN_ID = "00000000-0000-4000-8000-000000000144";

const run = (name: string, extra: { pipeline_version?: number; panel?: Record<string, unknown> }) => ({
  id: RUN_ID,
  name,
  dataset_version_id: "00000000-0000-4000-8000-000000000001",
  is_synthetic: true,
  status: "completed",
  created_at: "2026-09-01T12:00:00Z",
  config: {},
  input_hash: "f".repeat(64),
  job: null,
  result: {
    problem: "P(next 21-session total return beats the cross-sectional median)",
    pipeline_version: extra.pipeline_version,
    panel: { rows: 1200, dates: 40, symbols: 30, hash: "a".repeat(64), positive_rate: 0.5, ...extra.panel },
    splits: [{ fold: 1, train_dates: 20, test_start: "2024-01-02", test_end: "2024-06-28" }],
    summary: [{ model: "majority", rows: 600, auc: null, accuracy: 0.5, brier: 0.25, ic_mean: null, ic_t_stat: null, top_minus_bottom_quintile: null }],
    feature_importance: {},
    limitations: [],
  },
});

async function register(page: Page) {
  await page.goto("/register");
  await page.getByLabel("Name").fill("ML panel");
  await page.getByLabel("Email").fill(`ml-panel-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("Panel-inputs-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
}

/** Shows `body` as the only ML run and returns the panel-construction region. */
async function show(page: Page, body: ReturnType<typeof run>) {
  const json = (b: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(b) });
  await page.route("**/api/v1/workspaces/*/ml-runs?*", (route) => route.fulfill(json([{ ...body, result: undefined }])));
  await page.route(`**/api/v1/ml-runs/${RUN_ID}`, (route) => route.fulfill(json(body)));
  await page.goto("/ml");
  const inputs = page.getByRole("region", { name: "Panel construction" });
  await expect(inputs).toBeVisible();
  return inputs;
}

/** Each item as [term, value, explanation or null], in page order. */
async function items(inputs: ReturnType<Page["getByRole"]>) {
  return inputs.locator("dl > div").evaluateAll((divs) =>
    divs.map((d) => {
      const dds = [...d.querySelectorAll("dd")].map((x) => x.textContent ?? "");
      return [d.querySelector("dt")?.textContent ?? "", dds[0] ?? "", dds[1] ?? null];
    }),
  );
}

async function audit(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
}

test("an ML result from before pipeline v2 shows its panel inputs as not recorded", async ({ page }) => {
  await register(page);
  const inputs = await show(page, run("Before pipeline v2", {}));
  const got = await items(inputs);
  expect(got.map(([term, value]) => [term, value])).toEqual([
    ["Pipeline version", "not recorded"],
    ["Rows excluded", "not recorded"],
    ["Rows with flat volume", "not recorded"],
  ]);
  await audit(page);
});

test("a v2 result shows its own exclusion keys, with a real 0 as 0", async ({ page }) => {
  await register(page);
  const inputs = await show(page, run("Pipeline v2", { pipeline_version: 2, panel: { volume_flat_rows: 5, excluded_rows: { untraded_21: 0, dates_below_10_after_untraded: 3 } } }));
  expect(await items(inputs)).toEqual([
    ["Pipeline version", "v2", expect.any(String)],
    ["Rows excluded: no trading", "0", "Zero volume over its last 21 bars (its own trading days); counted only on dates that make the panel."],
    ["Dates skipped: too few names", "3", "Fewer than 10 names left on the date once untraded rows were removed."],
    ["Rows with flat volume", "5", expect.any(String)],
  ]);
});

test("a v3 result shows its own exclusion keys, and an unknown key under its raw name", async ({ page }) => {
  await register(page);
  // Keys out of table order, plus one this page does not know.
  const excluded_rows = { dates_below_10_with_zero_volume: 0, later_reason: 7, zero_volume_bars: 1234 };
  const inputs = await show(page, run("Pipeline v3", { pipeline_version: 3, panel: { volume_flat_rows: 0, excluded_rows } }));
  expect(await items(inputs)).toEqual([
    ["Pipeline version", "v3", expect.any(String)],
    ["Zero-volume bars", "1,234", "Dropped as no trade; features, labels and windows skip them."],
    ["Dates skipped: too few names", "0", "Fewer than 10 names on that date once zero-volume bars were dropped (a longer warm-up from dropped bars is not counted)."],
    ["later_reason", "7", null],
    ["Rows with flat volume", "0", expect.any(String)],
  ]);
  await expect(inputs.getByText("not recorded")).toHaveCount(0);
  await audit(page);
});
