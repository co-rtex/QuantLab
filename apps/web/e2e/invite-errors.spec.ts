import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Browser, type Page, type Route } from "@playwright/test";

// #85: only a 401 from /auth/me means "signed out". An outage used to show the sign-in
// prompt to a signed-in invitee, and never said the API was down.

async function audit(page: Page, name: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const summary = results.violations.map((v) => `${v.id} (${v.impact}): ${v.help} [${v.nodes.map((n) => n.target.join(" ")).join("; ")}]`);
  expect(summary, `${name}\n${summary.join("\n")}`).toEqual([]);
}

async function registerAs(page: Page, name: string, email: string) {
  await page.goto("/register");
  await page.getByLabel("Name").fill(name);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("Invite-errors-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.getByText("This workspace has no market data yet")).toBeVisible();
}

/** A signed-in invitee and the accept link the owner would pass on. */
async function invited(browser: Browser, tag: string) {
  const stamp = `${tag}-${Date.now()}`;
  const owner = await (await browser.newContext()).newPage();
  await registerAs(owner, "Owner", `owner-${stamp}@example.com`);
  const ws = (await (await owner.request.get("/api/v1/workspaces")).json())[0].id;
  const email = `invitee-${stamp}@example.com`;
  const created = await (await owner.request.post(`/api/v1/workspaces/${ws}/invitations`, { data: { email, role: "viewer" } })).json();
  const invitee = await (await browser.newContext()).newPage();
  await registerAs(invitee, "Invitee", email);
  return { invitee, link: created.accept_path as string };
}

const fail = (status: number) => (route: Route) =>
  route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify({ error: { code: "internal_error", message: "Internal server error", request_id: "req-e2e-85" } }),
  });

test("an outage on /auth/me shows an error with Try again, not the sign-in prompt", async ({ browser }) => {
  const { invitee, link } = await invited(browser, "me5xx");
  await invitee.route("**/api/v1/auth/me", fail(503));
  await invitee.goto(link);
  const alert = invitee.getByRole("alert").filter({ hasText: "could not check the invitation" });
  await expect(alert).toBeVisible();
  await expect(invitee.getByText("Request req-e2e-85")).toBeVisible();
  await expect(invitee.getByRole("link", { name: "Sign in" })).toHaveCount(0);
  await expect(invitee.getByRole("button", { name: "Sign out and use another account" })).toHaveCount(0);
  await audit(invitee, "invite, /auth/me failed");

  // The token left the address bar but is kept, so trying again finds the invitation.
  await invitee.unroute("**/api/v1/auth/me");
  await invitee.getByRole("button", { name: "Try again" }).click();
  await expect(invitee.getByText(/invited you to .* as a viewer/)).toBeVisible();
  await expect(alert).toHaveCount(0);
  await expect(invitee.getByRole("button", { name: "Try again" })).toHaveCount(0);
  // Focus moved to the answer, not to the document body, when Try again disappeared.
  await expect(invitee.locator(":focus")).toContainText("invited you to");
});

test("a network failure on the preview offers Try again, not another account", async ({ browser }) => {
  const { invitee, link } = await invited(browser, "previewnet");
  await invitee.route("**/api/v1/auth/invitations/preview", (route) => route.abort("failed"));
  await invitee.goto(link);
  await expect(invitee.getByRole("alert").filter({ hasText: "could not be reached" })).toBeVisible();
  await expect(invitee.getByRole("button", { name: "Sign out and use another account" })).toHaveCount(0);
  await invitee.unroute("**/api/v1/auth/invitations/preview");
  await invitee.getByRole("button", { name: "Try again" }).click();
  await expect(invitee.getByText(/invited you to .* as a viewer/)).toBeVisible();
});

test("a 401 on /auth/me still means signed out", async ({ browser }) => {
  const { invitee, link } = await invited(browser, "me401");
  await invitee.context().clearCookies();
  await invitee.goto(link);
  await expect(invitee.getByRole("link", { name: "Sign in" })).toBeVisible();
  // Next's route announcer is also role="alert"; match ours by text.
  await expect(invitee.getByRole("alert").filter({ hasText: /QuantLab|invitation/ })).toHaveCount(0);
});
