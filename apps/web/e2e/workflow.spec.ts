import { expect, test } from "@playwright/test";

const unique = Date.now().toString(36);
const email = `e2e-${unique}@example.com`;
const password = "E2e-research-2026";

test("research workflow: data -> backtest -> results -> reproduction", async ({ page }) => {
  // Five chained jobs (dataset, backtest, validation, ML, reproduction) share this budget.
  // Each takes 3-5 s on an idle machine but 4-6x that under load, which overran the default
  // 90 s even though every job completed (#16). The per-step waits still bound each job.
  test.setTimeout(240_000);
  await page.goto("/");
  await expect(page).toHaveURL(/\/login/);

  await page.getByRole("link", { name: "Create an account" }).click();
  await page.getByLabel("Name").fill("E2E Researcher");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();

  await page.getByRole("link", { name: "Add market data" }).click();
  await page.getByLabel("Assets").fill("20");
  await page.getByLabel("Identifier").fill(`e2e-${unique}`);
  await page.getByRole("button", { name: "Generate dataset version" }).click();
  await expect(page.getByRole("cell", { name: "Passed" }).or(page.getByText("Passed")).first()).toBeVisible();
  await expect(page.getByText("Synthetic data").first()).toBeVisible();

  await page.getByRole("link", { name: "Experiments" }).click();
  await page.getByRole("link", { name: "New experiment" }).first().click();
  await page.getByText("Equal Weight", { exact: true }).click();
  await page.getByLabel("Experiment name").fill(`EW ${unique}`);
  await page.getByLabel("Hypothesis and notes").fill("Baseline for comparison.");
  await page.getByRole("button", { name: "Run backtest" }).click();

  await expect(page).toHaveURL(/\/experiments\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading", { name: "Largest drawdowns" })).toBeVisible({ timeout: 60_000 });
  const evidence = page.getByRole("region", { name: "Evidence" });
  await expect(evidence.getByText("Synthetic data")).toBeVisible();
  await expect(evidence.getByText("Next open")).toBeVisible();
  await expect(page.getByRole("table").filter({ hasText: "Sharpe ratio" })).toBeVisible();
  await expect(page.getByText(/fills$/)).toBeVisible();

  // Trades are always filled after the decision date.
  const firstRow = page.getByRole("table").filter({ hasText: "Fill price" }).getByRole("row").nth(1);
  const [decided, filled] = await Promise.all([firstRow.getByRole("cell").nth(0).innerText(), firstRow.getByRole("cell").nth(1).innerText()]);
  expect(filled > decided).toBeTruthy();

  // Integrity report is available immediately; validation fills in the unassessed checks.
  await expect(page.getByText("Research integrity report")).toBeVisible();
  await page.getByRole("button", { name: "Run validation suite" }).click();
  await expect(page.getByText("Performance by market regime")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText(/Signals on \d+ sampled dates were identical/)).toBeVisible();

  await page.getByRole("button", { name: "What was the worst drawdown?" }).click();
  await expect(page.getByText("Assembled from computed facts")).toBeVisible();
  await page.getByPlaceholder("What drove the largest drawdown?").fill("Should I invest in this strategy?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  await expect(page.getByText("I can't tell you whether to buy")).toBeVisible();

  const report = await page.request.get(page.url().replace("/experiments/", "/api/v1/experiments/") + "/report");
  expect(report.status()).toBe(200);
  expect(await report.text()).toContain("Research integrity");

  // ML research on the same dataset version (small config keeps the run quick).
  const ws = await page.request.get("/api/v1/workspaces");
  const wsId = (await ws.json())[0].id;
  const versions = await (await page.request.get(`/api/v1/workspaces/${wsId}/dataset-versions`)).json();
  const ml = await page.request.post(`/api/v1/workspaces/${wsId}/ml-runs`, {
    data: { name: "E2E ML", dataset_version_id: versions[0].id, config: { folds: 2, min_train_dates: 6, sample_every: 10, models: ["majority", "logistic"] } },
  });
  expect(ml.status()).toBe(202);
  const experimentUrl = page.url();
  await page.goto("/ml");
  await expect(page.getByText("Out-of-sample AUC")).toBeVisible({ timeout: 60_000 });
  // A new result records how its panel was built (#144). The exclusion keys depend on the
  // pipeline version, so this checks only that each count after the version is a number;
  // ml-panel.spec.ts covers each version's labels.
  const inputs = page.getByRole("region", { name: "Panel construction" });
  await expect(inputs.getByRole("definition").first()).toHaveText(/^v\d+$/);
  await expect(inputs.getByText("not recorded")).toHaveCount(0);
  const items = inputs.locator("dl > div");
  expect(await items.count()).toBeGreaterThanOrEqual(3);
  for (const item of (await items.all()).slice(1)) await expect(item.getByRole("definition").first()).toHaveText(/^[\d,]+$/);
  await page.goto(experimentUrl);

  await page.getByRole("button", { name: "Reproduce" }).click();
  await expect(page.getByText("(reproduction)").first()).toBeVisible();
  await expect(page.getByText("outputs match this run exactly")).toBeVisible({ timeout: 60_000 });
});
