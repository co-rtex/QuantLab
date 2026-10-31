import { expect, test } from "@playwright/test";

// The experiment builder's default first session must be a date the form accepts: a year
// after the data starts was past the last session on shorter data, and the browser then
// refused to submit the form.

test("a backtest can be started on a dataset shorter than a year", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/register");
  await page.getByLabel("Name").fill("Builder");
  await page.getByLabel("Email").fill(`builder-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("Short-data-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
  const ws = (await (await page.request.get("/api/v1/workspaces")).json())[0].id as string;
  const job = await (
    await page.request.post(`/api/v1/workspaces/${ws}/datasets/synthetic`, {
      data: { slug: `short-${Date.now()}`, name: "Short data", n_assets: 5, start: "2021-03-01", end: "2021-11-30" },
    })
  ).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 60_000 }).toBe("completed");
  const version = (await (await page.request.get(`/api/v1/workspaces/${ws}/dataset-versions`)).json())[0];

  await page.goto("/experiments/new");
  const start = page.locator('input[name="start"]');
  await expect(start).toHaveValue(version.start_date);
  expect(await start.evaluate((el: HTMLInputElement) => el.checkValidity())).toBe(true);
  await page.getByLabel("Experiment name").fill("Short-data run");
  await page.getByRole("button", { name: "Run backtest" }).click();
  await expect(page).toHaveURL(/\/experiments\/[0-9a-f-]{36}$/);
});
