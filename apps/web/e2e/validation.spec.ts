import { expect, test, type Page } from "@playwright/test";

// #16: the validation panel must show the suite's results however its first poll after
// "Run validation suite" goes: delayed until the job has finished, or failing once.

async function finishedExperiment(page: Page, tag: string) {
  await page.goto("/register");
  await page.getByLabel("Name").fill("Validation");
  await page.getByLabel("Email").fill(`validation-${tag}-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("Validation-race-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
  const ws = (await (await page.request.get("/api/v1/workspaces")).json())[0].id;
  const job = await (
    await page.request.post(`/api/v1/workspaces/${ws}/datasets/synthetic`, {
      data: { slug: `validation-${tag}-${Date.now()}`, name: "Validation data", n_assets: 20, start: "2020-01-02", end: "2022-12-30" },
    })
  ).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 60_000 }).toBe("completed");
  const dv = (await (await page.request.get(`/api/v1/workspaces/${ws}/dataset-versions`)).json())[0].id;
  const sub = await (
    await page.request.post(`/api/v1/workspaces/${ws}/experiments`, {
      data: { name: `Validation ${tag}`, dataset_version_id: dv, strategy_key: "equal_weight", config: { start: "2021-01-04", benchmark: "SYNIDX" } },
    })
  ).json();
  const id = sub.experiment.id as string;
  await expect.poll(async () => (await (await page.request.get(`/api/v1/experiments/${id}`)).json()).status, { timeout: 60_000 }).toBe("completed");
  return id;
}

/** The suite's run, polled through the API (not the page), until it has finished. */
async function suiteFinished(page: Page, id: string) {
  await expect.poll(async () => (await (await page.request.get(`/api/v1/experiments/${id}/validation`)).json()).status, { timeout: 90_000 }).toBe("completed");
}

const regimes = (page: Page) => page.getByText("Performance by market regime");

test("results appear when the job finishes before the panel's first poll", async ({ page }) => {
  test.setTimeout(240_000);
  const id = await finishedExperiment(page, "late");
  let held = false;
  await page.route("**/api/v1/experiments/*/validation?s=*", async (route) => {
    // Hold the first poll after the POST until the job is done, as on a busy machine.
    if (route.request().method() === "GET" && !route.request().url().endsWith("?s=0") && !held) {
      held = true;
      await suiteFinished(page, id);
    }
    await route.continue();
  });
  await page.goto(`/experiments/${id}`);
  await page.getByRole("button", { name: "Run validation suite" }).click();
  await expect(regimes(page)).toBeVisible({ timeout: 30_000 });
});

test("results appear when the first poll after the POST fails once", async ({ page }) => {
  test.setTimeout(240_000);
  const id = await finishedExperiment(page, "flaky");
  let failed = false;
  await page.route("**/api/v1/experiments/*/validation?s=*", async (route) => {
    if (route.request().method() === "GET" && !route.request().url().endsWith("?s=0") && !failed) {
      failed = true;
      await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: { code: "unavailable", message: "Service unavailable" } }) });
      return;
    }
    await route.continue();
  });
  await page.goto(`/experiments/${id}`);
  await page.getByRole("button", { name: "Run validation suite" }).click();
  await expect(regimes(page)).toBeVisible({ timeout: 90_000 });
});
