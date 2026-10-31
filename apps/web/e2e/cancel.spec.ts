import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Browser, type Page } from "@playwright/test";

// #23: a running job can be cancelled from the page. Job states are held by route
// interception, so the button is there however fast the worker is; the API's own cancel
// semantics are covered by the integration tests.

async function audit(page: Page, name: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const summary = results.violations.map((v) => `${v.id} (${v.impact}): ${v.help} [${v.nodes.map((n) => n.target.join(" ")).join("; ")}]`);
  expect(summary, `${name}\n${summary.join("\n")}`).toEqual([]);
}

async function registerAs(page: Page, name: string, email: string) {
  await page.goto("/register");
  await page.getByLabel("Name").fill(name);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("Cancel-jobs-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
}

test("a running dataset job is cancelled once, and says it is cancelling", async ({ page }) => {
  await registerAs(page, "Cancel", `cancel-${Date.now()}@example.com`);

  // running -> (cancel POST) -> running with cancel_requested -> (stop) -> cancelled
  const cancels: string[] = [];
  let stopped = false;
  await page.route(/\/api\/v1\/jobs\/[0-9a-f-]+(\/cancel)?$/, async (route) => {
    const req = route.request();
    if (req.method() === "POST") {
      cancels.push(req.url());
      const job = await (await page.request.get(req.url().replace(/\/cancel$/, ""))).json();
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...job, status: "running", cancel_requested: true }) });
    }
    const job = await (await route.fetch()).json();
    const state = stopped
      ? { status: "cancelled", message: "cancelled by user", cancel_requested: true }
      : { status: "running", message: "generating bars", cancel_requested: cancels.length > 0 };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...job, progress: 0.3, ...state }) });
  });

  await page.goto("/data");
  await page.getByLabel("Assets").fill("5");
  await page.getByLabel("Identifier").fill(`cancel-${Date.now()}`);
  await page.getByRole("button", { name: "Generate dataset version" }).click();
  const button = page.getByRole("button", { name: /Cancel run|Cancelling/ });
  await expect(button).toHaveText("Cancel run");
  await expect(page.getByRole("progressbar", { name: "Job progress" })).toHaveAttribute("aria-valuetext", "30%, generating bars");
  await audit(page, "data page, running job");

  await button.click();
  await expect(button).toHaveText("Cancelling…");
  await expect(button).toHaveAttribute("aria-busy", "true");
  await expect(page.getByText("Cancellation requested.")).toBeVisible();
  await button.click({ force: true }); // aria-disabled: a click lands, but sends nothing
  await page.waitForTimeout(1500);
  expect(cancels).toHaveLength(1);
  expect(cancels[0]).toMatch(/\/api\/v1\/jobs\/[0-9a-f-]{36}\/cancel$/);

  stopped = true;
  await expect(page.getByText("Cancelled", { exact: true })).toBeVisible();
  await expect(button).toHaveCount(0);
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

test("a viewer is not offered a cancel the API would refuse", async ({ browser }: { browser: Browser }) => {
  test.setTimeout(120_000);
  const stamp = Date.now();
  const owner = await (await browser.newContext()).newPage();
  await registerAs(owner, "Owner", `cancel-o-${stamp}@example.com`);
  const ws = (await (await owner.request.get("/api/v1/workspaces")).json())[0].id as string;
  const job = await (
    await owner.request.post(`/api/v1/workspaces/${ws}/datasets/synthetic`, {
      data: { slug: `cancel-${stamp}`, name: "Cancel data", n_assets: 5, start: "2021-01-04", end: "2021-12-31" },
    })
  ).json();
  await expect.poll(async () => (await (await owner.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 60_000 }).toBe("completed");
  const dv = (await (await owner.request.get(`/api/v1/workspaces/${ws}/dataset-versions`)).json())[0].id as string;
  const sub = await (
    await owner.request.post(`/api/v1/workspaces/${ws}/experiments`, {
      data: { name: "Long run", dataset_version_id: dv, strategy_key: "equal_weight", config: { start: "2021-03-01", benchmark: "SYNIDX" } },
    })
  ).json();
  const id = sub.experiment.id as string;
  const email = `cancel-v-${stamp}@example.com`;
  const created = await (await owner.request.post(`/api/v1/workspaces/${ws}/invitations`, { data: { email, role: "viewer" } })).json();
  const viewer = await (await browser.newContext()).newPage();
  await registerAs(viewer, "Viewer", email);
  const token = new URLSearchParams(String(created.accept_path).split("#")[1]).get("token");
  expect((await viewer.request.post("/api/v1/auth/invitations/accept", { data: { token } })).ok()).toBe(true);

  // Both see the backtest as still running.
  for (const page of [owner, viewer]) {
    await page.route(`**/api/v1/experiments/${id}`, async (route) => {
      const e = await (await route.fetch()).json();
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...e, status: "running" }) });
    });
    await page.route(`**/api/v1/experiments/${id}/job`, async (route) => {
      const j = await (await route.fetch()).json();
      await route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...j, status: "running", progress: 0.4, cancel_requested: false }) });
    });
  }
  await viewer.evaluate((w) => localStorage.setItem("quantlab.workspace", w), ws);
  await viewer.goto(`/experiments/${id}`);
  await expect(viewer.getByRole("progressbar", { name: "Job progress" })).toBeVisible();
  await expect(viewer.getByRole("button", { name: "Cancel run" })).toHaveCount(0);

  await owner.goto(`/experiments/${id}`);
  await expect(owner.getByRole("button", { name: "Cancel run" })).toBeVisible();
  // Both pages keep polling through these routes; a callback still in flight when the
  // test ends fails the run outside any test.
  for (const page of [owner, viewer]) await page.unrouteAll({ behavior: "ignoreErrors" });
});
