"""Bundled Windows runtime entry point. Commands are invoked only by local Electron IPC.

No user-facing terminal is needed. Password/token input uses stdin, never argv.
This module intentionally imports the application only after isolated config is set.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

# Electron's private pipes always carry UTF-8, including Cyrillic passwords/paths.
# Embedded CPython isolated mode may otherwise use the Windows ANSI code page.
for stream in (sys.stdin, sys.stdout, sys.stderr):
    if stream is not None and hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
if not getattr(sys, "frozen", False):
    sys.path[:0] = [
        str(ROOT),
        str(ROOT.parent / "windows-agent"),
        str(ROOT.parents[1] / "packages/gameplay-test-engine"),
    ]


def data_path(value):
    path = Path(value).resolve()
    path.mkdir(parents=True, exist_ok=True)
    from security.credentials import restrict_path

    restrict_path(path)
    return path


def configure_environment(directory, origin):
    os.environ["DEPLOYMENT_MODE"] = "desktop"
    os.environ["DESKTOP_DATA_DIR"] = str(directory)
    os.environ["DATABASE_URL"] = "sqlite:///" + str(directory / "farm.sqlite3").replace(
        "\\", "/"
    )
    os.environ["PUBLIC_ORIGIN"] = origin
    os.environ["SESSION_SECURE"] = (
        "false"  # local HTTP only; TLS responses upgrade cookie to Secure in middleware
    )
    os.environ["ADMIN_PASSWORD"] = ""
    os.environ["SCHEDULER_MAX_PARALLEL"] = "10"
    secret_file = directory / "notifications.dat"
    if secret_file.exists():
        from security.credentials import read_credentials

        secret = read_credentials(secret_file)
        os.environ["TELEGRAM_BOT_TOKEN"] = secret.get("token", "")
        os.environ["TELEGRAM_CHAT_ID"] = secret.get("chat_id", "")
        os.environ["TELEGRAM_MIN_SEVERITY"] = secret.get("min_severity", "warning")
    else:
        os.environ["TELEGRAM_BOT_TOKEN"] = ""
        os.environ["TELEGRAM_CHAT_ID"] = ""
    os.environ.pop("DESKTOP_NETWORK_ORIGIN", None)
    network = directory / "network.json"
    if network.exists():
        info = json.loads(network.read_text())
        if info.get("enabled"):
            os.environ["DESKTOP_NETWORK_ORIGIN"] = info["url"]


def migrate():
    from alembic import command
    from alembic.config import Config

    base = Path(sys._MEIPASS) / "server" if getattr(sys, "frozen", False) else ROOT
    config = Config(str(base / "alembic.ini"))
    config.set_main_option("script_location", str(base / "migrations"))
    command.upgrade(config, "head")


def initialize(directory, origin, payload):
    if set(payload) - {"username", "password"}:
        raise ValueError("Unexpected setup field")
    username = payload.get("username", "")
    password = payload.get("password", "")
    if not isinstance(username, str) or not 1 <= len(username) <= 100:
        raise ValueError("Administrator username required")
    if not isinstance(password, str) or not 14 <= len(password) <= 1024:
        raise ValueError("Password must contain 14–1024 characters")
    configure_environment(directory, origin)
    os.environ["ADMIN_USERNAME"] = username
    os.environ["ADMIN_PASSWORD"] = password
    migrate()
    from app.core.db import SessionLocal
    from app.core.security import bootstrap
    from app.models.models import User
    from sqlalchemy import select

    with SessionLocal() as db:
        if db.scalar(select(User.id)):
            raise ValueError(
                "Administrator already configured; existing data was preserved"
            )
        bootstrap(db)
    os.environ["ADMIN_PASSWORD"] = ""
    return {
        "status": "configured",
        "database": "sqlite",
        "queue": "sqlite",
        "version": "0.3.0",
    }


def backup(directory, output):
    import sqlite3, zipfile

    output = Path(output).resolve()
    if output.is_relative_to(directory):
        raise ValueError("Choose a backup outside the application data directory")
    if output.exists():
        raise ValueError("Backup destination already exists")
    from security.credentials import restrict_path

    with tempfile.TemporaryDirectory(prefix="farm-backup-") as temporary:
        temporary = Path(temporary)
        for name in ("farm.sqlite3", "coordination.sqlite3"):
            source = directory / name
            if not source.exists():
                if name == "farm.sqlite3":
                    raise ValueError("Application database does not exist")
                continue
            with (
                closing(sqlite3.connect(source)) as src,
                closing(sqlite3.connect(temporary / name)) as dst,
            ):
                src.backup(dst)
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "format": 1,
                        "version": "0.3.0",
                        "kind": "database-only",
                        "includes_credentials": False,
                    }
                ),
            )
            for file in temporary.iterdir():
                archive.write(file, file.name)
        restrict_path(output)
    return {
        "status": "backed_up",
        "path": str(output),
        "network_configuration_included": False,
    }


def restore(directory, source):
    import sqlite3, zipfile, shutil

    source = Path(source).resolve()
    with tempfile.TemporaryDirectory(
        prefix="farm-restore-", dir=directory
    ) as temporary:
        temporary = Path(temporary)
        with zipfile.ZipFile(source) as archive:
            allowed = {"manifest.json", "farm.sqlite3", "coordination.sqlite3"}
            if (
                len(archive.namelist()) != len(set(archive.namelist()))
                or set(archive.namelist()) - allowed
            ):
                raise ValueError("Invalid backup members")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != 1 or manifest.get("kind") != "database-only":
                raise ValueError("Unsupported backup format")
            if sum(i.file_size for i in archive.infolist()) > 2 * 1024**3:
                raise ValueError("Backup exceeds size limit")
            archive.extractall(temporary)
        for name in ("farm.sqlite3", "coordination.sqlite3"):
            file = temporary / name
            if not file.exists():
                if name == "farm.sqlite3":
                    raise ValueError("Backup database missing")
                continue
            with closing(sqlite3.connect(file)) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Backup integrity check failed")
                tables = {
                    r[0]
                    for r in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if name == "farm.sqlite3" and not {
                    "users",
                    "steam_accounts",
                    "alembic_version",
                    "auth_sessions",
                }.issubset(tables):
                    raise ValueError("Not a Farm Manager database")
        # Supervisor stops both components before restore. Keep rollback copies.
        stamp = __import__("time").time_ns()
        rollback = directory / "restore-rollback" / str(stamp)
        rollback.mkdir(parents=True)
        names = ("farm.sqlite3", "coordination.sqlite3")
        existed = {name: (directory / name).exists() for name in names}
        for name in names:
            if existed[name]:
                # Include any surviving WAL from an interrupted process in rollback state.
                with (
                    closing(sqlite3.connect(directory / name)) as src,
                    closing(sqlite3.connect(rollback / name)) as dst,
                ):
                    src.backup(dst)
        try:
            for name in names:
                target = directory / name
                for suffix in ("-wal", "-shm"):
                    target.with_name(name + suffix).unlink(missing_ok=True)
                if (temporary / name).exists():
                    os.replace(temporary / name, target)
                elif name == "coordination.sqlite3":
                    target.unlink(missing_ok=True)
            with closing(sqlite3.connect(directory / "farm.sqlite3")) as db:
                db.execute("DELETE FROM auth_sessions")
                db.commit()
        except BaseException:
            for name in names:
                target = directory / name
                for suffix in ("-wal", "-shm"):
                    target.with_name(name + suffix).unlink(missing_ok=True)
                if existed[name]:
                    shutil.copy2(rollback / name, target)
                else:
                    target.unlink(missing_ok=True)
            raise
    return {
        "status": "restored",
        "rollback_path": str(rollback),
        "restart_required": True,
    }


def network_info(directory):
    file = directory / "network.json"
    info = json.loads(file.read_text()) if file.exists() else {"enabled": False}
    ca = directory / "tls/ca.pem"
    return {
        **info,
        "ca_certificate": ca.read_text() if ca.exists() else None,
        "warning": "Firewall rules are not changed. Allow this application only on a trusted private network using Windows Security.",
    }


def configure_network(directory, payload):
    import ipaddress, socket, secrets
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from security.credentials import write_credentials, read_credentials, restrict_path

    if set(payload) - {"enabled", "bind_host", "advertised_host", "port"}:
        raise ValueError("Unexpected network setting")
    if payload.get("enabled") is not True:
        (directory / "network.json").write_text(json.dumps({"enabled": False}))
        return network_info(directory)
    bind = str(payload.get("bind_host", ""))
    host = str(payload.get("advertised_host", "")).strip()
    port = payload.get("port", 17832)
    ip = ipaddress.ip_address(bind)
    if ip.is_unspecified or ip.is_multicast or ip.is_loopback:
        raise ValueError("Select a specific local network IP address")
    if not isinstance(port, int) or not 1024 <= port <= 65535 or port in (17830, 17831):
        raise ValueError(
            "Choose a TLS port between1024 and65535 other than local service ports"
        )
    if not host or len(host) > 253 or any(c in host for c in "/\\?#@:\r\n "):
        # IPv6 URLs require brackets; use a DNS hostname when advertising IPv6.
        raise ValueError(
            "Use a plain DNS hostname or IPv4 address for the advertised server"
        )
    # Test ownership without opening a listening socket or changing Windows firewall.
    with socket.socket(
        socket.AF_INET6 if ip.version == 6 else socket.AF_INET, socket.SOCK_STREAM
    ) as probe:
        probe.bind((bind, 0))
    tls = directory / "tls"
    tls.mkdir(exist_ok=True)
    restrict_path(tls)
    secrets_file = tls / "secrets.dat"
    if secrets_file.exists():
        password = read_credentials(secrets_file)["key_password"].encode()
        ca_key = serialization.load_pem_private_key(
            (tls / "ca.key").read_bytes(), password
        )
        ca = x509.load_pem_x509_certificate((tls / "ca.pem").read_bytes())
    else:
        password = secrets.token_urlsafe(48).encode()
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "CS2 Farm Manager Local CA")]
        )
        now = datetime.now(timezone.utc)
        ca = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=None,
                    decipher_only=None,
                ),
                critical=True,
            )
            .sign(ca_key, hashes.SHA256())
        )
        (tls / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        (tls / "ca.key").write_bytes(
            ca_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.BestAvailableEncryption(password),
            )
        )
        write_credentials(secrets_file, {"key_password": password.decode()})
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    now = datetime.now(timezone.utc)
    sans = [x509.IPAddress(ip)]
    try:
        sans.append(x509.IPAddress(ipaddress.ip_address(host)))
    except ValueError:
        sans.append(x509.DNSName(host))
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    (tls / "server.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tls / "server.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(password),
        )
    )
    for file in tls.iterdir():
        restrict_path(file)
    (directory / "network.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "bind_host": bind,
                "advertised_host": host,
                "port": port,
                "url": f"https://{host}:{port}",
            }
        )
    )
    restrict_path(directory / "network.json")
    return network_info(directory)


def notifications_info(directory):
    from security.credentials import read_credentials

    path = directory / "notifications.dat"
    data = read_credentials(path) if path.exists() else {}
    return {
        "configured": bool(data.get("token") and data.get("chat_id")),
        "min_severity": data.get("min_severity", "warning"),
    }


def notifications_configure(directory, payload):
    import re
    from security.credentials import write_credentials

    if set(payload) - {"token", "chat_id", "min_severity"}:
        raise ValueError("Unexpected notification setting")
    token = payload.get("token", "")
    chat = payload.get("chat_id", "")
    level = payload.get("min_severity", "warning")
    if not isinstance(token, str) or (
        token and not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", token)
    ):
        raise ValueError("Invalid Telegram bot token format")
    if not isinstance(chat, str) or (
        token and not re.fullmatch(r"-?[0-9]{1,25}", chat)
    ):
        raise ValueError("Telegram chat ID must be numeric")
    if level not in ("info", "warning", "error", "critical"):
        raise ValueError("Invalid notification severity")
    write_credentials(
        directory / "notifications.dat",
        {"token": token, "chat_id": chat, "min_severity": level},
    )
    return notifications_info(directory)


def serve(directory, origin, port):
    configure_environment(directory, origin)
    migrate()
    import uvicorn
    from app.main import app

    info = network_info(directory)

    async def run_servers():
        servers = [
            uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port,
                    log_level="info",
                    timeout_graceful_shutdown=5,
                )
            )
        ]
        if info.get("enabled"):
            from security.credentials import read_credentials

            password = read_credentials(directory / "tls/secrets.dat")["key_password"]
            servers.append(
                uvicorn.Server(
                    uvicorn.Config(
                        app,
                        host=info["bind_host"],
                        port=info["port"],
                        lifespan="off",
                        ssl_certfile=str(directory / "tls/server.pem"),
                        ssl_keyfile=str(directory / "tls/server.key"),
                        ssl_keyfile_password=password,
                        log_level="info",
                        timeout_graceful_shutdown=5,
                    )
                )
            )
        import threading

        loop = asyncio.get_running_loop()

        def request_shutdown():
            for server in servers:
                server.should_exit = True

        def stdin_shutdown():
            try:
                for line in sys.stdin:
                    if line.strip() == "shutdown":
                        break
            finally:
                try:
                    loop.call_soon_threadsafe(request_shutdown)
                except RuntimeError:
                    pass

        if sys.stdin is not None:
            threading.Thread(
                target=stdin_shutdown, name="desktop-shutdown", daemon=True
            ).start()
        tasks = [asyncio.create_task(server.serve()) for server in servers]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*tasks)
        if any(task.exception() for task in done):
            raise RuntimeError("A local server component failed")

    asyncio.run(run_servers())


from contextlib import contextmanager


@contextmanager
def runtime_lock(directory):
    file = (directory / ".runtime.lock").open("a+b")
    try:
        file.seek(0)
        file.write(b"0")
        file.flush()
        file.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError(
                    "Stop server components before this operation; data directory is in use"
                ) from None
        else:
            import fcntl

            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    "Stop server components before this operation; data directory is in use"
                ) from None
        yield
    finally:
        file.close()


def data_check(directory):
    """Read-only integrity and row digests; never export database field values."""
    import hashlib
    import sqlite3

    reports = {}
    for name in ("farm.sqlite3", "coordination.sqlite3"):
        file = directory / name
        if not file.is_file():
            reports[name] = {"status": "missing"}
            continue
        with closing(
            sqlite3.connect(file.as_uri() + "?mode=ro", uri=True, timeout=10)
        ) as db:
            db.execute("PRAGMA query_only=ON")
            integrity = [row[0] for row in db.execute("PRAGMA quick_check")]
            tables = {}
            db.execute("BEGIN")
            for (table,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall():
                quoted = '"' + table.replace('"', '""') + '"'
                # Canonicalize rows independently of insertion/query order.
                rows = sorted(
                    json.dumps(
                        row,
                        default=lambda x: x.hex() if isinstance(x, bytes) else str(x),
                        separators=(",", ":"),
                        ensure_ascii=True,
                    )
                    for row in db.execute("SELECT * FROM " + quoted)
                )
                digest = hashlib.sha256()
                for row in rows:
                    digest.update(row.encode() + b"\n")
                tables[table] = {"rows": len(rows), "sha256": digest.hexdigest()}
            reports[name] = {
                "status": "ok" if integrity == ["ok"] else "failed",
                "integrity": integrity,
                "tables": tables,
            }
    return {
        "databases": reports,
        "ok": reports["farm.sqlite3"]["status"] == "ok"
        and all(v["status"] in {"ok", "missing"} for v in reports.values()),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "data-check",
            "service-host",
            "service-status",
            "service-admin",
            "protect-file",
            "configure",
            "serve",
            "backup",
            "restore",
            "network-configure",
            "network-info",
            "agent-enroll",
            "agent-applications-info",
            "agent-applications-configure",
            "notifications-configure",
            "notifications-info",
            "agent",
            "engine",
        ],
    )
    parser.add_argument("--data-dir")
    parser.add_argument("--origin", default="http://127.0.0.1:17830")
    parser.add_argument("--port", type=int, default=17831)
    parser.add_argument("--output")
    parser.add_argument("--input")
    parser.add_argument("--session")
    parser.add_argument("--config")
    parser.add_argument("--service-name", default="CS2FarmAgent")
    parser.add_argument("--enroll", action="store_true")
    args = parser.parse_args()
    if args.command == "protect-file":
        from security.credentials import restrict_path

        restrict_path(Path(args.input))
        print(json.dumps({"ok": True}))
        return 0
    if args.command in {"service-host", "service-status", "service-admin"}:
        from agent import service_bridge

        if args.command == "service-host":
            try:
                service_bridge.host(args.service_name, args.enroll)
            except Exception as exc:  # noqa: BLE001 - service has no interactive stderr
                _, service_data = service_bridge.paths(args.service_name)
                if service_data.is_dir():
                    (service_data / "service-error.json").write_text(
                        json.dumps(
                            {"error": type(exc).__name__ + ": " + str(exc)[:2000]}
                        ),
                        encoding="utf-8",
                    )
                raise
            return 0
        if args.command == "service-status":
            print(json.dumps(service_bridge.status(args.service_name)))
            return 0
        try:
            request = json.loads(Path(args.input).read_text(encoding="utf-8"))
            Path(args.input).unlink()
            action = request.get("action")
            if action == "install":
                base = (
                    Path(sys.executable).parent
                    if getattr(sys, "frozen", False)
                    else ROOT
                )
                result = service_bridge.install(
                    request.get("payload", {}), base, args.service_name
                )
            elif action == "refresh":
                base = (
                    Path(sys.executable).parent
                    if getattr(sys, "frozen", False)
                    else ROOT
                )
                result = service_bridge.refresh(base, args.service_name)
            elif action == "reset":
                result = service_bridge.reset(args.service_name)
            elif action == "applications-info":
                from agent.desktop import get_applications

                service_bridge._windows(admin=True)
                result = get_applications(service_bridge.paths(args.service_name)[1])
            elif action == "applications":
                result = service_bridge.applications(
                    request.get("payload", {}), args.service_name
                )
            else:
                result = service_bridge.control(action, args.service_name)
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {
                "ok": False,
                "error": type(exc).__name__ + ": " + str(exc)[:2000],
            }
        Path(args.output).write_text(json.dumps(response), encoding="utf-8")
        return 0 if response["ok"] else 1
    if args.command == "engine":
        from gameplay_test_engine.run import main as run_engine

        sys.argv = [sys.argv[0], "--session", args.session, "--config", args.config]
        return asyncio.run(run_engine())
    if not args.data_dir:
        raise ValueError("Data directory required")
    directory = data_path(args.data_dir)
    if args.command == "serve":
        with runtime_lock(directory):
            serve(directory, args.origin, args.port)
        return 0
    if args.command == "agent":
        from agent.desktop import run_desktop

        async def run_agent():
            import threading

            loop = asyncio.get_running_loop()
            task = asyncio.create_task(run_desktop(directory))

            def shutdown_reader():
                try:
                    for line in sys.stdin:
                        if line.strip() == "shutdown":
                            break
                finally:
                    try:
                        loop.call_soon_threadsafe(task.cancel)
                    except RuntimeError:
                        pass

            if sys.stdin is not None:
                threading.Thread(target=shutdown_reader, daemon=True).start()
            try:
                return await task
            except asyncio.CancelledError:
                return 0

        return asyncio.run(run_agent())
    if args.command in (
        "configure",
        "network-configure",
        "agent-enroll",
        "notifications-configure",
        "agent-applications-configure",
    ):
        raw = sys.stdin.read(131073)
        if len(raw) > 131072:
            raise ValueError("Setup request too large")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Setup request must be an object")
        if args.command == "configure":
            result = initialize(directory, args.origin, payload)
        elif args.command == "network-configure":
            result = configure_network(directory, payload)
        elif args.command == "agent-applications-configure":
            from agent.desktop import configure_applications

            result = configure_applications(directory, payload)
        elif args.command == "notifications-configure":
            result = notifications_configure(directory, payload)
        else:
            from agent.desktop import enroll_desktop

            result = asyncio.run(enroll_desktop(directory, payload))
    elif args.command == "agent-applications-info":
        from agent.desktop import get_applications

        result = get_applications(directory)
    elif args.command == "notifications-info":
        result = notifications_info(directory)
    elif args.command == "network-info":
        result = network_info(directory)
    elif args.command == "data-check":
        result = data_check(directory)
    elif args.command == "backup":
        result = backup(directory, args.output)
    elif args.command == "restore":
        with runtime_lock(directory):
            result = restore(directory, args.input)
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    try:
        raise SystemExit(main())
    except Exception as exc:
        # No input, password, token or argv dump in GUI diagnostics.
        print(
            json.dumps({"error": str(exc)[:2000], "type": type(exc).__name__}),
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1)
