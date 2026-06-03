"""
GET /stores/{store_id}/anomalies

Detects and returns active operational anomalies:
  1. QUEUE_SPIKE      — billing queue depth above threshold
  2. CONVERSION_DROP  — today's rate > 20% below 7-day average
  3. DEAD_ZONE        — a zone with no visitor activity for > 4 hours
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import distinct, func
from sqlalchemy.orm import Session

from app.database import EventORM, POSTransactionORM, get_db
from app.metrics import _get_converted_visitors

router = APIRouter(prefix="/stores", tags=["Analytics"])

# Constants

QUEUE_WARN_THRESHOLD = 10
QUEUE_CRITICAL_THRESHOLD = 20
DEAD_ZONE_HOURS = 4
CONVERSION_DROP_WARN = 0.20    # 20 % below 7-day avg
CONVERSION_DROP_CRITICAL = 0.40

# Response schema

class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class Anomaly(BaseModel):
    anomaly_type: str
    severity: Severity
    description: str
    suggested_action: str
    detected_at: datetime
    value: Optional[float] = None
    threshold: Optional[float] = None


class AnomaliesResponse(BaseModel):
    store_id: str
    checked_at: datetime
    active_anomalies: list[Anomaly]


# Internal: conversion rate helper

def _conversion_rate(
    db: Session,
    store_id: str,
    since: datetime,
    until: datetime,
) -> float:
    entries = (
        db.query(func.count(distinct(EventORM.visitor_id)))
        .filter(
            EventORM.store_id == store_id,
            EventORM.is_staff.is_(False),
            EventORM.event_type.in_(["ENTRY", "REENTRY"]),
            EventORM.timestamp >= since,
            EventORM.timestamp <= until,
        )
        .scalar()
        or 0
    )
    if entries == 0:
        return 0.0
    converted = _get_converted_visitors(db, store_id, since, until)
    return len(converted) / entries


# GET /stores/{store_id}/anomalies

@router.get("/{store_id}/anomalies", response_model=AnomaliesResponse)
def get_anomalies(
    store_id: str,
    db: Session = Depends(get_db),
) -> AnomaliesResponse:
    now = datetime.now(timezone.utc)
    anomalies: list[Anomaly] = []

    # Queue Spike (rolling 2-hour window)
    queue_since = now - timedelta(hours=2)
    joins: int = (
        db.query(func.count(EventORM.id))
        .filter(
            EventORM.store_id == store_id,
            EventORM.is_staff.is_(False),
            EventORM.event_type == "BILLING_QUEUE_JOIN",
            EventORM.timestamp >= queue_since,
        )
        .scalar()
        or 0
    )
    abandons: int = (
        db.query(func.count(EventORM.id))
        .filter(
            EventORM.store_id == store_id,
            EventORM.is_staff.is_(False),
            EventORM.event_type == "BILLING_QUEUE_ABANDON",
            EventORM.timestamp >= queue_since,
        )
        .scalar()
        or 0
    )
    queue_depth = max(0, joins - abandons)

    if queue_depth > QUEUE_CRITICAL_THRESHOLD:
        anomalies.append(
            Anomaly(
                anomaly_type="QUEUE_SPIKE",
                severity=Severity.CRITICAL,
                description=(
                    f"Billing queue depth is {queue_depth} — critically above normal."
                ),
                suggested_action=(
                    "Open all available billing counters immediately "
                    "and notify the floor supervisor."
                ),
                detected_at=now,
                value=float(queue_depth),
                threshold=float(QUEUE_CRITICAL_THRESHOLD),
            )
        )
    elif queue_depth > QUEUE_WARN_THRESHOLD:
        anomalies.append(
            Anomaly(
                anomaly_type="QUEUE_SPIKE",
                severity=Severity.WARN,
                description=(
                    f"Billing queue depth is {queue_depth}, "
                    f"above warning threshold of {QUEUE_WARN_THRESHOLD}."
                ),
                suggested_action="Consider opening an additional billing counter.",
                detected_at=now,
                value=float(queue_depth),
                threshold=float(QUEUE_WARN_THRESHOLD),
            )
        )

    # Conversion Drop (today vs 7-day average)
    today_start = now - timedelta(hours=24)
    week_start = now - timedelta(days=7)

    today_rate = _conversion_rate(db, store_id, today_start, now)
    week_rate = _conversion_rate(db, store_id, week_start, today_start)

    if week_rate > 0:
        relative_drop = (week_rate - today_rate) / week_rate
        if relative_drop >= CONVERSION_DROP_WARN:
            severity = (
                Severity.CRITICAL
                if relative_drop >= CONVERSION_DROP_CRITICAL
                else Severity.WARN
            )
            anomalies.append(
                Anomaly(
                    anomaly_type="CONVERSION_DROP",
                    severity=severity,
                    description=(
                        f"Today's conversion rate ({today_rate:.1%}) is "
                        f"{relative_drop:.1%} below the 7-day average "
                        f"({week_rate:.1%})."
                    ),
                    suggested_action=(
                        "Review product placement, promotions, and staffing levels."
                    ),
                    detected_at=now,
                    value=round(today_rate, 4),
                    threshold=round(week_rate, 4),
                )
            )

    # Dead Zones
    dead_cutoff = now - timedelta(hours=DEAD_ZONE_HOURS)

    all_zones = (
        db.query(distinct(EventORM.zone_id))
        .filter(
            EventORM.store_id == store_id,
            EventORM.zone_id.isnot(None),
        )
        .all()
    )

    for (zone_id,) in all_zones:
        last_activity = (
            db.query(func.max(EventORM.timestamp))
            .filter(
                EventORM.store_id == store_id,
                EventORM.zone_id == zone_id,
                EventORM.is_staff.is_(False),
            )
            .scalar()
        )

        if last_activity:
            if last_activity.tzinfo is None:
                last_activity = last_activity.replace(tzinfo=timezone.utc)

            if last_activity < dead_cutoff:
                hours_silent = (now - last_activity).total_seconds() / 3600
                anomalies.append(
                    Anomaly(
                        anomaly_type="DEAD_ZONE",
                        severity=Severity.INFO,
                        description=(
                            f"Zone '{zone_id}' has had no visitor activity "
                            f"for {hours_silent:.1f} hours."
                        ),
                        suggested_action=(
                            f"Verify the camera feed for zone '{zone_id}'. "
                            "Check product placement and signage."
                        ),
                        detected_at=now,
                        value=round(hours_silent, 1),
                        threshold=float(DEAD_ZONE_HOURS),
                    )
                )


    return AnomaliesResponse(
        store_id=store_id,
        checked_at=now,
        active_anomalies=anomalies,
    )
