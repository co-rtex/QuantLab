import { expect, test } from "@playwright/test";

test("proxy refuses paths outside the versioned API", async ({ request }) => {
  for (const p of ["/api/v1/..%2F..%2Fmetrics", "/api/metrics", "/api/v1/%2E%2E/%2E%2E/metrics"]) {
    // Depending on URL normalization the request is either refused by the API proxy (404)
    // or never reaches it (redirect to sign-in). Either way upstream metrics must not leak.
    const r = await request.get(p, { maxRedirects: 0 });
    expect([404, 307], p).toContain(r.status());
    expect(await r.text()).not.toContain("quantlab_http_request_duration_seconds");
  }
});

test("post-login redirect stays on this origin", async ({ page }) => {
  const email = `redir-${Date.now()}@example.com`;
  await page.goto(`/register?next=${encodeURIComponent("/\\evil.example.com")}`);
  await page.getByLabel("Name").fill("R");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("Redirect-test-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/^http:\/\/localhost:3000\//);
});
