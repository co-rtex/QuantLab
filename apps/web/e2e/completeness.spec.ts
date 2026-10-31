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

test("a result whose dataset cannot be checked is never shown as real", async ({ page }) => {
  test.setTimeout(180_000);
  const { dv, id, name } = await workspaceWithExperiment(page, "unknown");
  const row = page.getByRole("row").filter({ hasText: name });
  const list = "**/api/v1/workspaces/*/experiments?*";
  const versions = "**/api/v1/workspaces/*/dataset-versions";
  const failure = { status: 500, contentType: "application/json", body: JSON.stringify({ error: { code: "internal_error", message: "Internal server error" } }) };
  /** Serve the experiments list with each row's is_synthetic replaced (undefined drops it). */
  const rowsWith = async (flag: boolean | null | undefined) => {
    await page.unroute(list);
    await page.route(list, async (route) => {
      const body = await (await route.fetch()).json();
      body.items = body.items.map((e: Record<string, unknown>) => {
        const rest = { ...e };
        delete rest.is_synthetic;
        return flag === undefined ? rest : { ...rest, is_synthetic: flag };
      });
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    });
  };

  // The row says so itself (#106), with no dataset-version lookup at all.
  const lookups: string[] = [];
  page.on("request", (r) => {
    if (/\/workspaces\/[^/]+\/dataset-versions/.test(r.url())) lookups.push(r.url());
  });
  await page.goto("/experiments");
  await expect(row.getByText("Synthetic data")).toBeVisible();
  expect(lookups).toEqual([]);
  await rowsWith(null);
  await page.goto("/experiments");
  await expect(row.getByText("Synthetic status unknown")).toBeVisible();
  await audit(page, "experiments list, synthetic status unknown");
  await rowsWith(false);
  await page.goto("/experiments");
  await expect(row).toBeVisible();
  await expect(row.getByText(/Synthetic/)).toHaveCount(0);

  // An API without the field: the dataset-version list decides, failing toward a label.
  await rowsWith(undefined);

  // Loading: the version list has not arrived yet.
  let release: () => void = () => {};
  const held = new Promise<void>((r) => (release = r));
  await page.route(versions, async (route) => {
    await held;
    await route.continue();
  });
  await page.goto("/experiments");
  await expect(row.getByText("Synthetic status unknown")).toBeVisible();
  release();
  await expect(row.getByText("Synthetic data")).toBeVisible();
  await page.unroute(versions);

  // Failed: the version list could not be loaded.
  await page.route(versions, (route) => route.fulfill(failure));
  await page.goto("/experiments");
  await expect(row.getByText("Synthetic status unknown")).toBeVisible();
  await page.unroute(versions);

  // Missing: the list does not contain the experiment's version.
  await page.route(versions, (route) => route.fulfill({ contentType: "application/json", body: "[]" }));
  await page.goto("/experiments");
  await expect(row.getByText("Synthetic status unknown")).toBeVisible();
  await page.unroute(versions);

  // Positively real in the version list: no label.
  await page.route(versions, async (route) => {
    const all = await (await route.fetch()).json();
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(all.map((v: { id: string }) => (v.id === dv ? { ...v, is_synthetic: false } : v))) });
  });
  await page.goto("/experiments");
  await expect(row).toBeVisible();
  await expect(row.getByText(/Synthetic/)).toHaveCount(0);
  await page.unroute(versions);
  await page.unroute(list);

  // The experiment page, when its dataset version cannot be loaded.
  await page.route(`**/api/v1/dataset-versions/${dv}`, (route) => route.fulfill(failure));
  await page.goto(`/experiments/${id}`);
  await expect(page.getByRole("region", { name: "Evidence" }).getByText("Synthetic status unknown")).toBeVisible();
  await expect(page.locator("section").filter({ hasText: "Ask about this experiment" }).getByText("Synthetic status unknown")).toBeVisible();
});
