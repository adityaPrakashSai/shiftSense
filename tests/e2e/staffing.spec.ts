// Staffing check panel (design doc 6.7, M14): with Kavya out for the period and standby allowed, the
// mini ward is short of target; the panel shows the result, the per-nurse load and the lower-bound
// caveat, and the published roster is unchanged.
import { expect, test } from "@playwright/test";

test("staffing check: Kavya out, standby allowed", async ({ page, request }) => {
  await request.post("/api/demo/reset");
  const before = await (await request.get("/api/roster")).json();
  await page.goto("/charge");
  await page.getByRole("button", { name: "Staffing", exact: true }).click();
  await expect(page.getByTestId("staffing-caveat")).toContainText("Lower bound");
  await page.getByRole("button", { name: "Kavya", exact: true }).click();
  await page.getByLabel("Standby may work").check();
  await page.getByRole("button", { name: "Run staffing check" }).click();
  const result = page.getByTestId("staffing-result");
  await expect(result).toContainText("slots short of target with 6 nurses");
  await expect(result).toContainText("Shortfall 4 ✓ proven");
  await expect(result.getByRole("cell", { name: "Kavya Reddy" })).toHaveCount(0);
  const after = await (await request.get("/api/roster")).json();
  expect(after.run.run_id).toBe(before.run.run_id);
});
