// Voice note and roster photo import (design doc 7.3, 7.8, M12), in mock mode. The voice chip sends
// the committed Hindi clip, which the mock "hears" from tests/fixtures/voice; the bubble shows the
// transcript with a mic mark and a Confirm. The mock cannot see, so the photo read is stubbed with a
// sheet from evals/vision/labels.json with one unreadable cell; the preview, the fix and the real
// import endpoint are exercised.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "@playwright/test";

const labels = JSON.parse(readFileSync(join(__dirname, "../../evals/vision/labels.json"), "utf-8"));

test("voice note chip: transcript with Confirm", async ({ page, request }) => {
  await request.post("/api/demo/reset");
  await page.goto("/demo");
  const phone = page.getByRole("region", { name: "Nurse phone", exact: true });
  await page.getByRole("button", { name: /Voice note \(Hindi\)/ }).click();
  await expect(phone.getByLabel("Voice note").first()).toBeVisible();
  await expect(phone.getByText("aaj subah ki duty nahi kar paungi")).toBeVisible();
  await expect(phone.getByRole("button", { name: /Confirm/ }).first()).toBeVisible();
});

test("photo import: preview, fix the unreadable cell, import as the published plan", async ({ page, request }) => {
  await request.post("/api/demo/reset");
  const sheet = labels.find((s: { id: string }) => s.id === "mini-a");
  const start = new Date(`${sheet.start}T00:00:00Z`);
  const days = sheet.rows[0].cells.map((_: string, i: number) =>
    new Date(start.getTime() + i * 86400000).toISOString().slice(0, 10),
  );
  const rows = sheet.rows.map((r: { name: string; nurse_id: string; cells: string[] }, i: number) => ({
    name: r.name,
    nurse_id: r.nurse_id,
    candidates: [r.nurse_id],
    flagged: false,
    cells: i === 0 ? ["?", ...r.cells.slice(1)] : r.cells,
  }));
  const first = sheet.rows[0].cells[0];
  await page.route("**/api/import/photo", (route) =>
    route.fulfill({
      json: { days, rows, totals: sheet.totals, mismatches: [], unreadable: 1, tiles: 1, sheet_days: days.map((d: string) => Number(d.slice(8))), call_ids: [], cache_hits: 0 },
    }),
  );

  await page.goto("/charge");
  await page.getByRole("button", { name: "Import photo" }).click();
  const dialog = page.getByRole("dialog", { name: "Import roster photo" });
  await dialog.locator('input[type="file"]').setInputFiles(join(__dirname, "../../evals/vision/mini-a.jpg"));
  await expect(dialog.getByTestId("photo-summary")).toContainText("1 unreadable");
  await expect(dialog.getByTestId("photo-summary")).toContainText("1 day misses");
  const importBtn = dialog.getByRole("button", { name: "Import as published plan" });
  await expect(importBtn).toBeDisabled();

  await dialog.getByLabel(`${sheet.rows[0].name} day 1`, { exact: true }).selectOption(first);
  await expect(dialog.getByTestId("photo-summary")).toContainText("0 unreadable");
  await expect(dialog.getByTestId("photo-summary")).toContainText("0 days miss");
  await importBtn.click();
  await expect(dialog.getByTestId("import-done")).toContainText("Imported and published");
  const roster = await (await request.get("/api/roster")).json();
  expect(roster.run.kind).toBe("import");
});
