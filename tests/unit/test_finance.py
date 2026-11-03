from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.db import Base, utcnow
from app.models.models import Expense, Revenue, SteamAccount
from app.services.finance import analytics_overview
from app.services import notifications


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def test_cash_profit_and_roi_use_recorded_fees_and_expenses(db):
    db.add_all([
        Expense(category="equipment", amount=Decimal("200.00"), currency="USD", occurred_at=utcnow()),
        Expense(category="electricity", amount=Decimal("10.25"), currency="USD", occurred_at=utcnow()),
        Revenue(amount=Decimal("300.50"), fees=Decimal("30.05"), currency="USD", occurred_at=utcnow()),
        SteamAccount(steam_id="76561198000000001", profile_url="https://steamcommunity.com", estimated_value=Decimal("50.03")),
    ])
    db.commit()
    result = analytics_overview(db)
    assert result["expenses"] == 210.25
    assert result["revenue"] == 300.50
    assert result["fees"] == 30.05
    assert result["realized_profit"] == 60.20
    assert result["roi"] == pytest.approx(60.20 / 210.25 * 100)
    assert result["inventory_value"] == 50.03
    assert result["unrealized_value"] == 50.03
    assert result["payback_months"] is None
    assert result["series"][0]["revenue"] == 270.45


def test_unknown_inventory_is_not_zero_and_does_not_inflate_cash_profit(db):
    db.add_all([
        SteamAccount(steam_id="76561198000000001", profile_url="https://steamcommunity.com", estimated_value=Decimal("100")),
        SteamAccount(steam_id="76561198000000002", profile_url="https://steamcommunity.com", estimated_value=None),
        SteamAccount(steam_id="76561198000000003", profile_url="https://steamcommunity.com", estimated_value=Decimal("200"), archived=True),
    ])
    db.commit()
    result = analytics_overview(db)
    assert result["realized_profit"] == 0
    assert result["roi"] is None
    assert result["inventory_value"] is None
    assert result["known_inventory_listing_value"] == 100
    assert result["accounts_without_valuation"] == 1


def test_notification_redacts_secrets_and_filters_severity(db, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:very-secret")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret-admin-pass")
    monkeypatch.setenv("TELEGRAM_MIN_SEVERITY", "warning")
    row = notifications.notify(db, "test", "password=hello Bearer abc.123 secret-admin-pass 123:very-secret", "info")
    assert row.status == "skipped"
    assert "hello" not in row.message
    assert "abc.123" not in row.message
    assert "secret-admin-pass" not in row.message
    assert "123:very-secret" not in row.message


def test_notification_without_configuration_does_not_send(db, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    row = notifications.notify(db, "session_failed", "Session failed", "error")
    result = notifications.deliver_notifications(db)
    assert result == {"configured": False, "sent": 0, "failed": 0}
    assert row.status == "pending"


def test_notification_delivery_uses_plain_text_and_does_not_leak_request_url(db, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:very-secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "101")
    sent_requests = []
    real_client = httpx.Client
    def handler(request):
        sent_requests.append(request)
        return httpx.Response(500)
    monkeypatch.setattr(notifications.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    row = notifications.notify(db, "test", "<b>hello</b>", "critical")
    result = notifications.deliver_notifications(db)
    assert result["failed"] == 1
    assert "123:very-secret" not in row.error
    assert row.status in {"failed", "pending"}
    assert b"parse_mode" not in sent_requests[0].content


def test_notification_telemetry_is_deduplicated_for_ten_minutes(db):
    first = notifications.notify(db, "high_ram", "High RAM on node A", "warning")
    second = notifications.notify(db, "high_ram", "High RAM on node A", "warning")
    different = notifications.notify(db, "high_ram", "High RAM on node B", "warning")
    assert second.id == first.id
    assert different.id != first.id


def test_portfolio_history_uses_latest_snapshot_per_account_and_carries_forward(db):
    from datetime import timedelta
    from app.models.models import InventorySnapshot
    from app.services.finance import portfolio_value_history
    now = utcnow()
    a = SteamAccount(steam_id="76561198000000001", profile_url="https://steamcommunity.com", created_at=now-timedelta(days=5))
    b = SteamAccount(steam_id="76561198000000002", profile_url="https://steamcommunity.com", created_at=now-timedelta(days=5))
    db.add_all([a,b]); db.flush()
    yesterday = now - timedelta(days=1)
    db.add_all([
        InventorySnapshot(account_id=a.id, fetched_at=yesterday.replace(hour=1), total_value=Decimal("10"), status="success"),
        InventorySnapshot(account_id=a.id, fetched_at=yesterday.replace(hour=2), total_value=Decimal("15"), status="success"),
        InventorySnapshot(account_id=b.id, fetched_at=yesterday.replace(hour=1), total_value=Decimal("20"), status="success"),
        InventorySnapshot(account_id=a.id, fetched_at=now, total_value=Decimal("16"), status="success"),
    ])
    db.commit()
    history = portfolio_value_history(db, days=5)
    assert history == [{"date": yesterday.date().isoformat(), "value": 35.0}, {"date": now.date().isoformat(), "value": 36.0}]


def test_portfolio_history_unknown_prices_and_missing_account_valuation_are_null(db):
    from datetime import timedelta
    from app.models.models import InventorySnapshot
    from app.services.finance import portfolio_value_history
    now = utcnow()
    a = SteamAccount(steam_id="76561198000000001", profile_url="https://steamcommunity.com", created_at=now-timedelta(days=5))
    b = SteamAccount(steam_id="76561198000000002", profile_url="https://steamcommunity.com", created_at=now-timedelta(days=5))
    db.add_all([a,b]); db.flush()
    db.add(InventorySnapshot(account_id=a.id, fetched_at=now-timedelta(days=3), total_value=Decimal("10"), status="success"))
    db.add(InventorySnapshot(account_id=b.id, fetched_at=now, total_value=None, status="success"))
    db.commit()
    assert all(row["value"] is None for row in portfolio_value_history(db))


def test_notification_retries_are_bounded_and_have_backoff(db, monkeypatch):
    from datetime import timedelta
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:very-secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "101")
    real_client = httpx.Client
    monkeypatch.setattr(notifications.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs))
    row = notifications.notify(db, "test_retry", "Delivery failure", "critical")
    for attempt in range(1, 6):
        row.next_attempt_at = utcnow() - timedelta(seconds=1)
        db.commit()
        result = notifications.deliver_notifications(db)
        assert result["failed"] == 1
        assert row.attempts == attempt
        assert row.next_attempt_at > utcnow()
        assert row.status == ("failed" if attempt == 5 else "pending")
    assert notifications.deliver_notifications(db)["failed"] == 0
