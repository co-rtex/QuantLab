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

for (const next of ["/\\evil.example.com", "/.//evil.example.com", "//evil.example.com", "https://evil.example.com"]) {
test(`post-login redirect stays on this origin: ${next}`, async ({ page, baseURL }) => {
  const email = `redir-${Date.now()}-${Math.random().toString(36).slice(2)}@example.com`;
  await page.goto(`/register?next=${encodeURIComponent(next)}`);
  await page.getByLabel("Name").fill("R");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("Redirect-test-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  // The configured origin, not a fixed port: each git worktree serves the app on its own.
  const origin = new URL(baseURL ?? "http://localhost:3000").origin;
  // Leaving /register proves the redirect happened; it must land on this origin.
  await expect(page).toHaveURL((url) => url.origin === origin && !url.pathname.startsWith("/register"));
});
}
