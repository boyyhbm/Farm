"""CLI subprocess for a single isolated simulation."""
import argparse
import asyncio
import json
import signal
from .engine import SyntheticEngine


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    engine = SyntheticEngine()
    await engine.initialize(json.loads(args.config))
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, lambda: asyncio.create_task(engine.stop()))
        except (NotImplementedError, ValueError):
            pass  # Windows process group stop is handled by agent's process manager.
    await engine.start(args.session)
    await engine.task
    print(json.dumps(await engine.get_metrics()), flush=True)
    return 1 if engine.error else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
