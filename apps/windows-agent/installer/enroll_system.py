"""LocalSystem one-time enrollment helper launched by protected installer task."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.main import run

config = Path(sys.argv[1])
token_file = config.parent / "enroll.once"
try:
    token = token_file.read_text(encoding="utf-8-sig").strip()
finally:
    token_file.unlink(missing_ok=True)
raise SystemExit(asyncio.run(run(str(config), token)))
