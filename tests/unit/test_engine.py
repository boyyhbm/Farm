import asyncio
import pytest
from gameplay_test_engine import SyntheticEngine


@pytest.mark.asyncio
async def test_deterministic_isolated_instances():
    engines = [SyntheticEngine() for _ in range(3)]
    for index, engine in enumerate(engines):
        await engine.initialize({"seed": 42 if index < 2 else 43, "max_steps": 20, "tick_seconds": 0.01})
        await engine.start(f"test-{index}")
    await asyncio.gather(*(engine.task for engine in engines))
    metrics = [await engine.get_metrics() for engine in engines]
    assert all(item["state"] == "COMPLETED" for item in metrics)
    assert metrics[0]["trace_digest"] == metrics[1]["trace_digest"]
    assert metrics[0]["trace_digest"] != metrics[2]["trace_digest"]
    assert metrics[0]["steps"] == 20
    assert all(item["synthetic"] for item in metrics)


@pytest.mark.asyncio
async def test_fsm_pause_resume_and_safe_stop():
    engine = SyntheticEngine()
    with pytest.raises(ValueError):
        await engine.start("bad")
    await engine.initialize({"tick_seconds": 0.01})
    await engine.start("pause-test")
    await asyncio.sleep(0.04)
    await engine.pause()
    steps = engine.steps
    await asyncio.sleep(0.04)
    assert engine.steps == steps
    await engine.resume()
    await asyncio.sleep(0.04)
    assert engine.steps > steps
    await engine.stop()
    await engine.stop()
    assert (await engine.get_state())["state"] == "STOPPED"
    with pytest.raises(ValueError):
        await engine.resume()


@pytest.mark.asyncio
async def test_engine_rejects_remote_commands_and_invalid_configuration():
    engine = SyntheticEngine()
    with pytest.raises(ValueError):
        await engine.initialize({"executable": "cmd.exe"})
    with pytest.raises(ValueError):
        await engine.initialize({"tick_seconds": 0})
