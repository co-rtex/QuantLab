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

const switcher = (page: Page) => page.getByLabel("Workspace", { exact: true });

test("an owner renames and deletes a workspace, once however often they click", async ({ page }) => {
  await registerAs(page, "Owner", `settings-owner-${Date.now()}@example.com`);
  const scratch = (await (await page.request.post("/api/v1/workspaces", { data: { name: "Scratch" } })).json()).id as string;
  await page.goto("/settings");
  await switcher(page).selectOption({ label: "Scratch" });

  const panel = page.locator("section").filter({ hasText: "This workspace" });
  await panel.getByLabel("Name").fill("Scratch renamed");
  await panel.getByRole("button", { name: "Rename" }).click();
  await expect(page.getByRole("status")).toContainText("Workspace renamed to Scratch renamed.");
  await expect(switcher(page).locator("option", { hasText: "Scratch renamed" })).toHaveCount(1);

  const deletes: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "DELETE" && r.url().endsWith(`/api/v1/workspaces/${scratch}`)) deletes.push(r.url());
  });
  const remove = panel.getByRole("button", { name: "Delete workspace" });
  await expect(remove).toBeDisabled();
  await panel.getByLabel("Type Scratch renamed to confirm").fill("Scratch renamed");
  await audit(page, "settings, delete confirmation");
  await remove.dblclick();
  await expect(page.getByRole("status")).toContainText("Workspace Scratch renamed was deleted");
  expect(deletes).toHaveLength(1);
  await expect(page.getByRole("alert").filter({ hasText: /not found/i })).toHaveCount(0);
  await expect(switcher(page).locator("option", { hasText: "Scratch renamed" })).toHaveCount(0);
});

test("deleting the last workspace leaves nothing selected and says so", async ({ page }) => {
  await registerAs(page, "Alone", `settings-alone-${Date.now()}@example.com`);
  await page.goto("/settings");
  const name = await switcher(page).locator("option:checked").innerText();
  const panel = page.locator("section").filter({ hasText: "This workspace" });
  await panel.getByLabel(`Type ${name} to confirm`).fill(name);
  await panel.getByRole("button", { name: "Delete workspace" }).click();
  await expect(page.getByRole("status").filter({ hasText: "You have no workspaces." })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("quantlab.workspace"))).toBeNull();
  await page.reload();
  await expect(page.getByRole("status").filter({ hasText: "You have no workspaces." })).toBeVisible();
});

test("an owner removes a member through a separate confirmation, who then loses access", async ({ browser }: { browser: Browser }) => {
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
  const open = owner.getByRole("button", { name: "Remove Member" });
  const step = owner.getByRole("group", { name: /Remove Member from/ });

  // Opening moves focus into the step; Escape closes it and returns focus to the row.
  await open.click();
  await expect(step).toBeFocused();
  await expect(step.getByRole("button", { name: "Confirm removing Member" })).toBeVisible();
  await audit(owner, "settings, member removal confirmation");
  await owner.keyboard.press("Escape");
  await expect(step).toHaveCount(0);
  await expect(open).toBeFocused();

  // "Keep" closes it too; a double click on Remove only opens it.
  await open.dblclick();
  await step.getByRole("button", { name: "Keep Member" }).click();
  await expect(open).toBeFocused();
  expect((await member.request.get(`/api/v1/workspaces/${ws}`)).status()).toBe(200);

  await open.click();
  await step.getByRole("button", { name: "Confirm removing Member" }).click();
  await expect(owner.getByRole("status")).toContainText("Member was removed");
  await expect(owner.getByRole("row").filter({ hasText: email })).toHaveCount(0);
  await expect(owner.locator("h2 span", { hasText: "Members" })).toBeFocused();
  expect((await member.request.get(`/api/v1/workspaces/${ws}`)).status()).toBe(404);
});
