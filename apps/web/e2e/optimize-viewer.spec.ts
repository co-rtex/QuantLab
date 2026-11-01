import { expect, test, type Browser, type Page } from "@playwright/test";

// #224: an optimization is a worker job that writes a run, so viewers read results but are
// not offered a submit the API would refuse (403); the page says why.

async function registerAs(page: Page, name: string, email: string) {
  await page.goto("/register");
  await page.getByLabel("Name").fill(name);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("Optimize-jobs-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
}

test("a viewer reads an optimization but cannot run one", async ({ browser }: { browser: Browser }) => {
  test.setTimeout(120_000);
  const stamp = Date.now();
  const owner = await (await browser.newContext()).newPage();
  await registerAs(owner, "Owner", `optimize-o-${stamp}@example.com`);
  const ws = (await (await owner.request.get("/api/v1/workspaces")).json())[0].id as string;
  const job = await (
    await owner.request.post(`/api/v1/workspaces/${ws}/datasets/synthetic`, {
      data: { slug: `optimize-${stamp}`, name: "Optimize data", n_assets: 8, start: "2020-01-02", end: "2021-12-31" },
    })
  ).json();
  await expect.poll(async () => (await (await owner.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 60_000 }).toBe("completed");
  const dv = (await (await owner.request.get(`/api/v1/workspaces/${ws}/dataset-versions`)).json())[0].id as string;
  // Every asset, as the page's default does: synthetic names can list late or delist, and
  // a run needs two with enough history.
  const body = { symbols: Array.from({ length: 8 }, (_, i) => `SYN00${i + 1}`), lookback: 120 };
  const submitted = await owner.request.post(`/api/v1/dataset-versions/${dv}/optimizations`, { data: body });
  expect(submitted.status()).toBe(202);
  const run = await submitted.json();
  const settled = async () => (await owner.request.get(`/api/v1/optimizations/${run.id}`)).json();
  await expect.poll(async () => (await settled()).status, { timeout: 60_000 }).not.toMatch(/queued|running/);
  const done = await settled();
  expect(done.status, JSON.stringify(done.job?.error)).toBe("completed");

  const email = `optimize-v-${stamp}@example.com`;
  const created = await (await owner.request.post(`/api/v1/workspaces/${ws}/invitations`, { data: { email, role: "viewer" } })).json();
  const viewer = await (await browser.newContext()).newPage();
  await registerAs(viewer, "Viewer", email);
  const token = new URLSearchParams(String(created.accept_path).split("#")[1]).get("token");
  expect((await viewer.request.post("/api/v1/auth/invitations/accept", { data: { token } })).ok()).toBe(true);
  await viewer.evaluate((w) => localStorage.setItem("quantlab.workspace", w), ws);

  await viewer.goto(`/optimize?run=${run.id}`);
  await expect(viewer.getByRole("cell", { name: "Expected volatility" })).toBeVisible();
  await expect(viewer.getByRole("button", { name: "Compare allocations" })).toBeDisabled();
  await expect(viewer.getByText("Running one needs the editor role")).toBeVisible();
  expect((await viewer.request.post(`/api/v1/dataset-versions/${dv}/optimizations`, { data: body })).status()).toBe(403);

  await owner.goto("/optimize");
  await expect(owner.getByPlaceholder(/First 12: SYN001/)).toBeVisible();
  await expect(owner.getByRole("button", { name: "Compare allocations" })).toBeEnabled();
  await expect(owner.getByText("Running one needs the editor role")).toHaveCount(0);
});
