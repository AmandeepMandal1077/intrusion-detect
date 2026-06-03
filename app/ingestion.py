"""
POST /events/ingest — Batch event ingestion with idempotency.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import ValidationError

from app.models import EventError, IngestRequest, IngestResponse, StoreEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["Ingestion"])

_event_store: dict[uuid.UUID, StoreEvent] = {}


def get_event_store() -> dict[uuid.UUID, StoreEvent]:
    """Accessor used by tests to inspect stored events without importing the dict directly."""
    return _event_store


def clear_event_store() -> None:
    """Test helper — wipes the in-memory store between test runs."""
    _event_store.clear()


# Endpoint

@router.post(
    "/ingest",
    response_model=IngestResponse,
    summary="Ingest a batch of CV detection events",
    responses={
        200: {"description": "Batch processed (may include partial failures)."},
    },
)
async def ingest_events(payload: IngestRequest) -> IngestResponse:
    """
    Accept a batch of up to 500 StoreEvent objects.

    **Idempotency**: If an event with the same `event_id` is submitted more
    than once, subsequent submissions are silently counted as duplicates and
    NOT re-stored. The endpoint always returns HTTP 200 so callers can safely
    retry the entire batch.

    **Partial success**: Events that fail secondary validation (e.g. zone
    rules) are collected in `errors`; all valid, non-duplicate events in the
    same batch are still accepted.
    """
    accepted = 0
    duplicates = 0
    errors: list[EventError] = []

    for idx, event in enumerate(payload.events):
        if event.event_id in _event_store:
            duplicates += 1
            logger.debug("Duplicate event skipped: %s", event.event_id)
            continue

        _event_store[event.event_id] = event
        accepted += 1

    return IngestResponse(
        accepted=accepted,
        duplicates=duplicates,
        rejected=len(errors),
        errors=errors,
    )


# Raw-batch endpoint for partial-success on malformed items

@router.post(
    "/ingest/raw",
    response_model=IngestResponse,
    summary="Ingest a raw batch — validates each item individually",
    description=(
        "Accepts a JSON array of raw event dicts. "
        "Each item is validated independently; malformed items are reported "
        "in `errors` while valid items are still stored."
    ),
)
async def ingest_events_raw(payload: list[dict[str, Any]]) -> IngestResponse:
    """
    Raw ingest: validates each dict individually so a single bad record does
    not reject the whole batch.  The structured `POST /events/ingest` endpoint
    should be preferred by well-behaved clients; this endpoint exists for
    pipeline error-recovery scenarios.
    """
    if not payload:
        return IngestResponse(accepted=0, duplicates=0, rejected=0, errors=[])

    if len(payload) > 500:
        # Soft-reject oversized batch — callers must chunk themselves
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

    accepted = 0
    duplicates = 0
    errors: list[EventError] = []

    for idx, raw in enumerate(payload):
        event_id_str: str | None = None
        try:
            event_id_str = str(raw.get("event_id", ""))
            event = StoreEvent.model_validate(raw)
        except (ValidationError, Exception) as exc:
            errors.append(
                EventError(
                    index=idx,
                    event_id=event_id_str or None,
                    reason=str(exc),
                )
            )
            continue

        if event.event_id in _event_store:
            duplicates += 1
            continue

        _event_store[event.event_id] = event
        accepted += 1

    return IngestResponse(
        accepted=accepted,
        duplicates=duplicates,
        rejected=len(errors),
        errors=errors,
    )
