"""
POST /events/ingest — Batch event ingestion backed by PostgreSQL.

Idempotency is enforced via a UNIQUE constraint on event_id.
We pre-query for duplicates in one round-trip before inserting.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import EventORM, get_db
from app.models import EventError, IngestRequest, IngestResponse, StoreEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["Ingestion"])


# Internal helpers

def _pydantic_to_orm(event: StoreEvent) -> EventORM:
    """Convert a validated Pydantic StoreEvent into an ORM row."""
    return EventORM(
        event_id=str(event.event_id),
        store_id=event.store_id,
        camera_id=event.camera_id,
        visitor_id=event.visitor_id,
        event_type=event.event_type.value,
        timestamp=event.timestamp,
        zone_id=event.zone_id,
        dwell_ms=event.dwell_ms,
        is_staff=event.is_staff,
        confidence=event.confidence,
        event_metadata=event.metadata,
        session_seq=event.session_seq,
    )


def _find_existing_ids(db: Session, candidate_ids: list[str]) -> set[str]:
    """Single-query duplicate detection across the entire batch."""
    if not candidate_ids:
        return set()
    return {
        row.event_id
        for row in db.query(EventORM.event_id)
        .filter(EventORM.event_id.in_(candidate_ids))
        .all()
    }


# Structured ingest endpoint

@router.post(
    "/ingest",
    response_model=IngestResponse,
    summary="Ingest a batch of CV detection events",
    responses={200: {"description": "Batch processed (may include partial failures)."}},
)
async def ingest_events(
    payload: IngestRequest,
    db: Session = Depends(get_db),
) -> IngestResponse:
    """
    Accept a batch of up to 500 validated StoreEvent objects.

    **Idempotency**: Duplicates (by event_id) are detected with one pre-query
    and skipped. Intra-batch duplicates are also collapsed. Returns HTTP 200
    always so callers can safely retry entire batches.
    """
    incoming_ids = [str(e.event_id) for e in payload.events]
    existing_ids = _find_existing_ids(db, incoming_ids)

    accepted = 0
    duplicates = 0
    seen_in_batch: set[str] = set()

    for event in payload.events:
        eid = str(event.event_id)
        if eid in existing_ids or eid in seen_in_batch:
            duplicates += 1
            continue
        db.add(_pydantic_to_orm(event))
        seen_in_batch.add(eid)
        accepted += 1

    db.commit()
    return IngestResponse(accepted=accepted, duplicates=duplicates, rejected=0, errors=[])


# Raw ingest endpoint (partial success on malformed items)

@router.post(
    "/ingest/raw",
    response_model=IngestResponse,
    summary="Ingest a raw batch — validates each item individually",
    description=(
        "Accepts a JSON array of raw event dicts. Each item is validated "
        "independently; malformed items are reported in `errors` while "
        "valid items are still stored."
    ),
)
async def ingest_events_raw(
    payload: list[dict[str, Any]],
    db: Session = Depends(get_db),
) -> IngestResponse:
    if not payload:
        return IngestResponse(accepted=0, duplicates=0, rejected=0, errors=[])

    if len(payload) > 500:
        return IngestResponse(
            accepted=0,
            duplicates=0,
            rejected=len(payload),
            errors=[
                EventError(
                    index=0,
                    event_id=None,
                    reason=f"Batch size {len(payload)} exceeds maximum of 500.",
                )
            ],
        )

    # validate each item
    valid_events: list[tuple[int, StoreEvent]] = []
    errors: list[EventError] = []

    for idx, raw in enumerate(payload):
        event_id_str = str(raw.get("event_id", "")) or None
        try:
            event = StoreEvent.model_validate(raw)
            valid_events.append((idx, event))
        except (ValidationError, Exception) as exc:
            errors.append(EventError(index=idx, event_id=event_id_str, reason=str(exc)))

    # find duplicates in one query
    candidate_ids = [str(e.event_id) for _, e in valid_events]
    existing_ids = _find_existing_ids(db, candidate_ids)

    accepted = 0
    duplicates = 0
    seen_in_batch: set[str] = set()

    for _, event in valid_events:
        eid = str(event.event_id)
        if eid in existing_ids or eid in seen_in_batch:
            duplicates += 1
            continue
        db.add(_pydantic_to_orm(event))
        seen_in_batch.add(eid)
        accepted += 1

    db.commit()
    return IngestResponse(
        accepted=accepted,
        duplicates=duplicates,
        rejected=len(errors),
        errors=errors,
    )
