"""Steam's public inventory and Community Market adapters.

No authenticated Steam session, proxies, CAPTCHA solving or hidden data sources.
Only successful, complete fetches become inventory snapshots. Partial valuations
remain null, while known prices retain their provenance and timestamp.
"""

from __future__ import annotations

import os
import re
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.config import settings
from app.models.models import (
    InventoryChange,
    InventoryItem,
    InventorySnapshot,
    MarketPrice,
    SteamAccount,
)


class InventoryUnavailable(RuntimeError):
    def __init__(
        self, message: str, *, retry_after: int = 60, visibility: str | None = None
    ):
        super().__init__(message)
        self.retry_after = retry_after
        self.visibility = visibility


def _rate_limit(bucket: str = "inventory", interval: float | None = None) -> None:
    """Fail closed when distributed throttling is unavailable.

    A single Redis key spaces requests across workers. A 429 adds a server-directed
    cooldown in addition to normal spacing. Sleep is bounded; Celery retries long
    cooldowns rather than tying up a worker.
    """
    import redis

    spacing = (
        interval
        if interval is not None
        else float(os.getenv("STEAM_REQUEST_INTERVAL", "2"))
    )
    try:
        client = redis.Redis.from_url(settings().redis_url, socket_timeout=3)
        cooldown = int(client.ttl("steam:cooldown") or 0)
        if cooldown > 0:
            raise InventoryUnavailable(
                "Steam request cooldown active", retry_after=cooldown
            )
        script = """
        local t = redis.call('TIME')
        local now = tonumber(t[1]) * 1000 + tonumber(t[2]) / 1000
        local key = KEYS[1]
        local next = tonumber(redis.call('GET', key) or '0')
        if next > now then return math.ceil(next - now) end
        redis.call('PSETEX', key, ARGV[1], now + tonumber(ARGV[1]))
        return 0
        """
        milliseconds = max(250, int(spacing * 1000))
        wait_ms = int(client.eval(script, 1, f"steam:rate:{bucket}", milliseconds))
        if wait_ms > 0:
            time.sleep(min(wait_ms / 1000, 5))
            # Reacquire: sleeping alone doesn't reserve a distributed slot.
            if int(client.eval(script, 1, f"steam:rate:{bucket}", milliseconds)) > 0:
                raise InventoryUnavailable(
                    "Steam request queue busy", retry_after=max(1, int(spacing))
                )
    except InventoryUnavailable:
        raise
    except redis.RedisError as exc:
        raise InventoryUnavailable(
            "Steam synchronization paused: distributed rate limiter unavailable"
        ) from exc


def _cooldown(seconds: int) -> None:
    import redis

    try:
        redis.Redis.from_url(settings().redis_url, socket_timeout=3).set(
            "steam:cooldown", "1", ex=min(max(seconds, 1), 86400)
        )
    except redis.RedisError:
        # The next request fails closed in _rate_limit if Redis is still down.
        pass


def _get_json(client: httpx.Client, url: str, params: dict[str, Any]) -> dict[str, Any]:
    _rate_limit()
    try:
        response = client.get(url, params=params)
        if response.status_code == 429:
            raw_retry = response.headers.get("retry-after", "60")
            retry = min(86400, max(1, int(raw_retry))) if raw_retry.isdigit() else 60
            _cooldown(retry)
            raise InventoryUnavailable(
                "Steam rate limit reached (HTTP 429)", retry_after=retry
            )
        if response.status_code in (401, 403):
            raise InventoryUnavailable(
                "Steam inventory is private or access was denied", visibility="private"
            )
        if response.status_code >= 400:
            raise InventoryUnavailable(
                f"Steam temporarily unavailable (HTTP {response.status_code})"
            )
        data = response.json()
        if not isinstance(data, dict):
            raise InventoryUnavailable("Steam returned an unexpected response")
        return data
    except InventoryUnavailable:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise InventoryUnavailable(
            f"Steam request failed: {type(exc).__name__}"
        ) from exc


def parse_market_price(raw: Any, currency: str = "USD") -> Decimal | None:
    """Parse only an explicitly supported currency; never infer FX conversion."""
    if currency != "USD" or not isinstance(raw, str):
        return None
    match = re.fullmatch(
        r"\s*\$?\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*(?:USD)?\s*", raw
    )
    if not match:
        return None
    try:
        value = Decimal(match.group(1).replace(",", ""))
        return value if value.is_finite() and value >= 0 else None
    except InvalidOperation:
        return None


