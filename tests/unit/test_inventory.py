"""Unit tests use an explicitly injected HTTP transport, never live Steam accounts."""
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.db import Base, utcnow
from app.models.models import InventoryChange, InventoryItem, InventorySnapshot, MarketPrice, SteamAccount
from app.services import inventory


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


@pytest.fixture
def account(db):
    row = SteamAccount(steam_id="76561198000000001", profile_url="https://steamcommunity.com/profiles/76561198000000001")
    db.add(row)
    db.commit()
    return row


def inject_http(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(inventory, "_rate_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(inventory.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))


def page(ids):
    return {"success": 1, "assets": [{"assetid": str(i), "classid": "10", "instanceid": "0", "amount": "1"} for i in ids],
            "descriptions": [{"classid": "10", "instanceid": "0", "name": "Test item", "market_hash_name": "Test item", "marketable": 1, "tradable": 1}]}


@pytest.mark.parametrize(("raw", "expected"), [
    ("$1,234.56 USD", Decimal("1234.56")), ("$0.03", Decimal("0.03")),
    ("0.50", Decimal("0.50")), ("--", None), ("€1,23", None),
    ("$1.234,56", None), ("NaN", None), ("-1.00", None),
    ("$1<script>", None), (None, None),
])
def test_price_parser(raw, expected):
    assert inventory.parse_market_price(raw) == expected


def test_price_rejects_currency_conversion():
    assert inventory.parse_market_price("$1.50", "EUR") is None


def test_complete_snapshots_and_asset_changes(db, account, monkeypatch):
    current = ["100", "101"]
    def handler(request):
        if request.url.path.startswith("/inventory/"):
            return httpx.Response(200, json=page(current))
        return httpx.Response(200, json={"success": True, "lowest_price": "$1.25"})
    inject_http(monkeypatch, handler)
    first = inventory.sync_inventory(db, account.id)
    assert first["total_value"] == 2.5
    assert first["valuation_complete"] is True
    assert not list(db.scalars(select(InventoryChange)))  # baseline isn't a claimed drop
    current[:] = ["101", "102"]
    second = inventory.sync_inventory(db, account.id)
    assert second["total_value"] == 2.5
    changes = list(db.scalars(select(InventoryChange)))
    assert {(row.asset_id, row.change_type) for row in changes} == {("100", "removed"), ("102", "added")}
    assert len(list(db.scalars(select(InventorySnapshot)))) == 2
    assert len(list(db.scalars(select(InventoryItem)))) == 4
    assert account.inventory_visibility == "public"


def test_unknown_prices_remain_unknown(db, account, monkeypatch):
    inject_http(monkeypatch, lambda request: httpx.Response(200, json=(page(["100"]) if "/inventory/" in request.url.path else {"success": False})))
    result = inventory.sync_inventory(db, account.id)
    assert result["total_value"] is None
    assert result["known_listing_value"] == 0
    assert result["unknown_prices"] == 1
    assert account.estimated_value is None
    assert db.scalar(select(InventoryItem)).price is None


def test_private_inventory_retains_last_successful_snapshot(db, account, monkeypatch):
    snapshot = InventorySnapshot(account_id=account.id, status="success", total_value=Decimal("20"))
    db.add(snapshot)
    db.commit()
    inject_http(monkeypatch, lambda request: httpx.Response(403))
    with pytest.raises(inventory.InventoryUnavailable, match="private"):
        inventory.sync_inventory(db, account.id)
    assert account.inventory_visibility == "private"
    assert account.last_sync is None
    assert account.last_error
    rows = list(db.scalars(select(InventorySnapshot)))
    assert {row.status for row in rows} == {"success", "failed"}
    assert db.get(InventorySnapshot, snapshot.id).total_value == Decimal("20")


def test_failed_pagination_does_not_commit_partial_snapshot(db, account, monkeypatch):
    payload = page(["100"])
    payload.update(more_items=1, last_assetid="100")
    inject_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(inventory.InventoryUnavailable, match="did not advance"):
        inventory.sync_inventory(db, account.id)
    assert not list(db.scalars(select(InventoryItem)))
    assert db.scalar(select(InventorySnapshot)).status == "failed"


def test_rate_limit_propagates_server_retry_after(db, monkeypatch):
    cooldowns = []
    inject_http(monkeypatch, lambda request: httpx.Response(429, headers={"Retry-After": "123"}))
    monkeypatch.setattr(inventory, "_cooldown", cooldowns.append)
    with pytest.raises(inventory.InventoryUnavailable) as error:
        inventory.fetch_market_price(db, "Test item")
    assert error.value.retry_after == 123
    assert cooldowns == [123]
    assert not list(db.scalars(select(MarketPrice)))


def test_price_cache_avoids_external_request(db, monkeypatch):
    db.add(MarketPrice(market_hash_name="Test item", currency="USD", price=Decimal("3.11"), fetched_at=utcnow()))
    db.commit()
    def fail(request):
        pytest.fail("cached quote unexpectedly made an external request")
    inject_http(monkeypatch, fail)
    assert inventory.fetch_market_price(db, "Test item") == Decimal("3.11")
