"""Low disk packaging retry from a completed unpacked Manager; no fake executable creation.

Shares immutable Electron/runtime files via hard links while NSIS writes full,
independent installers. Product role metadata is replaced on its own inode.
"""

import json, os, shutil, subprocess, sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
manager = root / "artifacts/windows/manager/win-unpacked"
agent = root / "artifacts/windows/agent/win-unpacked"
if not (manager / "CS2-Farm-Manager.exe").is_file():
    raise SystemExit("Completed real Manager payload required")
runtime = manager / "resources/runtime/farm-runtime"
shutil.copy2(root / "apps/api/desktop_entry.py", runtime / "desktop_entry.py")
shutil.copy2(root / "apps/api/app/main.py", runtime / "app/main.py")
if agent.exists():
    shutil.rmtree(agent)
agent.mkdir(parents=True)
for path in manager.iterdir():
    if path.name == "resources":
        continue
    target = agent / (
        "CS2-Farm-Agent.exe" if path.name == "CS2-Farm-Manager.exe" else path.name
    )
    if path.is_dir():
        shutil.copytree(path, target, copy_function=os.link)
    else:
        os.link(path, target)
resources = agent / "resources"
resources.mkdir()
os.link(manager / "resources/app.asar", resources / "app.asar")
shutil.copytree(runtime, resources / "runtime/farm-runtime", copy_function=os.link)
(resources / "product.json").write_text(
    json.dumps(
        {
            "defaultMode": "agent",
            "agentOnly": True,
            "productName": "CS2 Farm Agent",
            "version": "0.2.0",
        }
    )
)
# Only regenerable payload/cache duplicates are removed; source and actual EXEs stay.
shutil.rmtree(root / "build/windows/stage", ignore_errors=True)
shutil.rmtree(Path.home() / ".cache/electron", ignore_errors=True)
for product in ("agent", "manager"):
    subprocess.run(
        ["node", str(root / "build/windows/compile-installer.cjs"), product],
        cwd=root,
        check=True,
    )
    if product == "agent":
        destination = manager / "resources/downloads/CS2-Farm-Agent-Setup.exe"
        destination.unlink(missing_ok=True)
        os.link(root / "artifacts/windows/agent/CS2-Farm-Agent-Setup.exe", destination)
        shutil.rmtree(agent)
print("Both genuine NSIS installers compiled from complete real Windows payloads")
