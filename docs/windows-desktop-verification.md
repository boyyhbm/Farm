# Реальная Windows-валидация CS2 Farm Manager

Дата: 2026-11-04. Репозиторий: [boyyhbm/Farm](https://github.com/boyyhbm/Farm).

## Выполненные прогоны Windows CI

Использован GitHub-hosted **Windows Server 2022 x64**, не чистая Windows 11. Коммиты и логи доступны по ссылкам; неуспешные проверки сохранены в истории.

| Прогон | Результат |
|---|---|
| [41752838401](https://github.com/boyyhbm/Farm/actions/runs/41752838401) | 62 Python passed, 5 failed; найдена ошибка unnamed Windows Job Object и небезопасная тестовая фикстура PID |
| [41753334664](https://github.com/boyyhbm/Farm/actions/runs/41753334664) | 65 passed, 2 failed; выявлен относительный PYTHONPATH дочернего движка |
| [41753840294](https://github.com/boyyhbm/Farm/actions/runs/41753840294) | 67 Python passed; сборка остановилась на поиске npm.cmd |
| [41754265846](https://github.com/boyyhbm/Farm/actions/runs/41754265846) | Python passed; сборка выявила потерю кавычек NSIS через cmd-wrapper |
| [41754743612](https://github.com/boyyhbm/Farm/actions/runs/41754743612) | Python passed; native NSIS выявил неверный разделитель пути File /r |

Ошибки исправлялись в коде, после чего workflow запускался повторно. Пройденная компиляция сама по себе не считается пройденной установкой.

## Проверяемый сценарий поставки

Windows workflow собирает настоящие NSIS EXE и выполняет установку, создание ярлыков, запуск установленного Manager с PATH без Python/Node.js/Docker, создание администратора и SQLite, API/Next, запись аккаунта, полный restart, графическую диагностику, замену файлов настоящим установщиком с сравнением SHA-256 всех строк каждой таблицы обеих БД, повторный запуск с сохранённой базой и настоящий деинсталлятор.

Отдельный installed-runtime сценарий запускает две реальные SCM-службы LocalSystem с разными SYSTEM DPAPI credentials на одном CI-хосте. Он проверяет HTTPS/WSS, heartbeat, две параллельные synthetic-сессии, остановку, round robin, UNKNOWN при потере heartbeat, сверку состояния после возобновления, переподключение после перезапуска backend и замену защищённых файлов службы с сохранением Node ID. Конкретные успешные шаги должны подтверждаться JSON/JUnit-файлами финального прогона. Описание сценария не означает, что он уже прошёл.

## Локальные проверки 2026-11-04

- Python: **73 passed, 1 skipped, 21.91s**. Пропущен настоящий Windows SCM-сценарий: Linux не выдаётся за Windows.
- Desktop Node: **13 tests passed**, включая понятные причины ошибок и отсутствие ложного успеха диагностики.
- **8/8** реальных интеграционных проверок Node Supervisor → Python → SQLite → production Next: настройка, запись данных, backup, restart, restore и завершение. [JSON](../apps/desktop/test/results/supervisor-integration.json).
- Chromium: шестишаговый HTML-мастер, очистка пароля, отображение причины/действий при неуспешной диагностике и безопасное текстовое отображение ошибки. Использован явно тестовый IPC, это не Windows/Electron. [JSON](../apps/desktop/test/results/wizard-browser.json).

## Проверки, которые нельзя подменить CI

Чистая Windows 11 без ранее установленных runtime, два физических дополнительных ПК, настоящая перезагрузка ОС, GPU/температурные датчики конкретного оборудования, интерактивный Steam/CS2, диалоги UAC/SmartScreen и визуальное отсутствие консолей требуют [проверки на ваших ПК](WINDOWS-LOCAL-CHECKLIST-RU.md). Установка той же версии поверх себя проверяет сохранность при замене файлов; совместимость миграции с другой версией проверяется отдельно.

Установщики пока не подписаны Authenticode. Канал подписанных обновлений издателя не опубликован; интерфейс сообщает об этом. SmartScreen не отключается. Исторические результаты Linux cross-build и контрольные суммы прежних EXE сохранены [отдельно](windows-cross-build-history-2026-11-03.md).
