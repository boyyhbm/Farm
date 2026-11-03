"""Transparent, single-currency cash accounting; Steam Wallet is never cash revenue."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.models.models import (
    Expense,
    Revenue,
    SteamAccount,
    InventorySnapshot,
    DropRecord,
)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def analytics_overview(db: Session) -> dict:
    expenses = list(db.scalars(select(Expense).where(Expense.currency == "USD")))
    revenues = list(db.scalars(select(Revenue).where(Revenue.currency == "USD")))
    accounts = list(
        db.scalars(select(SteamAccount).where(SteamAccount.archived.is_(False)))
    )
    expense_total = sum((_decimal(row.amount) for row in expenses), Decimal(0))
    revenue_total = sum((_decimal(row.amount) for row in revenues), Decimal(0))
    fee_total = sum((_decimal(row.fees) for row in revenues), Decimal(0))
    realized = revenue_total - fee_total - expense_total
    confirmed = list(
        db.scalars(select(DropRecord).where(DropRecord.status == "CONFIRMED"))
    )
    valued_drops = [
        row for row in confirmed if row.value is not None and row.currency == "USD"
    ]
    average_drop_value = (
        sum((_decimal(row.value) for row in valued_drops), Decimal(0)) / len(confirmed)
        if confirmed and len(valued_drops) == len(confirmed)
        else None
    )
    known_accounts = [row for row in accounts if row.estimated_value is not None]
    known_value = sum(
        (_decimal(row.estimated_value) for row in known_accounts), Decimal(0)
    )
    inventory_value = known_value if len(known_accounts) == len(accounts) else None
    series: dict[str, dict] = defaultdict(
        lambda: {"expenses": Decimal(0), "revenue": Decimal(0)}
    )
    for row in expenses:
        series[row.occurred_at.date().isoformat()]["expenses"] += _decimal(row.amount)
    for row in revenues:
        series[row.occurred_at.date().isoformat()]["revenue"] += _decimal(
            row.amount
        ) - _decimal(row.fees)
    return {
        "base_currency": "USD",
        "accounts": len(accounts),
        "prime_accounts": sum(row.prime_status == "confirmed" for row in accounts),
        "confirmed_drops": len(confirmed),
        "average_drop_value": float(average_drop_value)
        if average_drop_value is not None
        else None,
        "confirmed_drops_without_valuation": len(confirmed) - len(valued_drops),
        "expenses": float(expense_total),
        "revenue": float(revenue_total),
        "fees": float(fee_total),
        "realized_profit": float(realized),
        "inventory_value": float(inventory_value)
        if inventory_value is not None
        else None,
        # Cost basis per asset is not recorded; unrealized profit cannot be inferred.
        "unrealized_value": float(inventory_value)
        if inventory_value is not None
        else None,
        "known_inventory_listing_value": float(known_value),
        "accounts_without_valuation": len(accounts) - len(known_accounts),
        "roi": float(realized / expense_total * 100) if expense_total > 0 else None,
        # No stable prospective cashflow model is configured; do not invent a forecast.
        "payback_months": None,
        "series": [
            {
                "date": day,
                "expenses": float(values["expenses"]),
                "revenue": float(values["revenue"]),
            }
            for day, values in sorted(series.items())
        ],
        "formulas": {
            "realized_profit": "cash_revenue - sale_fees - recorded_expenses",
            "roi": "100 * realized_profit / recorded_expenses; null when expenses = 0",
            "inventory_value": "sum of complete available inventory valuations, before selling fees; null if incomplete",
            "unrealized_value": "estimated listing value of unsold inventory, not profit and not withdrawable cash",
            "payback_months": "not calculated without a configured prospective net cashflow model",
            "series.revenue": "recorded cash revenue net of sale fees",
        },
    }


def portfolio_value_history(db: Session, days: int = 90) -> list[dict]:
    """Daily closing valuation of the current account registry, carried forward.

    Window queries select at most one successful snapshot per account per day,
    plus each account's pre-window baseline. Repeated syncs never double count.
    Missing valuations propagate as null, rather than silently counting as zero.
    Days before any inventory observation are omitted; no data is fabricated.
    """
    days = max(1, min(days, 366))
    today = utcnow().date()
    start_day = today - timedelta(days=days - 1)
    start = datetime.combine(start_day, datetime.min.time(), timezone.utc)
    accounts = list(
        db.scalars(select(SteamAccount).where(SteamAccount.archived.is_(False)))
    )
    if not accounts:
        return []
    base_query = (
        select(
            InventorySnapshot.account_id.label("account_id"),
            InventorySnapshot.fetched_at.label("fetched_at"),
            InventorySnapshot.total_value.label("total_value"),
        )
        .join(SteamAccount, SteamAccount.id == InventorySnapshot.account_id)
        .where(
            SteamAccount.archived.is_(False),
            InventorySnapshot.status == "success",
        )
    )
    before = (
        base_query.add_columns(
            func.row_number()
            .over(
                partition_by=InventorySnapshot.account_id,
                order_by=(
                    InventorySnapshot.fetched_at.desc(),
                    InventorySnapshot.id.desc(),
                ),
            )
            .label("position")
        )
        .where(InventorySnapshot.fetched_at < start)
        .subquery()
    )
    recent = (
        base_query.add_columns(
            func.row_number()
            .over(
                partition_by=(
                    InventorySnapshot.account_id,
                    func.date(InventorySnapshot.fetched_at),
                ),
                order_by=(
                    InventorySnapshot.fetched_at.desc(),
                    InventorySnapshot.id.desc(),
                ),
            )
            .label("position")
        )
        .where(InventorySnapshot.fetched_at >= start)
        .subquery()
    )
    baseline = db.execute(
        select(before.c.account_id, before.c.total_value).where(before.c.position == 1)
    ).all()
    updates = db.execute(
        select(recent.c.account_id, recent.c.fetched_at, recent.c.total_value)
        .where(recent.c.position == 1)
        .order_by(recent.c.fetched_at)
    ).all()
    latest = {account_id: value for account_id, value in baseline}
    by_day: dict = defaultdict(list)
    for account_id, fetched_at, value in updates:
        by_day[fetched_at.date()].append((account_id, value))
    if not latest and not by_day:
        return []
    first_day = start_day if latest else min(by_day)
    history = []
    day = first_day
    while day <= today:
        for account_id, value in by_day.get(day, []):
            latest[account_id] = value
        existing = [row for row in accounts if row.created_at.date() <= day]
        values = [latest.get(row.id) for row in existing]
        complete = bool(existing) and all(value is not None for value in values)
        total = (
            sum((_decimal(value) for value in values), Decimal(0)) if complete else None
        )
        history.append(
            {
                "date": day.isoformat(),
                "value": float(total) if total is not None else None,
            }
        )
        day += timedelta(days=1)
    return history
