// A demo reset clears the Ask tab: the last take's questions and answers are gone and the
// suggestions show again. Ends published (the specs share one server).
import { expect, test } from "@playwright/test";

test("demo reset clears the Ask tab", async ({ browser, request }) => {
  await request.post("/api/demo/reset", { data: { mode: "published" } });
  const charge = await browser.newPage();
  await charge.goto("/charge");
  await expect(charge.getByText("Published", { exact: true })).toBeVisible();

  // ask one question and wait for its answer, so the tab is non-empty
  await charge.getByRole("button", { name: "Ask", exact: true }).first().click();
  await charge.getByPlaceholder("Why Arun and not Priya?").fill("Why Arun and not Priya?");
  await charge.getByRole("button", { name: "Ask", exact: true }).last().click();
  await expect(charge.getByText("Why Arun and not Priya?", { exact: true })).toBeVisible();
  await expect(charge.getByText("Looking at the evidence…")).toBeHidden();
  await expect(charge.getByText("Try asking:")).toBeHidden();

  // reset mid-take: the page hears it (back on alerts) and the Ask tab starts empty
  await request.post("/api/demo/reset", { data: { mode: "published" } });
  await expect(charge.getByPlaceholder("Why Arun and not Priya?")).toBeHidden();
  await charge.getByRole("button", { name: "Ask", exact: true }).first().click();
  await expect(charge.getByText("Try asking:")).toBeVisible();
  await expect(charge.getByText("Why Arun and not Priya?", { exact: true })).toHaveCount(0);
});
