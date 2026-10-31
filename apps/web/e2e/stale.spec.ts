import { expect, test } from "@playwright/test";

// #23 (B7): while another workspace's lists load, the old workspace's data must not be
// shown as if it were the new one's.

test("switching workspace never shows the previous workspace's datasets", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/register");
  await page.getByLabel("Name").fill("Stale");
  await page.getByLabel("Email").fill(`stale-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("Stale-data-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();

  const first = (await (await page.request.get("/api/v1/workspaces")).json())[0].id as string;
  const job = await (
    await page.request.post(`/api/v1/workspaces/${first}/datasets/synthetic`, {
      data: { slug: `stale-${Date.now()}`, name: "First workspace data", n_assets: 5, start: "2021-01-04", end: "2021-06-30" },
    })
  ).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 60_000 }).toBe("completed");
  const second = (await (await page.request.post("/api/v1/workspaces", { data: { name: "Second workspace" } })).json()).id as string;

  await page.goto("/data");
  await expect(page.getByText("First workspace data")).toBeVisible();

  // Hold the second workspace's dataset list until the assertion below has run.
  let release: () => void = () => {};
  const held = new Promise<void>((r) => (release = r));
  await page.route(`**/api/v1/workspaces/${second}/datasets*`, async (route) => {
    await held;
    await route.continue();
  });
  await page.getByLabel("Workspace").selectOption(second);
  await expect(page.getByRole("status", { name: "Loading" }).first()).toBeVisible();
  await expect(page.getByText("First workspace data")).toHaveCount(0);
  release();
  await expect(page.getByText("No datasets yet")).toBeVisible();
});
