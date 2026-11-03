from __future__ import annotations
import asyncio
import logging
import random
import ssl
from urllib.parse import urlparse, urlunparse
import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

log = logging.getLogger(__name__)


def tls_context(config):
    context = ssl.create_default_context()
    if getattr(config, "ca_file", None):
        context.load_verify_locations(cafile=config.ca_file)
    return context


async def enroll(config, token, hardware):
    import platform
    import socket
    from agent import __version__
    async with httpx.AsyncClient(timeout=30, verify=tls_context(config)) as client:
        response = await client.post(f"{config.server_url}/api/v1/nodes/enroll", json={
            "token": token, "hostname": socket.gethostname(), "os_version": platform.platform(),
            "hardware": hardware, "agent_version": __version__})
        if response.status_code >= 400:
            # Never include request body/token in diagnostic logs.
            raise RuntimeError(f"Enrollment failed: HTTP {response.status_code}")
        result = response.json()
        if not result.get("node_id") or not result.get("credential"):
            raise RuntimeError("Invalid enrollment response")
        return {"node_id": result["node_id"], "credential": result["credential"]}


def websocket_url(config):
    parsed = urlparse(config.server_url)
    return urlunparse(("wss" if parsed.scheme == "https" else "ws", parsed.netloc,
                       parsed.path.rstrip("/") + "/ws/agent", "", "", ""))


async def reconnecting(config, credentials, callback, stop_event):
    delay = 1.0
    while not stop_event.is_set():
        try:
            async with connect(websocket_url(config), additional_headers={"Authorization": f"Bearer {credentials['credential']}"},
                               open_timeout=20, ping_interval=20, ping_timeout=20, max_size=256 * 1024, **({"ssl": tls_context(config)} if config.server_url.startswith("https://") else {})) as websocket:
                log.info("Authenticated agent transport connected")
                delay = 1.0
                await callback(websocket)
        except InvalidStatus as exc:
            status = exc.response.status_code
            log.error("Agent connection rejected with HTTP %s", status)
            if status in {401, 403}:
                raise RuntimeError("Agent credential rejected or revoked; administrator enrollment required") from None
        except ConnectionClosed as exc:
            log.warning("Agent transport closed: code %s", exc.code)
            if exc.code in {4401, 4403, 1008}:
                raise RuntimeError("Agent credential rejected or revoked") from None
        except (OSError, TimeoutError) as exc:
            log.warning("Agent transport unavailable: %s", type(exc).__name__)
        if stop_event.is_set():
            break
        try:
            await asyncio.wait_for(stop_event.wait(), delay + random.uniform(0, min(delay / 4, 3)))
        except TimeoutError:
            pass
        delay = min(delay * 2, 60)
