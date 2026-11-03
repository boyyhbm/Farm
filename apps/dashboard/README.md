# CS2 Farm Manager dashboard

Next.js 16 + React 19, TypeScript, Tailwind CSS 4, shadcn-style Radix components, Zustand, TanStack Query, Recharts. Backend contract: [API-CONTRACT](../../docs/API-CONTRACT.md).

## Local run

From the repository root, after starting the API on `http://127.0.0.1:8000` with `PUBLIC_ORIGIN=http://localhost:3000` and `SESSION_SECURE=false` for local HTTP:

```sh
npm ci --prefix apps/dashboard
npm run dev --prefix apps/dashboard
```

Open `http://localhost:3000`. Sign in using the initial administrator configured on the API. No seeded accounts or metrics are supplied by the UI. Empty and unavailable information is shown explicitly. Steam passwords and API secrets are never accepted by this client.

`API_URL` selects the API upstream when Next configuration is built or the development server starts. Production Compose uses Caddy to forward `/api/*` and `/ws/*` directly to the API, retaining same-origin cookies and WebSockets. `NEXT_PUBLIC_WS_URL` may select a different WebSocket origin if that origin is explicitly permitted by the API. Default: current origin.

## Production and checks

```sh
npm run typecheck --prefix apps/dashboard
npm run build --prefix apps/dashboard
npm audit --prefix apps/dashboard --omit=dev
npm run start --prefix apps/dashboard
```

Do not run `next build` and `next dev` against the same `.next` directory concurrently. The build emits standalone server files consumed by the dashboard Dockerfile.

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
