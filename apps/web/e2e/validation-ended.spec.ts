import { expect, test, type Page } from "@playwright/test";

// #260: a validation run that failed, was cancelled or timed out says why and can be run
// again. Its state is held by route interception (the backend's transitions are covered by
// the integration tests); the experiment itself is a real, completed backtest.

async function completedExperiment(page: Page): Promise<string> {
  await page.goto("/register");
  await page.getByLabel("Name").fill("Validation");
  await page.getByLabel("Email").fill(`validation-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("Validation-ended-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
  const ws = (await (await page.request.get("/api/v1/workspaces")).json())[0].id as string;
  const job = await (
    await page.request.post(`/api/v1/workspaces/${ws}/datasets/synthetic`, {
      data: { slug: `validation-${Date.now()}`, name: "Validation data", n_assets: 5, start: "2021-01-04", end: "2021-12-31" },
    })
  ).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 60_000 }).toBe("completed");
  const dv = (await (await page.request.get(`/api/v1/workspaces/${ws}/dataset-versions`)).json())[0].id as string;
  const sub = await (
    await page.request.post(`/api/v1/workspaces/${ws}/experiments`, {
      data: { name: "Validated", dataset_version_id: dv, strategy_key: "equal_weight", config: { start: "2021-03-01" } },
    })
  ).json();
  const id = sub.experiment.id as string;
  await expect.poll(async () => (await (await page.request.get(`/api/v1/experiments/${id}`)).json()).status, { timeout: 60_000 }).toBe("completed");
  return id;
}

function ended(status: string, message: string, error: { type: string; message: string } | null) {
  const now = new Date().toISOString();
  return {
    id: "00000000-0000-4000-8000-000000000260",
    kind: "validation",
    status,
    params: { suite_version: 2, seed: 0 },
    created_at: now,
    result: null,
    engine_version: null,
    comparable_engine: null,
    job: {
      id: "00000000-0000-4000-8000-000000000261",
      kind: "research.validation",
      status,
      progress: 0.4,
      message,
      attempts: 1,
      max_attempts: 3,
      error,
      cancel_requested: false,
      created_at: now,
      started_at: now,
      finished_at: now,
      payload: {},
    },
  };
}

test("a failed or timed-out validation says why and offers a rerun", async ({ page }) => {
  test.setTimeout(150_000);
  const id = await completedExperiment(page);
  const cases = [
    ended("failed", "failed: PermanentJobError", { type: "PermanentJobError", message: "validation could not run: too little history for SYN004" }),
    // The reaper's timeout: a message and no error.
    ended("timed_out", "exceeded timeout (worker unresponsive)", null),
    ended("cancelled", "cancelled by user", null),
  ];
  for (const run of cases) {
    await page.unroute(`**/api/v1/experiments/${id}/validation*`);
    await page.route(`**/api/v1/experiments/${id}/validation*`, async (route) => {
      if (route.request().method() !== "GET") return route.continue();
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(run) });
    });
    await page.goto(`/experiments/${id}`);
    await expect(page.getByText("Out-of-sample validation")).toBeVisible();
    // The run's own words (JobProgress): its message, and its error when it has one.
    await expect(page.getByText(run.job.message, { exact: true })).toBeVisible();
    if (run.job.error) await expect(page.getByText(run.job.error.message, { exact: false })).toBeVisible();
    await expect(page.getByRole("button", { name: "Run validation suite" })).toBeVisible();
  }
});
