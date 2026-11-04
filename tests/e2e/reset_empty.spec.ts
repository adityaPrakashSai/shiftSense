// Reset demo (Start: Empty) after a published take: the charge view shows only the empty grid,
// not the old roster under the Generate card, and "Back to published" appears only once the
// period has a published roster. Resets to published at the end (the specs share one server).
import { expect, test } from "@playwright/test";
test("reset demo to empty after a published take", async ({ browser }) => {
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  await page.goto("/demo");
  const charge = page.getByRole("region", { name: "Charge nurse view", exact: true });
  const reset = async () => {
    await page.getByRole("button", { name: "empty", exact: true }).click();
    await page.getByRole("button", { name: "Reset demo" }).click();
    await page.getByRole("button", { name: "Yes, reset" }).click();
    await expect(page.getByTestId("reset-done")).toBeVisible();
  };
  await reset();
  await expect(charge.getByTestId("empty-roster")).toBeVisible();
  await charge.getByRole("button", { name: "Generate roster", exact: true }).click();
  await expect(charge.getByText("Draft (proposed)", { exact: true })).toBeVisible();
  await expect(charge.getByRole("button", { name: "Back to published" })).toHaveCount(0);
  await charge.getByRole("button", { name: "Publish", exact: true }).click();
  await expect(charge.getByText(/Coverage .* short/)).toBeVisible();
  await reset();
  await expect(charge.getByTestId("empty-roster")).toBeVisible();
  await expect(charge.getByText("Published", { exact: true })).toHaveCount(0);
  await expect(charge.locator("table")).toHaveCount(1); // the empty grid only, not the old roster
  await charge.getByRole("button", { name: "Generate roster", exact: true }).click();
  await expect(charge.getByText("Draft (proposed)", { exact: true })).toBeVisible();
  await charge.getByRole("button", { name: "Publish", exact: true }).click();
  await expect(charge.getByText(/Coverage .* short/)).toBeVisible();
  await charge.getByRole("button", { name: "Generate", exact: true }).click();
  await expect(charge.getByText("Draft (proposed)", { exact: true })).toBeVisible();
  await expect(charge.getByRole("button", { name: "Back to published" })).toHaveCount(1);
  // back to a published start, so the other specs find a roster
  await page.getByRole("button", { name: "published", exact: true }).click();
  await page.getByRole("button", { name: "Reset demo" }).click();
  await page.getByRole("button", { name: "Yes, reset" }).click();
  await expect(page.getByTestId("reset-done")).toBeVisible();
});
