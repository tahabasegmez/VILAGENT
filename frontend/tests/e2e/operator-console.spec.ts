import { expect, test } from "@playwright/test";

import { mockLangGraphAPI } from "./utils/mock-api";

test.describe("VILAGENT operator console", () => {
  test("renders the operator route and can load browser health", async ({
    page,
  }) => {
    mockLangGraphAPI(page);
    await page.route("**/api/computer-use/browser/health", (route) =>
      route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          enabled: true,
          healthy: true,
          provider_name: "browser-use",
          active_sessions: 0,
        }),
      }),
    );

    await page.goto("/workspace/operator");

    await expect(
      page.getByRole("heading", { name: "Computer-use control surface" }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Planner Timeline" }),
    ).toBeVisible();
    await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Approvals" }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Lifecycle Events" }),
    ).toBeVisible();

    await page.getByRole("button", { name: "Health" }).click();

    await expect(
      page.getByText("Browser provider browser-use: healthy"),
    ).toBeVisible();
  });
});