def fetch_market_price(db: Session, name: str, currency: str = "USD") -> Decimal | None:
    if currency != "USD":
        raise ValueError(
            "Only USD prices are supported; currency conversion is not configured"
        )
    if not name or len(name) > 512:
        raise ValueError("Invalid market item name")
    cached = db.scalar(
        select(MarketPrice)
        .where(
            MarketPrice.market_hash_name == name,
            MarketPrice.currency == currency,
            MarketPrice.fetched_at >= utcnow() - timedelta(hours=6),
        )
        .order_by(MarketPrice.fetched_at.desc())
        .limit(1)
    )
    if cached:
        return Decimal(str(cached.price)) if cached.price is not None else None
    with httpx.Client(
        timeout=20, follow_redirects=False, headers={"User-Agent": "CS2FarmManager/1.0"}
    ) as client:
        data = _get_json(
            client,
            "https://steamcommunity.com/market/priceoverview/",
            {
                "appid": 730,
                "currency": 1,
                "market_hash_name": name,
            },
        )
    if not data.get("success"):
        return None
    price = parse_market_price(data.get("lowest_price"), currency)
    if price is None:
        return None
    db.add(
        MarketPrice(
            market_hash_name=name,
            currency=currency,
            price=price,
            fetched_at=utcnow(),
            source="steam_community_market_lowest_listing",
        )
    )
    db.flush()
    return price


def _fetch_inventory(steam_id: str) -> tuple[dict[str, dict], dict[str, dict]]:
    if not re.fullmatch(r"[0-9]{17}", steam_id):
        raise ValueError("SteamID64 must contain 17 digits")
    assets: dict[str, dict] = {}
    descriptions: dict[str, dict] = {}
    cursor: str | None = None
    cursors: set[str] = set()
    with httpx.Client(
        timeout=30, follow_redirects=False, headers={"User-Agent": "CS2FarmManager/1.0"}
    ) as client:
        for _ in range(100):
            params: dict[str, Any] = {"l": "english", "count": 2000}
            if cursor:
                params["start_assetid"] = cursor
            page = _get_json(
                client, f"https://steamcommunity.com/inventory/{steam_id}/730/2", params
            )
            if page.get("success") != 1:
                raise InventoryUnavailable(
                    "Steam inventory could not be read; it may be private or temporarily unavailable"
                )
            raw_assets, raw_descriptions = (
                page.get("assets", []),
                page.get("descriptions", []),
            )
            if not isinstance(raw_assets, list) or not isinstance(
                raw_descriptions, list
            ):
                raise InventoryUnavailable("Malformed Steam inventory page")
            for asset in raw_assets:
                if (
                    not isinstance(asset, dict)
                    or not str(asset.get("assetid", "")).isdigit()
                ):
                    raise InventoryUnavailable("Malformed Steam inventory asset")
                try:
                    amount = int(asset.get("amount", 1))
                except (ValueError, TypeError) as exc:
                    raise InventoryUnavailable("Invalid Steam asset amount") from exc
                if amount <= 0 or amount > 2_147_483_647:
                    raise InventoryUnavailable("Invalid Steam asset amount")
                assets[str(asset["assetid"])] = asset
            for desc in raw_descriptions:
                if not isinstance(desc, dict):
                    raise InventoryUnavailable("Malformed Steam inventory description")
                descriptions[
                    f"{desc.get('classid', '')}:{desc.get('instanceid', '0')}"
                ] = desc
            if not page.get("more_items"):
                return assets, descriptions
            cursor = str(page.get("last_assetid", ""))
            if not cursor.isdigit() or cursor in cursors:
                raise InventoryUnavailable("Steam inventory pagination did not advance")
            cursors.add(cursor)
    raise InventoryUnavailable("Steam inventory pagination exceeded safety limit")


