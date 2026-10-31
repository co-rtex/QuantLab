import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// #23: results on synthetic data are labelled in every list, and large catalogs are read
// in full.

async function audit(page: Page, name: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const summary = results.violations.map((v) => `${v.id} (${v.impact}): ${v.help} [${v.nodes.map((n) => n.target.join(" ")).join("; ")}]`);
  expect(summary, `${name}\n${summary.join("\n")}`).toEqual([]);
}

async function workspaceWithExperiment(page: Page, tag: string) {
  await page.goto("/register");
  await page.getByLabel("Name").fill("Completeness");
  await page.getByLabel("Email").fill(`complete-${tag}-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("Completeness-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
  const ws = (await (await page.request.get("/api/v1/workspaces")).json())[0].id as string;
  const job = await (
    await page.request.post(`/api/v1/workspaces/${ws}/datasets/synthetic`, {
      data: { slug: `complete-${tag}-${Date.now()}`, name: "Completeness data", n_assets: 8, start: "2020-01-02", end: "2021-12-31" },
    })
  ).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 60_000 }).toBe("completed");
  const dv = (await (await page.request.get(`/api/v1/workspaces/${ws}/dataset-versions`)).json())[0].id as string;
  const name = `Labelled ${tag}`;
  const sub = await (
    await page.request.post(`/api/v1/workspaces/${ws}/experiments`, {
      data: { name, dataset_version_id: dv, strategy_key: "equal_weight", config: { start: "2021-01-04", benchmark: "SYNIDX" } },
    })
  ).json();
  const id = sub.experiment.id as string;
  await expect.poll(async () => (await (await page.request.get(`/api/v1/experiments/${id}`)).json()).status, { timeout: 60_000 }).toBe("completed");
  return { ws, dv, id, name };
}

test("results on synthetic data are labelled in every list", async ({ page }) => {
  test.setTimeout(180_000);
  const { id, name } = await workspaceWithExperiment(page, "labels");
  const row = (n: string) => page.getByRole("row").filter({ hasText: n });

  await page.goto("/");
  await expect(row(name).getByText("Synthetic data")).toBeVisible();
  await page.goto("/experiments");
  await expect(row(name).getByText("Synthetic data")).toBeVisible();
  await audit(page, "experiments list with labels");

  await page.goto("/compare");
  const option = page.locator("label").filter({ hasText: name });
  await expect(option.getByText("Synthetic data")).toBeVisible();
  await option.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Compare selected" }).click();
  await expect(row(name).getByText("Synthetic data")).toBeVisible();
  await expect(page.getByRole("region").or(page.locator("section")).filter({ hasText: "Growth of $1" }).getByText("Synthetic data")).toBeVisible();
  await audit(page, "compare with labels");

  await page.goto(`/experiments/${id}`);
  await expect(page.locator("section").filter({ hasText: "Ask about this experiment" }).getByText("Synthetic data")).toBeVisible();
});

test("every instrument of a large catalog is read, so the benchmark is offered", async ({ page }) => {
  test.setTimeout(180_000);
  await workspaceWithExperiment(page, "catalog");
  // A 2,001-instrument catalog whose only index fund sorts after the first 2,000 rows.
  const stock = (i: number) => ({
    symbol: `S${String(i).padStart(4, "0")}`,
    name: `Stock ${i}`,
    sector: "Tech",
    asset_class: "equity",
    first_date: "2020-01-02",
    last_date: "2021-12-31",
    delisted: false,
    rows: 500,
    instrument_id: null,
  });
  await page.route("**/api/v1/dataset-versions/*/instruments?*", (route) => {
    const offset = Number(new URL(route.request().url()).searchParams.get("offset") ?? 0);
    const items = offset === 0 ? Array.from({ length: 2000 }, (_, i) => stock(i)) : [{ ...stock(9999), symbol: "ZZIDX", asset_class: "index_fund" }];
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ items, total: 2001, limit: 2000, offset }) });
  });
  await page.goto("/experiments/new");
  const benchmark = page.getByLabel("Benchmark");
  await expect(benchmark.locator("option", { hasText: "ZZIDX" })).toHaveCount(1);
  await expect(page.getByPlaceholder("All 2000 tradable symbols")).toBeVisible();
});
