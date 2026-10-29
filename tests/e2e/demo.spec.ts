// The demo script (design doc 2.2, 11.3) in mock mode: Meena's sick call in Hindi, confirm, the
// agent's proposal on the charge view, a why-question with claim chips, Approve, Arun's inbox
// with the drafted message, Accept, and the agent trace and decision log showing it all.
import { expect, test } from "@playwright/test";

test("sick call to accepted replacement", async ({ browser }) => {
  const meena = await browser.newPage({ viewport: { width: 400, height: 860 } });
  const charge = await browser.newPage();
  await charge.goto("/charge");
  await expect(charge.getByText("Published", { exact: true })).toBeVisible();

  // 2-3. Meena reports sick in Hindi and confirms the parsed edit
  await meena.goto("/nurse/N03");
  await meena.getByPlaceholder(/Message likhiye|Type a message/).fill("Sunita di, aaj subah ki duty nahi kar paungi, bukhar hai");
  await meena.keyboard.press("Enter");
  await meena.getByRole("button", { name: /Confirm/ }).first().click();

  // 4-5. the charge view shows the ranked proposal with the agent's claim-checked summary
  const summary = charge.getByTestId("agent-summary");
  await expect(summary).toBeVisible();
  await expect(summary).toContainText("Arun Kumar");
  await expect(charge.getByText(/Meena Sharma off/)).toBeVisible();

  // 6. why Arun and not Priya: grounded answer with claim chips
  await charge.getByRole("button", { name: "Ask", exact: true }).first().click();
  await charge.getByPlaceholder("Why Arun and not Priya?").fill("Why Arun and not Priya?");
  await charge.getByRole("button", { name: "Ask", exact: true }).last().click();
  await expect(charge.getByRole("button", { name: /^✓/ }).first()).toBeVisible();

  // 7. approve the top candidate
  await charge.getByRole("button", { name: "Alerts", exact: true }).click();
  await charge.getByRole("button", { name: "Approve" }).first().click();
  await expect(charge.getByTestId("agent-summary")).toHaveCount(0);

  // 8. Arun's inbox shows the drafted ask; he accepts
  const arun = await browser.newPage({ viewport: { width: 400, height: 860 } });
  await arun.goto("/nurse/S01");
  await expect(arun.getByTestId("inbox-badge")).toHaveText("1");
  await arun.getByRole("button", { name: /Inbox/ }).click();
  const item = arun.getByTestId("inbox-item");
  await expect(item).toContainText("3 Nov");
  await expect(item).not.toContainText(/bukhar|fever/i);
  await arun.getByRole("button", { name: /Haan|Accept/ }).click();
  await expect(item).toContainText(/accept/i);

  // 9. the agent trace ends done; the log has the approval and the acceptance
  await charge.getByRole("button", { name: "Agent", exact: true }).click();
  const task = charge.getByTestId("agent-task").first();
  await expect(task).toContainText("done");
  await expect(task).toContainText("Arun Kumar accepted");
  await charge.getByRole("button", { name: "Log", exact: true }).click();
  await expect(charge.getByText("replacement.accepted").first()).toBeVisible();
  await expect(charge.getByText("run.published").first()).toBeVisible();
});
