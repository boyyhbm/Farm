const { chromium } = require("@playwright/test");
const fs = require("fs");
const path = require("path");
const localConfig = path.join(__dirname, "../../.env");
const env = Object.fromEntries(
  (fs.existsSync(localConfig) ? fs.readFileSync(localConfig, "utf8") : "")
    .split("\n")
    .filter((x) => x.includes("="))
    .map((x) => {
      const i = x.indexOf("=");
      return [x.slice(0, i), x.slice(i + 1).replace(/^['"]|['"]$/g, "")];
    }),
);
(async () => {
  fs.mkdirSync(path.join(__dirname, "../../tests/e2e/artifacts"), {
    recursive: true,
  });
  const browser = await chromium.launch({
    executablePath:
      process.env.E2E_BROWSER_PATH ||
      (fs.existsSync("/usr/bin/chromium") ? "/usr/bin/chromium" : undefined),
    headless: true,
    args: ["--no-sandbox"],
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1100 },
  });
  const errors = [];
  const failures = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("response", (r) => {
    if (
      r.url().includes("/api/v1") &&
      r.status() >= 400 &&
      !r.url().endsWith("/auth/me")
    )
      failures.push(`${r.status()} ${r.url()}`);
  });
  await page.goto(process.env.E2E_BASE_URL || "http://localhost:3000");
  await page.getByRole("textbox", { name: "Имя пользователя" }).waitFor();
  await page
    .locator("input[name=username]")
    .fill(process.env.E2E_USERNAME || env.ADMIN_USERNAME || "admin");
  await page
    .locator("input[name=password]")
    .fill(process.env.E2E_PASSWORD || env.ADMIN_PASSWORD);
  await page.getByRole("button", { name: "Войти", exact: true }).click();
  await page
    .getByRole("heading", { name: "Обзор", exact: true })
    .waitFor({ timeout: 20000 });
  await page.screenshot({
    path: path.join(
      __dirname,
      "../../tests/e2e/artifacts/dashboard-desktop.png",
    ),
    fullPage: true,
  });
  for (const label of [
    "Аккаунты",
    "Компьютеры",
    "Сессии",
    "Планировщик",
    "Недельные награды",
    "Инвентарь",
    "Рынок",
    "Аналитика",
    "Журнал аудита",
    "Настройки",
  ]) {
    await page
      .getByRole("navigation")
      .getByRole("button", { name: new RegExp("^" + label + "(?:\\s|$)") })
      .click();
    await page.getByRole("heading", { name: label, exact: true }).waitFor();
    await page.waitForTimeout(150);
  }
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Аккаунты", exact: true })
    .click();
  await page.getByRole("button", { name: "Добавить", exact: true }).click();
  await page.locator("input[name=steam_id]").fill("76561199999888881");
  await page
    .locator("textarea[name=notes]")
    .fill("BROWSER_E2E_SYNTHETIC_FIXTURE");
  await page
    .getByRole("button", { name: "Confirm / Подтвердить", exact: true })
    .click();
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  await page.getByRole("link", { name: "76561199999888881" }).waitFor();
  const row = page
    .getByRole("row")
    .filter({ has: page.getByRole("link", { name: "76561199999888881" }) });
  await row.getByRole("button", { name: "Изменить", exact: true }).click();
  await page.locator("textarea[name=notes]").fill("BROWSER_E2E_EDITED");
  await page
    .getByRole("button", { name: "Confirm / Подтвердить", exact: true })
    .click();
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  await row.getByRole("button", { name: "Удалить", exact: true }).click();
  await page
    .getByRole("button", { name: "Confirm / Подтвердить", exact: true })
    .click();
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  await page
    .getByRole("link", { name: "76561199999888881" })
    .waitFor({ state: "hidden" });
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Обзор", exact: true })
    .click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: path.join(
      __dirname,
      "../../tests/e2e/artifacts/dashboard-mobile.png",
    ),
    fullPage: true,
  });
  await page.getByRole("button", { name: "Open menu" }).click();
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Аккаунты", exact: true })
    .click();
  await page.getByRole("heading", { name: "Аккаунты", exact: true }).waitFor();
  const result = {
    checked_at: new Date().toISOString(),
    sections: 11,
    account_create_edit_delete: true,
    mobile_navigation: true,
    pageErrors: errors,
    apiFailures: failures,
  };
  fs.writeFileSync(
    path.join(__dirname, "../../tests/e2e/artifacts/dashboard-result.json"),
    JSON.stringify(result, null, 2),
  );
  console.log(JSON.stringify(result));
  await browser.close();
  if (errors.length || failures.length) process.exitCode = 1;
})().catch((e) => {
  console.error(e.message);
  process.exit(1);
});
