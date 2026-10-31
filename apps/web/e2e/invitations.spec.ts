import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// Consent-based membership (#5): the owner invites an address, the invitee accepts through
// the link delivered to them. Locally that link lands in the development mailbox.

async function audit(page: Page, name: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const summary = results.violations.map((v) => `${v.id} (${v.impact}): ${v.nodes.length} node(s) — ${v.help}`);
  expect(summary, `${name}\n${summary.join("\n")}`).toEqual([]);
}

async function registerAs(page: Page, name: string, email: string) {
  await page.goto("/register");
  await page.getByLabel("Name").fill(name);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("Invitation-test-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
}

test("invite -> accept: membership only after the invitee consents", async ({ browser }) => {
  const stamp = Date.now();
  const ownerEmail = `owner-${stamp}@example.com`;
  const inviteeEmail = `invitee-${stamp}@example.com`;
  const owner = await (await browser.newContext()).newPage();
  const invitee = await (await browser.newContext()).newPage();

  await registerAs(owner, "Owner", ownerEmail);
  await owner.goto("/settings");
  await owner.getByLabel("Invite by email").fill(inviteeEmail);
  // The label wraps the select, so its accessible name includes the chosen option.
  await owner.locator("form select[name=role]").selectOption("editor");
  await owner.getByRole("button", { name: "Send invitation" }).click();
  await expect(owner.getByRole("status")).toContainText(`Invitation sent to ${inviteeEmail}`);
  await expect(owner.getByRole("cell", { name: inviteeEmail })).toBeVisible(); // pending, not a member
  await audit(owner, "settings with a pending invitation");

  // The invitee signs up, finds the invitation in the development mailbox, then opens the
  // link signed out, as from an email.
  await registerAs(invitee, "Invitee", inviteeEmail);
  const mail = await (await invitee.request.get("/api/v1/auth/invitations/dev-mailbox")).json();
  expect(mail).toHaveLength(1);
  const link: string = mail[0].accept_path;
  expect(link).toMatch(/^\/invite#token=/);
  await invitee.context().clearCookies();
  await invitee.goto(link);
  await expect(invitee).toHaveURL(/\/invite$/); // the token left the address bar
  await invitee.getByRole("link", { name: "Sign in" }).click();
  await invitee.getByLabel("Email").fill(inviteeEmail);
  await invitee.getByLabel("Password").fill("Invitation-test-2026");
  await invitee.getByRole("button", { name: "Sign in" }).click();
  await expect(invitee.getByText(/invited you to .* as an? editor/)).toBeVisible();
  await audit(invitee, "invitation preview");
  await invitee.getByRole("button", { name: "Accept invitation" }).click();
  await expect(invitee.getByRole("status")).toContainText("You joined");

  await owner.reload();
  const row = owner.getByRole("row", { name: new RegExp(inviteeEmail) });
  await expect(row.getByRole("combobox")).toHaveValue("editor");
  await expect(owner.getByText("Pending invitations")).toHaveCount(0);

  // The same link cannot be used again, and is no use to anyone else. (Leave /invite first:
  // a fragment-only change would not reload the page.)
  await invitee.goto("/settings");
  await invitee.goto(link);
  await expect(invitee.getByRole("alert").filter({ hasText: "already been used" })).toBeVisible();
  const other = await (await browser.newContext()).newPage();
  await registerAs(other, "Other", `other-${stamp}@example.com`);
  await other.goto(link);
  await expect(other.getByRole("alert").filter({ hasText: "not valid for the account" })).toBeVisible();
});
