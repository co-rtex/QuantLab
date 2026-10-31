import { writeFileSync } from "node:fs";
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// #23: import a CSV dataset from the data page, through the streaming upload endpoint.

async function audit(page: Page, name: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const summary = results.violations.map((v) => `${v.id} (${v.impact}): ${v.help} [${v.nodes.map((n) => n.target.join(" ")).join("; ")}]`);
  expect(summary, `${name}\n${summary.join("\n")}`).toEqual([]);
}

async function register(page: Page, tag: string) {
  await page.goto("/register");
  await page.getByLabel("Name").fill("Importer");
  await page.getByLabel("Email").fill(`csv-${tag}-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("Csv-import-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
}

/** Daily bars for two symbols over 40 business days, internally consistent. */
function barsCsv(): string {
  const rows = ["symbol,date,open,high,low,close,volume"];
  const day = new Date(Date.UTC(2024, 0, 2));
  let n = 0;
  while (n < 40) {
    const dow = day.getUTCDay();
    if (dow !== 0 && dow !== 6) {
      for (const [sym, base] of [["AAA", 100], ["BBB", 50]] as const) {
        const close = base * (1 + 0.002 * n);
        rows.push(`${sym},${day.toISOString().slice(0, 10)},${(close * 0.995).toFixed(2)},${(close * 1.01).toFixed(2)},${(close * 0.99).toFixed(2)},${close.toFixed(2)},${100000 + n}`);
      }
      n += 1;
    }
    day.setUTCDate(day.getUTCDate() + 1);
  }
  return rows.join("\n") + "\n";
}

async function fillForm(page: Page, slug: string) {
  const panel = page.locator("section").filter({ hasText: "Import CSV" });
  await panel.getByLabel("Name", { exact: true }).fill("Imported prices");
  await panel.getByLabel("Short name").fill(slug);
  await panel.getByLabel("Source").fill("Test fixture");
  await panel.getByLabel("Licence").fill("CC0 test data");
  return panel;
}

test("a CSV is imported into a new dataset", async ({ page }) => {
  test.setTimeout(120_000);
  await register(page, "ok");
  await page.goto("/data");
  const panel = await fillForm(page, `imported-${Date.now()}`);
  await panel.getByLabel("Daily bars (CSV)").setInputFiles({ name: "bars.csv", mimeType: "text/csv", buffer: Buffer.from(barsCsv()) });
  await audit(page, "data page, import form");
  await panel.getByRole("button", { name: "Import CSV" }).click();
  await expect(panel.getByRole("progressbar", { name: "Job progress" })).toBeVisible();
  await expect(panel.getByText("Completed", { exact: true })).toBeVisible({ timeout: 60_000 });
  const datasets = page.locator("section").filter({ hasText: "Datasets" }).first();
  await expect(datasets.getByText("Imported prices")).toBeVisible();

  // Provenance end to end: user-supplied data is not synthetic, and is not labelled so.
  const ws = (await (await page.request.get("/api/v1/workspaces")).json())[0].id as string;
  const versions = await (await page.request.get(`/api/v1/workspaces/${ws}/dataset-versions`)).json();
  expect(versions).toHaveLength(1);
  expect(versions[0].is_synthetic).toBe(false);
  await expect(datasets.getByText(/Synthetic/)).toHaveCount(0);
});

test("an oversized file is refused before it is sent", async ({ page }) => {
  await register(page, "big");
  const uploads: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().includes("/datasets/csv")) uploads.push(r.url());
  });
  await page.goto("/data");
  const panel = await fillForm(page, `too-big-${Date.now()}`);
  // Playwright passes buffers only up to 50 MB; a larger file goes by path.
  const huge = test.info().outputPath("huge.csv");
  writeFileSync(huge, Buffer.alloc(51 * 2 ** 20, 0x20));
  await panel.getByLabel("Daily bars (CSV)").setInputFiles(huge);
  await panel.getByRole("button", { name: "Import CSV" }).click();
  await expect(panel.getByRole("alert")).toContainText("huge.csv is 51.0 MB. Each file is limited to 50 MB.");
  expect(uploads).toEqual([]);
});

test("the API's refusals are shown in the panel", async ({ page }) => {
  await register(page, "refused");
  await page.goto("/data");
  const panel = await fillForm(page, `refused-${Date.now()}`);
  await panel.getByLabel("Daily bars (CSV)").setInputFiles({ name: "bars.csv", mimeType: "text/csv", buffer: Buffer.from(barsCsv()) });

  // 422 with field details.
  await page.route("**/api/v1/workspaces/*/datasets/csv", (route) =>
    route.fulfill({
      status: 422,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "validation_error", message: "Request validation failed", details: [{ loc: ["body", "license"], msg: "String should have at least 3 characters" }], request_id: "req-csv-422" } }),
    }),
  );
  await panel.getByRole("button", { name: "Import CSV" }).click();
  await expect(panel.getByRole("alert")).toContainText("Request validation failed");
  await expect(panel.getByRole("alert")).toContainText("license: String should have at least 3 characters");
  await expect(panel.getByText("Request req-csv-422")).toBeVisible();
  // The field the API refused is marked at the input, too.
  const licence = panel.getByLabel("Licence");
  await expect(licence).toHaveAttribute("aria-invalid", "true");
  await expect(licence).toHaveAccessibleDescription(/String should have at least 3 characters/);
  await expect(panel.getByLabel("Source")).not.toHaveAttribute("aria-invalid", "true");
  await audit(page, "data page, import refused");

  // 413 from the API's own limit.
  await page.unroute("**/api/v1/workspaces/*/datasets/csv");
  await page.route("**/api/v1/workspaces/*/datasets/csv", (route) =>
    route.fulfill({ status: 413, contentType: "application/json", body: JSON.stringify({ error: { code: "upload_too_large", message: "Uploads are limited to 50 MB" } }) }),
  );
  await panel.getByRole("button", { name: "Import CSV" }).click();
  await expect(panel.getByRole("alert")).toContainText("Uploads are limited to 50 MB");
});
