import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// #23: save a strategy with its parameters, start an experiment from it, delete it.

async function audit(page: Page, name: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const summary = results.violations.map((v) => `${v.id} (${v.impact}): ${v.help} [${v.nodes.map((n) => n.target.join(" ")).join("; ")}]`);
  expect(summary, `${name}\n${summary.join("\n")}`).toEqual([]);
}

test("a saved strategy keeps its parameters and can be loaded and deleted", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/register");
  await page.getByLabel("Name").fill("Strategist");
  await page.getByLabel("Email").fill(`strategies-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("Saved-strategies-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
  const ws = (await (await page.request.get("/api/v1/workspaces")).json())[0].id as string;
  const job = await (
    await page.request.post(`/api/v1/workspaces/${ws}/datasets/synthetic`, {
      data: { slug: `strategies-${Date.now()}`, name: "Strategy data", n_assets: 5, start: "2021-01-04", end: "2021-12-31" },
    })
  ).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 60_000 }).toBe("completed");

  await page.goto("/experiments/new");
  await page.getByText("Cross-Sectional Momentum", { exact: true }).click();
  await page.getByRole("spinbutton", { name: /^Lookback/ }).fill("120");
  const saveAs = page.getByLabel("Save this strategy and its parameters as");
  await saveAs.fill("Short momentum");
  await saveAs.press("Enter"); // saves; must not run the backtest
  await expect(page.getByRole("status").filter({ hasText: "Saved as Short momentum." })).toBeVisible();
  await expect(page).toHaveURL(/\/experiments\/new/);
  const stored = await (await page.request.get(`/api/v1/workspaces/${ws}/strategies`)).json();
  expect(stored).toHaveLength(1);
  expect(stored[0]).toMatchObject({ name: "Short momentum", strategy_key: "momentum" });
  expect(stored[0].params.lookback).toBe(120);
  expect((await (await page.request.get(`/api/v1/workspaces/${ws}/experiments`)).json()).total).toBe(0);
  await audit(page, "new experiment, saved strategies");

  await page.reload();
  await page.getByLabel("Start from a saved strategy").selectOption({ label: "Short momentum · Cross-Sectional Momentum" });
  await expect(page.getByRole("status").filter({ hasText: "Loaded Short momentum." })).toBeVisible();
  await expect(page.getByRole("spinbutton", { name: /^Lookback/ })).toHaveValue("120");

  // A double click confirms once and sends one DELETE (a second would 404 into an error).
  const deletes: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "DELETE" && r.url().includes("/strategies/")) deletes.push(r.url());
  });
  page.on("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Delete saved strategy" }).dblclick();
  await expect(page.getByRole("status").filter({ hasText: "Deleted Short momentum." })).toBeVisible();
  expect(deletes).toHaveLength(1);
  await expect(page.getByRole("alert").filter({ hasText: /not found/i })).toHaveCount(0);
  expect(await (await page.request.get(`/api/v1/workspaces/${ws}/strategies`)).json()).toEqual([]);
});

test("a saved strategy loads only what the form can show", async ({ page }) => {
  test.setTimeout(120_000);
  await page.goto("/register");
  await page.getByLabel("Name").fill("Strategist");
  await page.getByLabel("Email").fill(`strategies-load-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("Saved-strategies-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
  const ws = (await (await page.request.get("/api/v1/workspaces")).json())[0].id as string;
  const job = await (
    await page.request.post(`/api/v1/workspaces/${ws}/datasets/synthetic`, {
      // Long enough for the builder's default start, after the indicator warm-up.
      data: { slug: `strategies-load-${Date.now()}`, name: "Strategy data", n_assets: 5, start: "2020-01-02", end: "2022-12-30" },
    })
  ).json();
  await expect.poll(async () => (await (await page.request.get(`/api/v1/jobs/${job.id}`)).json()).status, { timeout: 60_000 }).toBe("completed");

  // Saved strategies as an older or other client may have stored them.
  const strategy = (id: string, name: string, key: string, params: Record<string, unknown>, at = "2026-10-01T09:00:00Z") => ({
    id, name, strategy_key: key, params, description: "", created_at: at,
  });
  await page.route(`**/api/v1/workspaces/${ws}/strategies`, (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify([
        strategy("00000000-0000-4000-8000-000000000001", "Retired", "retired_kind", {}),
        strategy("00000000-0000-4000-8000-000000000002", "Old knobs", "momentum", { lookback: 90, obsolete_knob: 3 }),
        strategy("00000000-0000-4000-8000-000000000003", "Hidden weights", "momentum", { weights: { AAA: 1 } }),
        strategy("00000000-0000-4000-8000-000000000004", "Old knobs", "momentum", { lookback: 60 }, "2026-10-02T09:00:00Z"),
      ]),
    }),
  );
  // A strategy whose schema has a parameter the form cannot render.
  await page.route("**/api/v1/strategies/catalog", async (route) => {
    const catalog = await (await route.fetch()).json();
    for (const c of catalog) if (c.key === "momentum") c.params_schema.properties.weights = { type: "object", title: "Weights" };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(catalog) });
  });
  await page.goto("/experiments/new");
  const picker = page.getByLabel("Start from a saved strategy");
  const options = await picker.locator("option").allInnerTexts();
  expect(options.filter((o) => o.startsWith("Old knobs")).every((o) => o.includes("saved"))).toBe(true);

  await picker.selectOption({ index: 1 }); // Retired
  await expect(page.getByRole("alert").filter({ hasText: "no longer available, so it was not loaded" })).toBeVisible();
  await expect(picker).toHaveValue("");

  await picker.selectOption({ index: 3 }); // Hidden weights
  await expect(page.getByRole("alert").filter({ hasText: "sets weights, which cannot be shown or edited here" })).toBeVisible();
  await expect(picker).toHaveValue("");

  await picker.selectOption({ index: 2 }); // Old knobs, with an unsupported parameter
  await expect(page.getByRole("status").filter({ hasText: "obsolete_knob is no longer supported and was not loaded" })).toBeVisible();
  await expect(page.getByRole("spinbutton", { name: /^Lookback/ })).toHaveValue("90");

  // What is sent is what is shown: the dropped parameter is not in the request.
  await page.getByLabel("Experiment name").fill("From saved");
  const sent = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith(`/workspaces/${ws}/experiments`));
  await page.getByRole("button", { name: "Run backtest" }).click();
  const body = (await sent).postDataJSON();
  expect(body.strategy_key).toBe("momentum");
  expect(body.strategy_params).toEqual({ lookback: 90 });
});
