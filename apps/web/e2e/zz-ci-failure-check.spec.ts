import { expect, test } from "@playwright/test";

// TEMPORARY (#159): a deliberate failure to verify that the E2E workflow fails and uploads
// its report, traces, screenshots and stack logs. Reverted in the next commit.
test("deliberate failure: the CI artifact path works", async ({ page }) => {
  await page.goto("/login");
  await expect(page.getByText("this text is deliberately absent")).toBeVisible({ timeout: 2_000 });
});
