import { expect, test } from "@playwright/test";

test("portfolio ledger: create, trade, value and reject an overdraft", async ({ page }) => {
  const email = `pf-${Date.now()}@example.com`;
  await page.goto("/register");
  await page.getByLabel("Name").fill("PF");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("Portfolio-test-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
  const ws = (await (await page.request.get("/api/v1/workspaces")).json())[0].id;
  const job = await (await page.request.post(`/api/v1/workspaces/${ws}/datasets/synthetic`, {
    data: { slug: `pf-${Date.now()}`, name: "PF data", n_assets: 10, start: "2021-01-04", end: "2022-12-30" },
  })).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 30_000 }).toBe("completed");

  await page.goto("/portfolios");
  await page.getByLabel("First session").fill("2021-01-04");
  await page.getByRole("button", { name: "Create portfolio" }).click();
  await expect(page).toHaveURL(/\/portfolios\/[0-9a-f-]{36}$/);
  await expect(page.getByText("Time-weighted return")).toBeVisible();

  await page.getByLabel("Session", { exact: true }).fill("2021-01-05");
  await page.getByLabel("Symbol", { exact: true }).fill("SYN001");
  await page.getByLabel("Shares", { exact: true }).fill("100");
  await page.getByRole("button", { name: "Record transaction" }).click();
  await expect(page.getByRole("cell", { name: "SYN001" }).first()).toBeVisible();

  await page.getByLabel("Session", { exact: true }).fill("2021-01-06");
  await page.getByLabel("Symbol", { exact: true }).fill("SYN002");
  await page.getByLabel("Shares", { exact: true }).fill("100000000");
  await page.getByRole("button", { name: "Record transaction" }).click();
  await expect(page.getByText(/transaction #3/)).toBeVisible();
  await expect(page.getByText("One-day VaR, 95%")).toBeVisible();
});
