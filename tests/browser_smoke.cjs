const assert = require("node:assert/strict");
const { chromium } = require("playwright");

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.BROWSER_EXECUTABLE || undefined,
  });
  const page = await browser.newPage();
  const errors = [];
  page.on("console", message => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/api/models", route => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ codex: { models: [] }, claude: { models: [] } }),
  }));

  const response = await page.goto("http://127.0.0.1:8765", { waitUntil: "networkidle" });
  assert.equal(response.status(), 200);
  assert.match((await response.allHeaders())["content-security-policy"], /script-src 'nonce-/);
  assert.equal(await page.locator("#startRunButton").isVisible(), true);

  const started = await page.evaluate(() => post("/api/runs", {
    task: "browser smoke", lead: "codex", reviewer: "claude",
    mock: "both", human_gate: true,
  }));
  await page.evaluate(async runId => {
    selected = runId;
    renderedRun = null;
    await show();
  }, started.run_id);
  const input = page.locator("#humanText");
  await input.waitFor({ state: "visible" });
  await input.fill("keep this intervention while polling");
  await input.focus();
  await page.waitForTimeout(2500);
  assert.equal(await input.inputValue(), "keep this intervention while polling");
  assert.equal(await input.evaluate(element => document.activeElement === element), true);

  await page.getByRole("button", { name: "取消", exact: true }).click();
  await page.waitForFunction(() => !document.querySelector("#humanText"));
  assert.deepEqual(errors, []);
  await browser.close();
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
