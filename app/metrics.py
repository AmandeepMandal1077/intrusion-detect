"""
GET /stores/{store_id}/metrics  — Live conversion and dwell metrics.
GET /stores/{store_id}/heatmap  — Zone visit frequency, normalised 0-100.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import distinct, func
from sqlalchemy.orm import Session

from app.database import EventORM, POSTransactionORM, get_db

router = APIRouter(prefix="/stores", tags=["Analytics"])

# Response schemas


class ZoneDwellStat(BaseModel):
    zone_id: str
    avg_dwell_ms: float
    visit_count: int


class MetricsResponse(BaseModel):
    store_id: str
    window_hours: int
    unique_visitors: int
    conversion_rate: float
    avg_dwell_per_zone: list[ZoneDwellStat]
    current_queue_depth: int
    abandonment_rate: float


class HeatmapZone(BaseModel):
    zone_id: str
    visit_count: int
    avg_dwell_ms: float
    normalized_score: float  # 0 – 100
    data_confidence: bool    # False when session count < 20


class HeatmapResponse(BaseModel):
    store_id: str
    window_hours: int
    zones: list[HeatmapZone]

# Internal helpers

def _get_converted_visitors(
    db: Session,
    store_id: str,
    since: datetime,
    until: datetime,
    correlation_window_minutes: int = 5,
) -> set[str]:
    """
    Return the set of visitor_ids who were in a billing zone within
    `correlation_window_minutes` before any POS transaction in [since, until].
    """
    transactions = (
        db.query(POSTransactionORM)
        .filter(
            POSTransactionORM.store_id == store_id,
            POSTransactionORM.timestamp >= since,
            POSTransactionORM.timestamp <= until,
        )
        .all()
    )

    converted: set[str] = set()
    for trx in transactions:
        window_start = trx.timestamp - timedelta(minutes=correlation_window_minutes)
        rows = (
            db.query(distinct(EventORM.visitor_id))
            .filter(
                EventORM.store_id == store_id,
                EventORM.is_staff.is_(False),
                EventORM.event_type.in_(["BILLING_QUEUE_JOIN", "ZONE_ENTER"]),
                EventORM.timestamp >= window_start,
                EventORM.timestamp <= trx.timestamp,
            )
            .all()
        )
        for (v,) in rows:
            converted.add(v)

    return converted


# GET /stores/{store_id}/metrics

@router.get("/{store_id}/metrics", response_model=MetricsResponse)
def get_metrics(
    store_id: str,
    window_hours: int = Query(default=24, ge=1, le=168, description="Lookback window in hours"),
    db: Session = Depends(get_db),
) -> MetricsResponse:
    """
    Live retail conversion metrics for a store.

    Rules:
    - Excludes is_staff=True events.
    - Re-entries do NOT double-count a visitor (distinct visitor_id).
    - Conversion = visitors correlated to a POS transaction via 5-min billing zone window.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)

    # Unique non-staff visitors
    unique_visitors: int = (
        db.query(func.count(distinct(EventORM.visitor_id)))
        .filter(
            EventORM.store_id == store_id,
            EventORM.is_staff.is_(False),
            EventORM.timestamp >= since,
        )
        .scalar()
        or 0
    )

    # Avg dwell per zone
    zone_rows = (
        db.query(
            EventORM.zone_id,
            func.avg(EventORM.dwell_ms).label("avg_dwell_ms"),
            func.count(EventORM.id).label("visit_count"),
        )
        .filter(
            EventORM.store_id == store_id,
            EventORM.is_staff.is_(False),
            EventORM.timestamp >= since,
            EventORM.zone_id.isnot(None),
            EventORM.dwell_ms > 0,
        )
        .group_by(EventORM.zone_id)
        .all()
    )
    avg_dwell_per_zone = [
        ZoneDwellStat(
            zone_id=z.zone_id,
            avg_dwell_ms=round(float(z.avg_dwell_ms), 1),
            visit_count=z.visit_count,
        )
        for z in zone_rows
    ]

    # Queue depth (rolling 2-hour window)
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
    exits: int = (
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
    current_queue_depth = max(0, joins - exits)

    # Abandonment rate (full window)
    total_joins: int = (
        db.query(func.count(EventORM.id))
        .filter(
            EventORM.store_id == store_id,
            EventORM.is_staff.is_(False),
            EventORM.event_type == "BILLING_QUEUE_JOIN",
            EventORM.timestamp >= since,
        )
        .scalar()
        or 0
    )
    total_abandons: int = (
        db.query(func.count(EventORM.id))
        .filter(
            EventORM.store_id == store_id,
            EventORM.is_staff.is_(False),
            EventORM.event_type == "BILLING_QUEUE_ABANDON",
            EventORM.timestamp >= since,
        )
        .scalar()
        or 0
    )
    abandonment_rate = (total_abandons / total_joins) if total_joins > 0 else 0.0

    # Conversion rate via POS correlation
    converted = _get_converted_visitors(db, store_id, since, now)
    conversion_rate = len(converted) / unique_visitors if unique_visitors > 0 else 0.0

    return MetricsResponse(
        store_id=store_id,
        window_hours=window_hours,
        unique_visitors=unique_visitors,
        conversion_rate=round(conversion_rate, 4),
        avg_dwell_per_zone=avg_dwell_per_zone,
        current_queue_depth=current_queue_depth,
        abandonment_rate=round(abandonment_rate, 4),
    )


# GET /stores/{store_id}/heatmap

@router.get("/{store_id}/heatmap", response_model=HeatmapResponse)
def get_heatmap(
    store_id: str,
    window_hours: int = Query(default=24, ge=1, le=168),
    db: Session = Depends(get_db),
) -> HeatmapResponse:
    """
    Zone visit heatmap, visit counts normalised to 0–100.

    `data_confidence` is False when a zone has fewer than 20 distinct visitor
    sessions in the window — not enough data to trust the score.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)

    zone_rows = (
        db.query(
            EventORM.zone_id,
            func.count(EventORM.id).label("visit_count"),
            func.avg(EventORM.dwell_ms).label("avg_dwell_ms"),
            func.count(distinct(EventORM.visitor_id)).label("session_count"),
        )
        .filter(
            EventORM.store_id == store_id,
            EventORM.is_staff.is_(False),
            EventORM.timestamp >= since,
            EventORM.zone_id.isnot(None),
        )
        .group_by(EventORM.zone_id)
        .all()
    )

    if not zone_rows:
        return HeatmapResponse(store_id=store_id, window_hours=window_hours, zones=[])

    max_visits = max(z.visit_count for z in zone_rows) or 1

    zones = [
        HeatmapZone(
            zone_id=z.zone_id,
            visit_count=z.visit_count,
            avg_dwell_ms=round(float(z.avg_dwell_ms or 0), 1),
            normalized_score=round((z.visit_count / max_visits) * 100, 1),
            data_confidence=z.session_count >= 20,
        )
        for z in zone_rows
    ]
    zones.sort(key=lambda x: x.normalized_score, reverse=True)

    return HeatmapResponse(store_id=store_id, window_hours=window_hours, zones=zones)
