// The /demo split view (design doc 10.3, M11): "/" lands on /demo; the sick-call chip sends
// Meena's message from the phone; confirm, approve on the charge side, Arun accepts in the phone;
// Reset demo restores the published baseline. Then the same page at phone width.
import { expect, test } from "@playwright/test";

test("demo page: chip to accepted replacement, then reset", async ({ page, request }) => {
  await request.post("/api/demo/reset"); // the other spec may have run first
  await page.goto("/");
  await expect(page).toHaveURL(/\/demo$/);
  // the demo banner (the charge header's provider badge says the same once a model has answered)
  await expect(page.getByText("Model: Muse Spark Open on Baseten").first()).toBeVisible();
  const charge = page.getByRole("region", { name: "Charge nurse view", exact: true });
  const phone = page.getByRole("region", { name: "Nurse phone", exact: true });
  await expect(charge.getByText("Published", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: /Sick call \(Hindi\)/ }).click();
  await expect(phone.getByText("aaj subah ki duty nahi kar paungi")).toBeVisible();
  await phone.getByRole("button", { name: /Confirm/ }).first().click();

  await expect(charge.getByTestId("agent-summary")).toContainText("Arun Kumar");
  await charge.getByRole("button", { name: "Approve" }).first().click();
  await expect(charge.getByTestId("agent-summary")).toHaveCount(0);

  await page.getByLabel("Phone", { exact: true }).selectOption("S01");
  await expect(phone.getByTestId("inbox-badge")).toHaveText("1");
  await phone.getByRole("button", { name: /Inbox/ }).click();
  await phone.getByRole("button", { name: /Haan|Accept/ }).click();
  await expect(phone.getByTestId("inbox-item")).toContainText(/accept/i);

  await page.getByRole("button", { name: "Reset demo" }).click();
  await expect(page.getByTestId("reset-done")).toBeVisible();
  await expect(page.getByLabel("Phone", { exact: true })).toHaveValue("N03");
  await expect(charge.getByText("Published", { exact: true })).toBeVisible();
  await page.getByLabel("Phone", { exact: true }).selectOption("S01");
  await expect(phone.getByTestId("inbox-badge")).toHaveCount(0);
});

test("demo page at phone width shows one pane at a time", async ({ browser, request }) => {
  await request.post("/api/demo/reset"); // a clean chat for Priya, whatever ran before
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.goto("/demo");
  const charge = page.getByRole("region", { name: "Charge nurse view", exact: true });
  const phone = page.getByRole("region", { name: "Nurse phone", exact: true });
  await expect(charge).toBeVisible();
  await expect(phone).toBeHidden();
  await page.getByRole("button", { name: /Why every weekend/ }).click();
  await expect(phone).toBeVisible();
  await expect(charge).toBeHidden();
  await expect(phone.getByText("Why do I get every weekend?")).toBeVisible();
  const width = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(width).toBeLessThanOrEqual(390);
});
