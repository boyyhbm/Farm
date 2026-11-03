# Фактический отчёт проверки

Дата: 2026-11-03. Linux x86_64, Python 3.12.14, Node.js 24.22.1, PostgreSQL 17.11, Redis 7. Исходный код MVP находится в этом репозитории; не все требования промышленной эксплуатации подтверждены физическим стендом.

## Автоматические тесты

Последний запуск `./scripts/test.sh`: **49 passed, 1 warning in 6.90s**. Предупреждение Starlette сообщает о будущем переходе TestClient с httpx на httpx2; ошибок тестов нет. Для интеграционных тестов скрипт создаёт отдельную БД `farm_test`, использует Redis DB14 и очищает только эти тестовые данные. Основная БД не очищается. Нужны работающие PostgreSQL/Redis и `.venv` с зависимостями.

Покрыты CSV импорт/поиск/экспорт/bulk update, SQL-запросы с недоверенным поиском, CSRF/RBAC, одноразовая регистрация, отзыв агента, идемпотентность команд, одновременные диспетчеры PostgreSQL, резервирование узла, последовательная очередь, тайм-аут без преждевременного освобождения узла, ограниченные повторы и ручной retry, restart, UNKNOWN/reconciliation, аварийная остановка, расписания, drop evidence, финансы, ошибки Steam/429/private, неполные цены, snapshot diff, история портфеля, Telegram outbox/backoff/redaction, журнал агента, реальное создание/остановка подпроцесса, гонка stop/telemetry, подпись обновления и ZIP traversal. Тесты Steam HTTP и Telegram используют явно внедрённый тестовый transport, а не реальную интеграцию с аккаунтом.

## Сквозная проверка реальных агентов

Команда при работающих локальных API, worker и beat:

```sh
PYTHONPATH=apps/api:apps/windows-agent:packages/gameplay-test-engine .venv/bin/python tests/integration/run_agent_integration.py
```

Последний отчёт: [agent-report.json](../tests/integration/agent-report.json). **8/8 проверок за 91.83s** на финальных обновлённых зависимостях. Два отдельных настоящих процесса агента на одном Linux-host передавали psutil-телеметрию и управляли настоящими дочерними процессами синтетического тестового движка. Проверены pause, параллельность двух узлов/третье задание в очереди, последовательное завершение, cancel, потеря heartbeat через SIGSTOP, UNKNOWN через 49.05s, SIGCONT/reconnect и последующая остановка. После прогона процессы остановлены и тестовые credentials отозваны.

Это **не** два физических Windows-ПК и не запуск Steam/CS2. Первый отладочный запуск без worker остался в очереди и завершился тайм-аутом; после запуска настоящих Celery worker+beat два последующих полных прогона прошли. Ошибку не подменяли положительным результатом.

## Браузер и сборка

`npm run typecheck --prefix apps/dashboard` и `npm run build --prefix apps/dashboard` — успешны. Chromium smoke на production standalone сервере и реальном API/PostgreSQL проверил все **11 разделов**, вход, создание/редактирование/удаление аккаунта, мобильную навигацию. `pageErrors: []`, `apiFailures: []`.

Повтор: `npm run test:e2e --prefix apps/dashboard`. [JSON результата](../tests/e2e/artifacts/dashboard-result.json), [desktop](../tests/e2e/artifacts/dashboard-desktop.png), [mobile](../tests/e2e/artifacts/dashboard-mobile.png). Запускать на тестовой установке: тест создаёт и удаляет явно тестовую запись, аудит сохраняется.

## Зависимости и статические проверки

- `pip-audit -r apps/api/requirements.txt`: **No known vulnerabilities found** после обновления FastAPI/Starlette, cryptography, python-multipart и pytest. Первоначальный аудит нашёл известные уязвимости; версии исправлены, полные тесты повторены.
- `npm audit --omit=dev --prefix apps/dashboard`: **0 vulnerabilities**.
- `ruff check apps/api/app --select E9,F63,F7,F82`: успешно; Python compileall успешно.
- Python transitive зависимости закреплены с хешами в `apps/api/requirements.lock`; Dockerfile использует этот lock. Frontend использует `npm ci` и `package-lock.json`.

Аудит баз известных уязвимостей не является penetration test и не гарантирует отсутствие неизвестных проблем.

## Инфраструктура и нагрузка

См. [infrastructure-verification.md](infrastructure-verification.md) и [load-results.json](load-results.json): Compose/Caddy/Bash checks, работающие PostgreSQL/Redis, CSV импорт 1000/5000, 200/200 чтений при concurrency8, реальный backup/restore 5000 записей. API Docker image собран до последних обновлений; итоговые Dockerfiles требуют повторной сборки на целевом сервере. Полный Compose в этой 8GB/VFS среде не запущен из-за ENOSPC. Локальные приложения и Celery проверены с контейнерными БД/Redis.

Список непроверенных и ограниченных возможностей: [known-limitations.md](known-limitations.md). Production, реальная Windows-служба, интерактивный Steam и публичный инвентарь конкретного аккаунта не объявляются проверенными.
