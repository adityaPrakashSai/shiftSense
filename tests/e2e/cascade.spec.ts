// The cascade in a browser (Fix 2): with AGENT_CASCADE=1, Meena's sick call proposes one
// contact plan; the charge nurse approves it once; Arun declines in his inbox, Ritu's ask is
// skipped and expires, Kavya accepts; the Alerts card's Publish publishes Kavya's run and only
// Kavya's. Runs against the second web server in playwright.config.ts (port 8766).
import { expect, test } from "@playwright/test";

const BASE = "http://127.0.0.1:8766";
test.use({ baseURL: BASE });

test("cascade: decline, skip, accept, publish the third nurse", async ({ browser, request }) => {
  await request.post("/api/demo/reset");
  const charge = await browser.newPage();
  await charge.goto(`${BASE}/charge`);
  await expect(charge.getByText("Published", { exact: true })).toBeVisible();

  // Meena reports sick in Hindi and confirms the parsed edit
  const meena = await browser.newPage({ viewport: { width: 400, height: 860 } });
  await meena.goto(`${BASE}/nurse/N03`);
  await meena.getByPlaceholder(/Message likhiye|Type a message/).fill("Sunita di, aaj subah ki duty nahi kar paungi, bukhar hai");
  await meena.keyboard.press("Enter");
  await meena.getByRole("button", { name: /Confirm/ }).first().click();

  // the plan asks Arun, then Ritu, then Kavya; approving it publishes nothing
  const plan = charge.getByTestId("cascade-plan");
  await expect(plan).toBeVisible();
  await expect(plan).toContainText("Arun Kumar → Ritu Singh → Kavya Reddy");

  // every inbox opens before the plan starts: each ask expires 5 s after it goes out
  const arun = await browser.newPage({ viewport: { width: 400, height: 860 } });
  await arun.goto(`${BASE}/nurse/S01`);
  await arun.getByRole("button", { name: /Inbox/ }).click();
  const ritu = await browser.newPage({ viewport: { width: 400, height: 860 } });
  await ritu.goto(`${BASE}/nurse/N05`);
  await ritu.getByRole("button", { name: /Inbox/ }).click();
  const kavya = await browser.newPage({ viewport: { width: 400, height: 860 } });
  await kavya.goto(`${BASE}/nurse/N04`);
  await kavya.getByRole("button", { name: /Inbox/ }).click();

  await plan.getByRole("button", { name: "Approve plan" }).click();
  await expect(charge.getByTestId("cascade-running")).toContainText("Plan: Arun Kumar → Ritu Singh → Kavya Reddy");
  await charge.getByRole("button", { name: "Agent", exact: true }).click();

  // Arun declines in his inbox; Ritu is asked next
  await expect(arun.getByTestId("inbox-item")).toBeVisible();
  await arun.getByRole("button", { name: "Nahi kar paungi", exact: true }).click();
  await expect(charge.getByTestId("waiting-line")).toContainText("Ritu Singh");

  // skipping expires her ask mid-wait
  await charge.getByRole("button", { name: "Skip to next" }).click();
  await expect(ritu.getByTestId("inbox-item")).toContainText("Yeh request samay par poori nahi hui, dhanyavaad");

  // Kavya accepts
  await expect(kavya.getByTestId("inbox-item")).toBeVisible();
  await kavya.getByRole("button", { name: "Accept", exact: true }).click();

  // the Alerts card publishes Kavya's run; the grid shows her on Tue 3 Nov M
  await charge.getByRole("button", { name: "Alerts", exact: true }).click();
  await expect(charge.getByTestId("agent-summary")).toContainText("Kavya Reddy accepted");
  await charge.getByTestId("proposal-publish").click();
  await expect(charge.getByText("Published", { exact: true }).first()).toBeVisible();
  const cell = charge.locator('td[title^="N04 2026-11-03: M"]');
  await expect(cell).toBeVisible();
  await expect(cell).toContainText("M");

  // the task is done with the whole trail behind it
  await charge.getByRole("button", { name: "Agent", exact: true }).click();
  const task = charge.getByTestId("agent-task").first();
  await expect(task).toContainText("done");
  await expect(task).toContainText("asked Arun Kumar (declined)");
  await expect(task).toContainText("asked Ritu Singh (timeout)");
  await expect(task).toContainText("asked Kavya Reddy (accepted)");
});
