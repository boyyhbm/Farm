"""Build genuine Windows NSIS installers with an embedded Windows CPython runtime.

Maintainer tooling, never an end-user requirement. Runs on Linux or Windows;
Windows installation/runtime verification remains a separate CI test.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "build/windows/stage"
OUT = ROOT / "artifacts/windows"


def run(args, env=None):
    argv = [str(a) for a in args]
    # Windows CreateProcess does not search PATHEXT for npm.cmd.
    argv[0] = shutil.which(argv[0]) or argv[0]
    subprocess.run(argv, cwd=ROOT, env=env, check=True)


def download(url, target, sha256):
    target = Path(target)
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == sha256:
        return
    context = ssl.create_default_context()
    with (
        urllib.request.urlopen(url, context=context, timeout=120) as response,
        target.open("wb") as output,
    ):
        shutil.copyfileobj(response, output)
    if hashlib.sha256(target.read_bytes()).hexdigest() != sha256:
        target.unlink(missing_ok=True)
        raise RuntimeError("Component checksum verification failed")


def copy_sources(destination):
    ignored = shutil.ignore_patterns(
        "__pycache__", "*.pyc", "*.egg-info", ".pytest_cache"
    )
    shutil.copytree(ROOT / "apps/api/app", destination / "app", ignore=ignored)
    shutil.copytree(
        ROOT / "apps/api/migrations", destination / "migrations", ignore=ignored
    )
    shutil.copy2(ROOT / "apps/api/alembic.ini", destination / "alembic.ini")
    shutil.copy2(ROOT / "apps/api/desktop_entry.py", destination / "desktop_entry.py")
    for package in ("agent", "collectors", "process_manager", "security", "transport"):
        shutil.copytree(
            ROOT / "apps/windows-agent" / package, destination / package, ignore=ignored
        )
    shutil.copytree(
        ROOT / "packages/gameplay-test-engine/gameplay_test_engine",
        destination / "gameplay_test_engine",
        ignore=ignored,
    )


def stage_runtime():
    manifest = json.loads((ROOT / "build/windows/python-runtime.json").read_text())
    runtime = STAGE / "runtime/farm-runtime"
    if runtime.exists():
        shutil.rmtree(runtime)
    runtime.parent.mkdir(parents=True, exist_ok=True)
    local = STAGE / "foreign-python" / manifest["key"]
    if not local.exists():
        run(
            [
                "uv",
                "python",
                "install",
                manifest["key"],
                "--install-dir",
                STAGE / "foreign-python",
                "--no-bin",
                "--no-registry",
                "--system-certs",
            ]
        )
    if (
        hashlib.sha256((local / "python.exe").read_bytes()).hexdigest()
        != manifest["python_exe_sha256"]
    ):
        raise RuntimeError("Windows interpreter identity mismatch")
    shutil.copytree(local, runtime, ignore_dangling_symlinks=True)
    if (runtime / "python.exe").read_bytes()[:2] != b"MZ":
        raise RuntimeError("Windows Python PE executable missing")
    # Wheel hashes are enforced by pip; binaries are downloaded for Windows, never Linux.
    wheels = STAGE / "windows-wheels"
    wheels.mkdir(exist_ok=True)
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--require-hashes",
            "-r",
            ROOT / "build/windows/requirements.lock",
            "--dest",
            wheels,
            "--only-binary=:all:",
            "--platform",
            "win_amd64",
            "--python-version",
            "312",
            "--implementation",
            "cp",
            "--abi",
            "cp312",
            "--progress-bar",
            "off",
        ]
    )
    packages = runtime / "Lib/site-packages"
    packages.mkdir(parents=True, exist_ok=True)
    for wheel in wheels.glob("*.whl"):
        if wheel.name.lower().startswith(
            ("uv-", "pyinstaller-", "pyinstaller_hooks_contrib-")
        ):
            continue
        with zipfile.ZipFile(wheel) as z:
            for info in z.infolist():
                name = Path(info.filename)
                if name.is_absolute() or ".." in name.parts:
                    raise RuntimeError("Unsafe wheel member")
                parts = name.parts
                if parts and parts[0].endswith(".data"):
                    if len(parts) > 2 and parts[1] in ("purelib", "platlib"):
                        name = Path(*parts[2:])
                    else:
                        continue
                target = packages / name
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(z.read(info))
    copy_sources(runtime)
    (runtime / "python312._pth").write_text(
        "python312.zip\n.\nLib\nDLLs\nLib/site-packages\nimport site\n"
    )
    # No developer headers, launchers, Tcl UI or Python stdlib tests are needed.
    for name in (
        "include",
        "libs",
        "tcl",
        "Scripts",
        "Lib/test",
        "Lib/idlelib",
        "Lib/tkinter",
        "Lib/ensurepip",
    ):
        shutil.rmtree(runtime / name, ignore_errors=True)
    return manifest


def windows_dashboard_addons(dashboard):
    import base64

    lock = json.loads((ROOT / "apps/dashboard/package-lock.json").read_text())
    names = {
        "node_modules/@img/sharp-win32-x64",
        "node_modules/@img/sharp-libvips-win32-x64",
    }
    for name, info in lock["packages"].items():
        if name not in names:
            continue
        with urllib.request.urlopen(info["resolved"], timeout=120) as response:
            raw = response.read()
        algorithm, encoded = info["integrity"].split("-", 1)
        if algorithm != "sha512" or hashlib.sha512(raw).digest() != base64.b64decode(
            encoded
        ):
            raise RuntimeError("Windows Node addon integrity mismatch")
        with tempfile.TemporaryDirectory(dir=STAGE) as temporary:
            archive = Path(temporary) / "addon.tgz"
            archive.write_bytes(raw)
            with tarfile.open(archive) as tar:
                tar.extractall(temporary, filter="data")
            target = dashboard / name
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(Path(temporary) / "package", target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-dashboard-build", action="store_true")
    parser.add_argument(
        "--reuse-stage",
        action="store_true",
        help="Reuse already verified staged payload during a packaging retry",
    )
    parser.add_argument("--product", choices=["all", "agent", "manager"], default="all")
    args = parser.parse_args()
    if os.getenv("CSC_LINK"):
        raise RuntimeError(
            "Portable cross-build emits unsigned installers. Choose the frozen native Windows workflow variant for Authenticode-signed releases."
        )
    STAGE.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    environment = {
        **os.environ,
        "API_URL": "http://127.0.0.1:17831",
        "NEXT_TELEMETRY_DISABLED": "1",
    }
    if not args.skip_dashboard_build:
        run(["npm", "run", "build", "--prefix", "apps/dashboard"], environment)
    python = (
        json.loads((ROOT / "build/windows/python-runtime.json").read_text())
        if args.reuse_stage
        else stage_runtime()
    )
    dashboard = STAGE / "dashboard"
    if not args.reuse_stage:
        if dashboard.exists():
            shutil.rmtree(dashboard)
        shutil.copytree(ROOT / "apps/dashboard/.next/standalone", dashboard)
        if sys.platform != "win32":
            windows_dashboard_addons(dashboard)
        node = json.loads((ROOT / "build/windows/node-runtime.json").read_text())
        archive = STAGE / "node-download.zip"
        download(node["url"], archive, node["sha256"])
        node_stage = STAGE / "node"
        node_stage.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive) as z:
            for member in z.namelist():
                if member.endswith(("/node.exe", "/LICENSE")):
                    (node_stage / Path(member).name).write_bytes(z.read(member))
        archive.unlink()
    else:
        node = json.loads((ROOT / "build/windows/node-runtime.json").read_text())
        if (
            not (STAGE / "runtime/farm-runtime/python.exe").exists()
            or not (dashboard / "server.js").exists()
        ):
            raise RuntimeError("Verified staging payload is missing")
    config = STAGE / "cross-builder.cjs"
    configuration = (
        "const c=require("
        + json.dumps(str(ROOT / "build/windows/electron-builder.cjs"))
        + ");"
    )
    if sys.platform != "win32":
        configuration += (
            "c.win.signAndEditExecutable=false;c.win.verifyUpdateCodeSignature=false;"
        )
    config.write_text(configuration + "module.exports=c;")
    downloads = STAGE / "downloads"
    downloads.mkdir(exist_ok=True)
    # Agent never embeds a prior Manager or itself; Manager embeds the actual completed Agent EXE.
    products = ("agent", "manager") if args.product == "all" else (args.product,)
    if "agent" in products:
        for file in downloads.glob("*"):
            file.unlink()
    for product in products:
        (STAGE / "product.json").write_text(
            json.dumps(
                {
                    "defaultMode": "agent" if product == "agent" else "server",
                    "agentOnly": product == "agent",
                    "productName": "CS2 Farm Agent"
                    if product == "agent"
                    else "CS2 Farm Manager",
                    "version": "0.3.0",
                }
            )
        )
        env = {**environment, "FARM_PRODUCT": product}
        if sys.platform != "win32":
            env["CSC_IDENTITY_AUTO_DISCOVERY"] = "false"
        run(
            [
                "node",
                ROOT / "apps/desktop/node_modules/electron-builder/out/cli/cli.js",
                "--win",
                "dir",
                "--x64",
                "--config",
                config,
                "--publish",
                "never",
            ],
            env,
        )
        run(["node", ROOT / "build/windows/compile-installer.cjs", product], env)
        name = (
            "CS2-Farm-Agent-Setup.exe"
            if product == "agent"
            else "CS2-Farm-Manager-Setup.exe"
        )
        file = OUT / product / name
        if not file.exists() or file.read_bytes()[:2] != b"MZ":
            raise RuntimeError("Actual NSIS PE installer not generated")
        if product == "agent":
            shutil.copy2(file, downloads / name)
        # Preserve installers; discard only reproducible unpacked trees to bound disk use.
        shutil.rmtree(OUT / product / "win-unpacked", ignore_errors=True)
    manifest = {
        "runtime": "embedded-windows-cpython",
        "host_platform": sys.platform,
        "native_windows_tested": False,
        "python": python,
        "node": node,
        "artifacts": {},
    }
    for product, name in (
        ("agent", "CS2-Farm-Agent-Setup.exe"),
        ("manager", "CS2-Farm-Manager-Setup.exe"),
    ):
        file = OUT / product / name
        manifest["artifacts"][name] = {
            "size_bytes": file.stat().st_size,
            "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
        }
    (OUT / "build-manifest.json").write_text(json.dumps(manifest, indent=2))
    print("Genuine NSIS installers generated; Windows execution remains unverified.")


if __name__ == "__main__":
    main()
