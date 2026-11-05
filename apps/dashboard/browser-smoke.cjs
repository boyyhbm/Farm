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
    "Авторизация",
    "Компьютеры",
    "Сессии",
    "Планировщик",
    "Недельные награды",
    "Инвентарь",
    "Рынок",
    "Аналитика",
    "Защита",
    "Журнал аудита",
    "Настройки",
  ]) {
    await page
      .getByRole("navigation")
      .getByRole("button", { name: new RegExp("^" + label + "(?:\\s|$)") })
      .click();
    await page.getByRole("heading", { name: label, exact: true }).waitFor();
    await page.waitForTimeout(150);
    if (label === "Настройки") {
      await page
        .getByRole("heading", { name: "System Management", exact: true })
        .waitFor();
      await page.locator(".system-version").waitFor();
      if ((await page.locator(".system-health-grid article").count()) !== 4)
        throw new Error(
          "System Management did not render four real component checks",
        );
      await page
        .getByRole("button", { name: "Диагностика соединения", exact: true })
        .click();
      await page
        .getByRole("heading", { name: "Результаты диагностики", exact: true })
        .waitFor();
      if ((await page.locator(".system-diagnostics dl > div").count()) === 0)
        throw new Error("Connection diagnostics returned no checks");
    }
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
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Защита", exact: true })
    .click();
  await page.getByRole("heading", { name: "Защита", exact: true }).waitFor();
  const engine = page.getByRole("region", {
    name: "Farm Protection Engine 2.0",
    exact: true,
  });
  await engine
    .getByRole("heading", { name: "Account Health", exact: true })
    .waitFor();
  await engine.getByRole("row").filter({ hasText: "76561199999888881" }).getByRole("button", { name: "Объяснение", exact: true }).click();
  await engine
    .getByRole("heading", { name: "Обоснование состояния", exact: true })
    .waitFor();
  await engine.getByRole("button", { name: "Закрыть", exact: true }).click();
  await engine
    .getByRole("button", { name: "Изменить политику", exact: true })
    .click();
  await page
    .locator("textarea[name=note]")
    .fill("Browser E2E explicit policy review");
  await page.locator("input[name=confirmed]").check();
  await page
    .getByRole("button", { name: "Confirm / Подтвердить", exact: true })
    .click();
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  const reportDownload = page.waitForEvent("download");
  await engine
    .getByRole("button", { name: "CSV", exact: true })
    .first()
    .click();
  const report = await reportDownload;
  await report.saveAs(
    path.join(__dirname, "../../tests/e2e/artifacts/protection-health.csv"),
  );
  if (
    !fs
      .readFileSync(
        path.join(__dirname, "../../tests/e2e/artifacts/protection-health.csv"),
        "utf8",
      )
      .includes("76561199999888881")
  )
    throw new Error(
      "Health report does not contain the real test database account",
    );
  const riskRow = page
    .getByRole("row")
    .filter({ hasText: "76561199999888881" });
  await riskRow.getByRole("button", { name: "История", exact: true }).click();
  await page
    .getByText(
      "Оценка проверяемых технических и операционных событий. Прогноз VAC-бана не выполняется.",
      { exact: true },
    )
    .waitFor();
  await riskRow
    .getByRole("button", { name: "Приостановить", exact: true })
    .click();
  await page
    .locator("textarea[name=note]")
    .fill("E2E explicit operator review");
  await page.locator("input[name=confirmed]").check();
  await page
    .getByRole("button", { name: "Confirm / Подтвердить", exact: true })
    .click();
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  await riskRow.getByText("HIGH", { exact: true }).waitFor();
  await page.screenshot({
    path: path.join(
      __dirname,
      "../../tests/e2e/artifacts/protection-desktop.png",
    ),
    fullPage: true,
  });
  await riskRow
    .getByRole("button", { name: "Снять паузу", exact: true })
    .click();
  await page
    .locator("textarea[name=note]")
    .fill("E2E restore permitted operations");
  await page.locator("input[name=confirmed]").check();
  await page
    .getByRole("button", { name: "Confirm / Подтвердить", exact: true })
    .click();
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  await riskRow.getByText("MEDIUM", { exact: true }).waitFor();
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "Аккаунты", exact: true })
    .click();
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
    sections: 13,
    protection_manual_hold_history_and_release: true,
    protection_v2_health_evidence_policy_and_csv_export: true,
    system_management_status: true,
    system_management_diagnostics: true,
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
