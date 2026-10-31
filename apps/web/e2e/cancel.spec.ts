import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// #23: a running dataset job can be cancelled from the page. The job's status is held at
// "running" by route interception, so the button is there however fast the worker is;
// the API's own cancel semantics are covered by the integration tests.

async function audit(page: Page, name: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const summary = results.violations.map((v) => `${v.id} (${v.impact}): ${v.help} [${v.nodes.map((n) => n.target.join(" ")).join("; ")}]`);
  expect(summary, `${name}\n${summary.join("\n")}`).toEqual([]);
}

test("a running dataset job is cancelled from the data page", async ({ page }) => {
  await page.goto("/register");
  await page.getByLabel("Name").fill("Cancel");
  await page.getByLabel("Email").fill(`cancel-${Date.now()}@example.com`);
  await page.getByLabel("Password").fill("Cancel-jobs-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();

  const cancels: string[] = [];
  await page.route(/\/api\/v1\/jobs\/[0-9a-f-]+(\/cancel)?$/, async (route) => {
    const req = route.request();
    if (req.method() === "POST") {
      cancels.push(req.url());
      const job = await (await page.request.get(req.url().replace(/\/cancel$/, ""))).json();
      return route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...job, status: "cancelled", message: "cancelled by user" }) });
    }
    const job = await (await route.fetch()).json();
    const status = cancels.length ? "cancelled" : "running";
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...job, status, progress: 0.3, message: status === "running" ? "generating bars" : "cancelled by user" }) });
  });

  await page.goto("/data");
  await page.getByLabel("Assets").fill("5");
  await page.getByLabel("Identifier").fill(`cancel-${Date.now()}`);
  await page.getByRole("button", { name: "Generate dataset version" }).click();
  const cancel = page.getByRole("button", { name: "Cancel run" });
  await expect(cancel).toBeVisible();
  await audit(page, "data page, running job");
  await cancel.click();
  await expect.poll(() => cancels.length).toBe(1);
  expect(cancels[0]).toMatch(/\/api\/v1\/jobs\/[0-9a-f-]{36}\/cancel$/);
  await expect(page.getByText("Cancelled", { exact: true })).toBeVisible();
  await expect(cancel).toHaveCount(0);
});
