# CS2 Farm Manager dashboard

Next.js 16 + React 19, TypeScript, Tailwind CSS 4, shadcn-style Radix components, Zustand, TanStack Query, Recharts. Backend contract: [API-CONTRACT](../../docs/API-CONTRACT.md).

## Windows: пользовательский запуск

Откройте установленный **CS2 Farm Manager** через ярлык рабочего стола или меню «Пуск». Мастер первого запуска предлагает главный компьютер, дополнительный узел или подключение к существующему HTTPS-серверу. Учётная запись администратора создаётся в графическом интерфейсе; пользователю не нужны Python, Node.js, Docker или терминал.

В **Settings → System Management** панель показывает реальные результаты проверки API, базы и очереди. Кнопка «Открыть обслуживание» в локальной desktop-оболочке открывает её системное окно для резервного копирования, восстановления, перезапуска, просмотра ошибок и обновлений. Браузерная версия сохраняется и не получает доступ к локальным файлам или процессам.

В **Nodes** скачайте поставляемый сервером **CS2-Farm-Agent-Setup.exe**, запустите графический мастер на дополнительном ПК и введите адрес доверенного HTTPS-сервера и одноразовый код из панели. Если EXE не включён в конкретную поставку сервера, панель явно сообщает об этом и не выдаёт другой файл за установщик. `localhost` основного компьютера не является адресом для другого ПК. Сеть и брандмауэр автоматически не открываются.

Фактические результаты сборки Windows и ограничения подписи описаны в [документации Windows-поставки](../../docs/windows-desktop-build.md). Наличие исходного кода не означает, что установщик прошёл проверку на физической Windows.

## Разработка интерфейса

Этот раздел предназначен разработчикам; пользователю desktop-поставки выполнять команды не требуется.

При работающем API на `http://127.0.0.1:8000`, `PUBLIC_ORIGIN=http://localhost:3000` и `SESSION_SECURE=false` только для локальной разработки:

```sh
npm ci --prefix apps/dashboard
npm run dev --prefix apps/dashboard
```

Сборка и проверки:

```sh
npm run typecheck --prefix apps/dashboard
npm run build --prefix apps/dashboard
npm audit --prefix apps/dashboard --omit=dev
npm run start --prefix apps/dashboard
```

`API_URL` выбирает upstream при сборке Next или запуске development-сервера. Desktop-сборка использует `http://127.0.0.1:17831`; Next слушает `127.0.0.1:17830`. Переписываются `/api/*`, `/ws/*` и `/health/ready`. Серверный Compose сохраняет Caddy и same-origin маршрутизацию. `NEXT_PUBLIC_WS_URL` может выбрать другой разрешённый API сервером Origin; по умолчанию используется текущий Origin.

Не запускайте `next build` и `next dev` одновременно с одной `.next`. Сборка создаёт standalone-сервер для контейнера или desktop-оболочки. Интерфейс не подставляет демонстрационные аккаунты или метрики и не принимает пароли Steam.

## Browser smoke test

Run against an isolated local test database: this test creates, edits and deletes one explicitly synthetic Steam-account record (`76561199999888881`). It leaves the corresponding audit history, makes no Steam request and never starts CS2. Existing records using that identifier cause a visible test failure.

With API and dashboard running:

```sh
npm run test:e2e --prefix apps/dashboard
```

Credentials are read from the root `.env`, or overridden through `E2E_USERNAME` / `E2E_PASSWORD`. `E2E_BASE_URL` defaults to `http://localhost:3000`. The test uses `/usr/bin/chromium` when available; set `E2E_BROWSER_PATH` to another installed Chromium or install Playwright Chromium using `cd apps/dashboard && npx playwright install chromium`. Credentials are never printed.

Actual smoke coverage: authenticated browser → same-origin Next proxy → FastAPI → PostgreSQL account creation/edit/deletion → updated table; all 11 sections load; mobile navigation works; uncaught browser exceptions and unsuccessful API responses are recorded. Artifacts are written to `tests/e2e/artifacts/dashboard-result.json`, `dashboard-desktop.png`, and `dashboard-mobile.png`.

## Operations

- Accounts: search, Prime/node/archive filters, tags, groups, bulk edit, CSV import/export. SteamID64 is immutable; archive can be restored in Edit.
- Nodes: generate single-use enrollment tokens, real heartbeat/CPU/RAM telemetry, diagnostics/history, restart and revoke access.
- Sessions and scheduler: confirmed start/stop/restart/cancel/retry, pause/resume queue, priority/FIFO/round-robin policy, repeating schedules and resource requirements. Real session status comes from the backend. Bulk starts retain idempotency keys across partial failures.
- Drops: manually register source-backed seven-day periods and confirm rewards with evidence. Newly detected items do not confirm a reward.
- Inventory and market: public-inventory synchronization queue, snapshots, asset changes, local search and CSV export. Unknown prices remain unavailable.
- Analytics: actual expense/sale entry, fees, realized profit, ROI, portfolio estimates, and CSV reports. USD base currency; no invented exchange rates or Steam Wallet cash balance.
- Settings: server configuration, notification delivery history and administrator-created users/roles. Telegram secrets remain on the server.

Display copy supports Russian and English. Technical status codes remain in their canonical form. The React UI uses cached queries and reconnecting WebSockets; when WebSocket access is unavailable, periodic HTTP refresh remains active.
