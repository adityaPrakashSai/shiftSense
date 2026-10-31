// The empty-start opening in mock mode: reset empty through the API, the charge view shows
// the Generate card over an empty grid, Generate roster produces a draft, Publish fills the
// grid and restores the stage strip. Resets to published at the end so other specs are
// unaffected (the specs share one server).
import { expect, test } from "@playwright/test";

test("empty start: generate and publish from nothing", async ({ browser, request }) => {
  await request.post("/api/demo/reset", { data: { mode: "empty" } });
  const charge = await browser.newPage();
  await charge.goto("/charge");
  await expect(charge.getByTestId("empty-roster")).toBeVisible();
  await expect(charge.getByText("No roster for 26 Oct – 8 Nov yet")).toBeVisible();

  await charge.getByRole("button", { name: "Generate roster", exact: true }).click();
  await expect(charge.getByText("Draft (proposed)", { exact: true })).toBeVisible();
  await expect(charge.getByTestId("empty-roster")).toHaveCount(0);

  await charge.getByRole("button", { name: "Publish", exact: true }).click();
  await expect(charge.getByText("Published", { exact: true }).first()).toBeVisible();
  await expect(charge.getByText(/Coverage .* short/)).toBeVisible();
  await expect(charge.locator("table td span", { hasText: "M" }).first()).toBeVisible();

  await request.post("/api/demo/reset", { data: { mode: "published" } });
});
