import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Browser, type Page } from "@playwright/test";

// #23: workspace rename and delete, and removing a member, from the settings page.

async function audit(page: Page, name: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const summary = results.violations.map((v) => `${v.id} (${v.impact}): ${v.help} [${v.nodes.map((n) => n.target.join(" ")).join("; ")}]`);
  expect(summary, `${name}\n${summary.join("\n")}`).toEqual([]);
}

async function registerAs(page: Page, name: string, email: string) {
  await page.goto("/register");
  await page.getByLabel("Name").fill(name);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("Settings-flows-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
}

test("an owner renames and deletes a workspace", async ({ page }) => {
  await registerAs(page, "Owner", `settings-owner-${Date.now()}@example.com`);
  await page.request.post("/api/v1/workspaces", { data: { name: "Scratch" } });
  await page.goto("/settings");
  await page.getByLabel("Workspace", { exact: true }).selectOption({ label: "Scratch" });

  const panel = page.locator("section").filter({ hasText: "This workspace" });
  await panel.getByLabel("Name").fill("Scratch renamed");
  await panel.getByRole("button", { name: "Rename" }).click();
  await expect(page.getByRole("status")).toContainText("Workspace renamed to Scratch renamed.");
  await expect(page.getByLabel("Workspace", { exact: true }).locator("option", { hasText: "Scratch renamed" })).toHaveCount(1);

  const remove = panel.getByRole("button", { name: "Delete workspace" });
  await expect(remove).toBeDisabled();
  await panel.getByLabel("Type Scratch renamed to confirm").fill("Scratch renamed");
  await audit(page, "settings, delete confirmation");
  await remove.click();
  await expect(page.getByRole("status")).toContainText("Workspace Scratch renamed was deleted");
  await expect(page.getByLabel("Workspace", { exact: true }).locator("option", { hasText: "Scratch renamed" })).toHaveCount(0);
  const names = (await (await page.request.get("/api/v1/workspaces")).json()).map((w: { name: string }) => w.name);
  expect(names).not.toContain("Scratch renamed");
});

test("an owner removes a member, who then loses access", async ({ browser }: { browser: Browser }) => {
  const stamp = Date.now();
  const owner = await (await browser.newContext()).newPage();
  await registerAs(owner, "Owner", `settings-o-${stamp}@example.com`);
  const ws = (await (await owner.request.get("/api/v1/workspaces")).json())[0].id as string;
  const email = `settings-m-${stamp}@example.com`;
  const created = await (await owner.request.post(`/api/v1/workspaces/${ws}/invitations`, { data: { email, role: "viewer" } })).json();
  const token = new URLSearchParams(String(created.accept_path).split("#")[1]).get("token");
  const member = await (await browser.newContext()).newPage();
  await registerAs(member, "Member", email);
  expect((await member.request.post("/api/v1/auth/invitations/accept", { data: { token } })).ok()).toBe(true);
  expect((await member.request.get(`/api/v1/workspaces/${ws}`)).status()).toBe(200);

  await owner.goto("/settings");
  const row = owner.getByRole("row").filter({ hasText: email });
  await row.getByRole("button", { name: "Remove Member" }).click();
  await row.getByRole("button", { name: "Remove Member" }).click(); // the confirmation
  await expect(owner.getByRole("status")).toContainText("Member was removed");
  await expect(owner.getByRole("row").filter({ hasText: email })).toHaveCount(0);
  expect((await member.request.get(`/api/v1/workspaces/${ws}`)).status()).toBe(404);
});
