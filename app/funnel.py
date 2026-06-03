"""
GET /stores/{store_id}/funnel

Conversion funnel:  Entry → Zone Visit → Billing Queue → Purchase
Each step shows the distinct visitor count and drop-off % from the previous step.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import distinct, func
from sqlalchemy.orm import Session

from app.database import EventORM, POSTransactionORM, get_db
from app.metrics import _get_converted_visitors

router = APIRouter(prefix="/stores", tags=["Analytics"])

# Response schema

class FunnelStep(BaseModel):
    step: str
    count: int
    drop_off_pct: float  # % dropped relative to the previous step (0.0 for first step)


class FunnelResponse(BaseModel):
    store_id: str
    window_hours: int
    correlation_window_minutes: int
    funnel: list[FunnelStep]


# GET /stores/{store_id}/funnel

@router.get("/{store_id}/funnel", response_model=FunnelResponse)
def get_funnel(
    store_id: str,
    window_hours: int = Query(default=24, ge=1, le=168),
    db: Session = Depends(get_db),
) -> FunnelResponse:
    """
    Retail conversion funnel for a store.

    Purchase correlation rule: a visitor who had a BILLING_QUEUE_JOIN or
    ZONE_ENTER event within 5 minutes before any POS transaction timestamp
    counts as a converted visitor.

    Rules:
    - is_staff=True events are excluded.
    - Each step uses DISTINCT visitor_id so re-entries don't inflate counts.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)
    CORRELATION_WINDOW_MINUTES = 5

    def _distinct_visitors(*event_types: str) -> int:
        return (
            db.query(func.count(distinct(EventORM.visitor_id)))
            .filter(
                EventORM.store_id == store_id,
                EventORM.is_staff.is_(False),
                EventORM.timestamp >= since,
                EventORM.event_type.in_(list(event_types)),
            )
            .scalar()
            or 0
        )

    def _drop_off(current: int, previous: int) -> float:
        if previous == 0:
            return 0.0
        return round((1 - current / previous) * 100, 1)

    # Step 1: Entry
    entries = _distinct_visitors("ENTRY", "REENTRY")

    # Step 2: Zone Visit
    zone_visitors = _distinct_visitors("ZONE_ENTER")

    # Step 3: Billing Queue Join
    billing_visitors = _distinct_visitors("BILLING_QUEUE_JOIN")

    # Step 4: Purchase (POS correlation)
    converted = _get_converted_visitors(
        db, store_id, since, now, CORRELATION_WINDOW_MINUTES
    )
    purchases = len(converted)

    funnel: list[FunnelStep] = [
        FunnelStep(step="Entry", count=entries, drop_off_pct=0.0),
        FunnelStep(
            step="Zone Visit",
            count=zone_visitors,
            drop_off_pct=_drop_off(zone_visitors, entries),
        ),
        FunnelStep(
            step="Billing Queue",
            count=billing_visitors,
            drop_off_pct=_drop_off(billing_visitors, zone_visitors),
        ),
        FunnelStep(
            step="Purchase",
            count=purchases,
            drop_off_pct=_drop_off(purchases, billing_visitors),
        ),
    ]

    return FunnelResponse(
        store_id=store_id,
        window_hours=window_hours,
        correlation_window_minutes=CORRELATION_WINDOW_MINUTES,
        funnel=funnel,
    )
