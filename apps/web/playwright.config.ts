import { defineConfig, devices } from "@playwright/test";

// Expects a running stack (web + API + worker), e.g. `make app-up` or the dev processes.
export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  // One worker by default: parallel spec files on a shared machine starved the API (25 s
  // registrations) and produced false failures. E2E_WORKERS=n opts back in.
  workers: Number(process.env.E2E_WORKERS ?? 1),
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    // For runs through the TLS edge (infra/edge/), whose development certificate comes from
    // Caddy's local CA.
    ignoreHTTPSErrors: process.env.E2E_IGNORE_HTTPS_ERRORS === "1",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "mobile", use: { ...devices["Pixel 7"] }, testMatch: /smoke\.spec\.ts/ },
  ],
});