def sync_inventory(db: Session, account_id: str) -> dict[str, Any]:
    account = db.scalar(
        select(SteamAccount).where(SteamAccount.id == account_id).with_for_update()
    )
    if not account:
        raise ValueError("Account not found")
    try:
        assets, descriptions = _fetch_inventory(account.steam_id)
    except InventoryUnavailable as exc:
        account.last_error = str(exc)
        if exc.visibility:
            account.inventory_visibility = exc.visibility
        db.add(
            InventorySnapshot(
                account_id=account.id,
                fetched_at=utcnow(),
                total_value=None,
                status="failed",
            )
        )
        db.commit()
        raise

    previous = db.scalar(
        select(InventorySnapshot)
        .where(
            InventorySnapshot.account_id == account.id,
            InventorySnapshot.status == "success",
        )
        .order_by(InventorySnapshot.fetched_at.desc())
        .limit(1)
    )
    old_items = (
        list(
            db.scalars(
                select(InventoryItem).where(InventoryItem.snapshot_id == previous.id)
            )
        )
        if previous
        else []
    )
    old = {item.asset_id: item for item in old_items}
    snapshot = InventorySnapshot(
        account_id=account.id, fetched_at=utcnow(), total_value=None, status="success"
    )
    db.add(snapshot)
    db.flush()
    priced_value = Decimal("0")
    unknown_prices = 0
    names: dict[str, Decimal | None] = {}
    max_quotes = max(0, min(100, int(os.getenv("INVENTORY_MAX_QUOTES_PER_SYNC", "20"))))
    requested_quotes = 0
    for asset_id, asset in assets.items():
        desc = descriptions.get(
            f"{asset.get('classid', '')}:{asset.get('instanceid', '0')}", {}
        )
        name = str(desc.get("name") or "Unknown item")[:500]
        market_name = str(desc.get("market_hash_name") or "")[:500]
        marketable = bool(desc.get("marketable", 0))
        price: Decimal | None = None
        if marketable and market_name:
            if market_name in names:
                price = names[market_name]
            else:
                cached = db.scalar(
                    select(MarketPrice)
                    .where(
                        MarketPrice.market_hash_name == market_name,
                        MarketPrice.currency == "USD",
                        MarketPrice.fetched_at >= utcnow() - timedelta(hours=6),
                    )
                    .order_by(MarketPrice.fetched_at.desc())
                    .limit(1)
                )
                if cached:
                    price = (
                        Decimal(str(cached.price)) if cached.price is not None else None
                    )
                elif requested_quotes < max_quotes:
                    requested_quotes += 1
                    try:
                        price = fetch_market_price(db, market_name)
                    except InventoryUnavailable:
                        # Inventory remains a valid complete snapshot even when pricing is unavailable.
                        price = None
                names[market_name] = price
        if price is None:
            unknown_prices += 1
        else:
            priced_value += price * int(asset.get("amount", 1))
        item = InventoryItem(
            snapshot_id=snapshot.id,
            asset_id=asset_id,
            class_id=str(asset.get("classid", "")),
            name=name,
            market_hash_name=market_name,
            amount=int(asset.get("amount", 1)),
            tradable=bool(desc.get("tradable", 0)),
            marketable=marketable,
            price=price,
        )
        db.add(item)
        if previous and asset_id not in old:
            db.add(
                InventoryChange(
                    account_id=account.id,
                    snapshot_id=snapshot.id,
                    asset_id=asset_id,
                    change_type="added",
                    name=name,
                    detected_at=utcnow(),
                )
            )
        elif previous and int(asset.get("amount", 1)) != old[asset_id].amount:
            db.add(
                InventoryChange(
                    account_id=account.id,
                    snapshot_id=snapshot.id,
                    asset_id=asset_id,
                    change_type="amount_changed",
                    name=name,
                    detected_at=utcnow(),
                )
            )
    if previous:
        for asset_id, item in old.items():
            if asset_id not in assets:
                db.add(
                    InventoryChange(
                        account_id=account.id,
                        snapshot_id=snapshot.id,
                        asset_id=asset_id,
                        change_type="removed",
                        name=item.name,
                        detected_at=utcnow(),
                    )
                )
    snapshot.total_value = priced_value if unknown_prices == 0 else None
    account.estimated_value = snapshot.total_value
    account.inventory_visibility = "public"
    account.last_sync = utcnow()
    account.last_error = None
    db.commit()
    return {
        "snapshot_id": str(snapshot.id),
        "items": len(assets),
        "unknown_prices": unknown_prices,
        "valuation_complete": unknown_prices == 0,
        "known_listing_value": float(priced_value),
        "total_value": float(snapshot.total_value)
        if snapshot.total_value is not None
        else None,
        "currency": "USD",
        "price_basis": "lowest_listing_before_fees",
    }
