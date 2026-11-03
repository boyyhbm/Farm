# Изолированный Gameplay Test Engine

Это синтетический навигационный сценарий на квадратной сетке. Он не подключается
к CS2/Steam, не отправляет игровые пакеты, не читает память других процессов,
не эмулирует ввод и не получает XP или награды. Его назначение — воспроизводимая
проверка планировщика, состояний сеанса, остановки, мониторинга и ошибок.

`GameplayEngine` задаёт асинхронный интерфейс `initialize`, `start`, `get_state`,
`pause`, `resume`, `stop`, `get_metrics`. `SyntheticEngine` реализует конечный автомат
`NEW → READY → RUNNING ↔ PAUSED → COMPLETED/STOPPED/FAILED`. Недопустимые переходы
возвращают `ValueError`; повторные stop/pause/resume в достигнутом состоянии
безопасны. Прерывание stop будит приостановленный экземпляр и ожидает его выхода.

Параметры:

| Параметр | Значение по умолчанию | Допустимые значения |
|---|---:|---|
| seed | 1 | целое число |
| duration_seconds | 60 | 0.1..86400 |
| tick_seconds | 0.1 | 0.01..10 |
| max_steps | 100000 | 1..1000000 |
| grid_size | 20 | 2..1000 |

Экземпляр делает случайный выбор из четырёх направлений с отдельным генератором
`random.Random(seed)`. Результат зависит от seed и количества шагов, а не от
планирования asyncio. Метрики содержат seed, steps, position, collisions,
trace_digest и обязательный флаг `synthetic: true`. История ограничена последними
2048 позициями; trace_digest относится к этому ограниченному фрагменту.

В панели создайте Job вида `test`: сервер назначит его доступному узлу, агент
запустит отдельный Python-процесс. Несколько заданий на разных узлах работают
параллельно. Сам движок поддерживает много независимых экземпляров; агент также
имеет `test_capacity`. В текущем серверном MVP консервативная резервация — один
сеанс любого вида на узел, поэтому несколько test jobs на одном узле выполняются
последовательно. Параллелизм движка не означает параллельные Steam-аккаунты в одном
интерактивном Windows-сеансе.

Локальный запуск после установки Python-пакета:

```bash
.venv/bin/python -m gameplay_test_engine.run \
  --session local-1 \
  --config '{"seed":42,"duration_seconds":2}'
```

Использование нескольких экземпляров напрямую:

```python
import asyncio
from gameplay_test_engine import SyntheticEngine

async def run():
    engines = [SyntheticEngine() for _ in range(3)]
    for index, engine in enumerate(engines):
        await engine.initialize({"seed": 42, "max_steps": 20, "tick_seconds": 0.01})
        await engine.start(f"isolated-{index}")
    await asyncio.gather(*(engine.task for engine in engines))
    return [await engine.get_metrics() for engine in engines]

print(asyncio.run(run()))
```

Все три trace_digest совпадут. Изменение seed меняет сценарий; состояние одного
экземпляра не доступно другому. Тесты выполняются командой:

```bash
.venv/bin/python -m pytest -q tests/unit/test_engine.py tests/unit/test_agent.py
```
