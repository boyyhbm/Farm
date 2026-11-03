# CS2 Farm Manager

Центральная панель управления собственными Steam/CS2-узлами: Next.js, FastAPI, PostgreSQL, Redis/Celery и Python Windows Agent. Панель не получает XP и награды автоматически и не взаимодействует с VAC. Официальный клиент Steam требует ручной авторизации владельца.

## Быстрый запуск на Linux

Требуются Docker Engine с Compose v2, Bash, Python 3, свободные порты 80/443. Из каталога проекта выполните одну команду:

```sh
./scripts/bootstrap.sh
```

Скрипт создаст случайные секреты в `.env` с правами `0600`, соберёт приложения, применит миграции и поднимет сервер. Панель: **https://localhost**. Начальный пользователь — `admin`, пароль находится в защищённом `.env` (`ADMIN_PASSWORD`). Для localhost требуется доверить корневой сертификат Caddy; для домена задайте `SITE_ADDRESS`, `PUBLIC_ORIGIN`, `TLS_EMAIL` по [инструкции развёртывания](docs/deployment.md). Не выключайте проверку TLS у агентов.

`.env.example` описывает настройки, но не содержит готовых секретов. Не публикуйте `.env`, токены подключения агентов, резервные копии и пароли. Данные PostgreSQL, Redis и сертификаты переживают обычные перезапуски в Docker volumes.

## Первые действия

1. Авторизуйтесь и добавьте SteamID64 в Accounts либо импортируйте CSV с колонками `steam_id,notes,tags`.
2. Создайте одноразовый токен в Nodes и установите Windows Agent на каждый ПК по [инструкции](docs/windows-agent.md).
3. Дождитесь реального heartbeat; аппаратные метрики появляются только после подключения узла.
4. Создайте разрешённое задание в Sessions/Scheduler. Авторизация Steam выполняется вручную на ПК. Один обычный Windows-сеанс не становится несколькими одновременными Steam-пользователями.
5. Синхронизируйте публичный инвентарь. Приватность, HTTP 429 и сетевые ошибки выводятся как ошибки, а не пустой «успешный» результат.
6. Для учёта Weekly Care Package задайте период с проверенным источником и подтвердите награду вручную. Обнаружение предмета не подтверждает недельный дроп.

Все таблицы в новой установке пусты. Недоступные показатели отображаются как неизвестные. Test Engine — отдельная воспроизводимая симуляция без подключения к CS2 или Steam; её показатели не являются метриками игры.

## Исходный код

- `apps/dashboard` — Next.js/TypeScript, Tailwind, TanStack Query, Zustand, Recharts.
- `apps/api` — FastAPI, модели SQLAlchemy, Alembic, планировщик и Celery.
- `apps/windows-agent` — агент, локальный список разрешённых приложений, служба и установщик Windows.
- `packages/gameplay-test-engine` — изолированный конечный автомат тестовых сценариев.
- `infrastructure` — Dockerfiles, Caddy HTTPS, инструкции мониторинга.
- `tests` — автоматические проверки и [измеряемая нагрузочная проба](tests/load/README.md). Результаты реальных замеров: [infrastructure-verification.md](docs/infrastructure-verification.md).

API и протокол агента описаны в [контракте](docs/API-CONTRACT.md), интерактивный OpenAPI доступен по `/docs`. [Безопасность](docs/security.md), [резервное копирование и восстановление](docs/deployment.md), ограничения и фактические результаты проверки приведены в документации проекта. Число записей аккаунтов не определяет число одновременно работающих клиентов CS2.

## Разработка и проверки

Серверная часть использует Python 3.12+; интерфейс — Node.js 24. Для локальных PostgreSQL/Redis:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --wait postgres redis
```

В `.env` должны быть созданы реальные локальные секреты. Для запуска API вне Docker задайте `DATABASE_URL` с адресом `127.0.0.1`, `REDIS_URL=redis://127.0.0.1:6379/0`, `SESSION_SECURE=false` и точный `PUBLIC_ORIGIN` локальной панели. Не применяйте эти настройки на публичном сервере.

```sh
python3 -m venv .venv
.venv/bin/pip install -r apps/api/requirements.txt
cd apps/api
../../.venv/bin/alembic upgrade head
../../.venv/bin/uvicorn app.main:app --reload
```

В отдельном терминале выполните `npm ci && npm run dev` в `apps/dashboard`. Celery worker и beat запускаются из `apps/api` командами из `docker-compose.yml`; без worker задания и синхронизация останутся в очереди. Следуйте актуальному отчёту о тестировании для точных команд, зависимостей и проверенных сценариев. Для Windows-процессов необходима отдельная проверка на физических Windows 10/11; Linux не подтверждает совместимость рабочего стола или службы.

## Результаты и ограничения

Фактические результаты: [49 автоматических тестов, 8 сквозных проверок двух агентов и браузерный прогон](docs/test-results.md). [Нагрузочная проба реестра](docs/infrastructure-verification.md) выполнена отдельно на 1000 и 5000 записях. [Известные ограничения](docs/known-limitations.md) включают отсутствие физического Windows-стенда, непроверенный полный Compose в ограниченной среде и необходимость ручной авторизации Steam. Приложение не объявляется полностью проверенной промышленной системой.

Для разработки с уже созданным `.env` и контейнерными PostgreSQL/Redis доступны помощники, которые читают секреты без вывода:

```sh
.venv/bin/python scripts/local_api.py migrate
.venv/bin/python scripts/local_api.py
# В отдельных терминалах:
.venv/bin/python scripts/local_worker.py worker
.venv/bin/python scripts/local_worker.py beat
npm run dev --prefix apps/dashboard
```

Локальные помощники разрешают HTTP только для разработки панели на `http://localhost:3000`; production Compose сохраняет Secure cookies и HTTPS. Периодическая синхронизация инвентарей идёт отдельной очередью, с ограничением 20 постановок в минуту и интервалом `INVENTORY_SYNC_INTERVAL_SECONDS` (по умолчанию 6 часов).
