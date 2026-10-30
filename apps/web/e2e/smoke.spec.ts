import { expect, test } from "@playwright/test";

test("login page renders and rejects bad credentials", async ({ page }) => {
  await page.goto("/login");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.getByLabel("Email").fill("nobody@example.com");
  await page.getByLabel("Password").fill("Wrong-password-1");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByText("Invalid email or password")).toBeVisible();
});

test("protected pages redirect to login", async ({ page }) => {
  await page.goto("/experiments");
  await expect(page).toHaveURL(/\/login\?next=%2Fexperiments/);
});
