import { expect, test } from "@playwright/test";

// #163: a backtest that would not fit the worker's memory is refused with 422
// backtest_too_large. Its details are an object (the estimate, the limit and the job's
// size), not the list of field errors other 422s carry, and the experiment form called
// .map on it while rendering. The refusal is served by intercepting the submission, so the
// test does not depend on the estimate's coefficients.

const REFUSAL = {
  error: {
    code: "backtest_too_large",
    message:
      "This backtest needs an estimated 2084 MB, over the 1228 MB limit for one backtest. " +
      "Use fewer symbols, a shorter period or a less frequent rebalance.",
    details: { estimated_mb: 2084.0, limit_mb: 1228, symbols: 400, sessions: 4785, rebalances: 4785 },
    request_id: null,
  },
};

test("a backtest refused for its memory shows why, and the form stays usable", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/register");
  await page.getByLabel("Name").fill("Too large");
  await page.getByLabel("Email").fill(`too-large-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("Memory-limit-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
  const ws = (await (await page.request.get("/api/v1/workspaces")).json())[0].id as string;
  const job = await (
    await page.request.post(`/api/v1/workspaces/${ws}/datasets/synthetic`, {
      data: { slug: `big-${Date.now()}`, name: "Synthetic", n_assets: 5, start: "2021-03-01", end: "2021-11-30" },
    })
  ).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 60_000 }).toBe("completed");

  await page.route(`**/api/v1/workspaces/${ws}/experiments`, (route) =>
    route.request().method() === "POST"
      ? route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify(REFUSAL) })
      : route.continue(),
  );
  await page.goto("/experiments/new");
  await page.getByLabel("Experiment name").fill("Too large");
  await page.getByRole("button", { name: "Run backtest" }).click();
  await expect(page.getByText("needs an estimated 2084 MB, over the 1228 MB limit")).toBeVisible();
  await expect(page.getByRole("button", { name: "Run backtest" })).toBeEnabled(); // still rendered
  await expect(page).toHaveURL(/\/experiments\/new$/);
});
