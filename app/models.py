from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# Enumerations

class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


# Event Model

class StoreEvent(BaseModel):
    """A single detection event emitted by the CV pipeline."""

    event_id: uuid.UUID = Field(
        description="UUID v4 — primary key; used for idempotency."
    )
    store_id: str = Field(min_length=1, description="Logical store identifier.")
    camera_id: str = Field(min_length=1, description="Camera that captured the event.")
    visitor_id: str = Field(
        min_length=1, description="Anonymised tracker ID for this individual."
    )
    event_type: EventType
    timestamp: datetime = Field(description="ISO-8601 UTC timestamp of the event.")
    zone_id: str | None = Field(
        default=None,
        description="Zone identifier; null for ENTRY/EXIT events.",
    )
    dwell_ms: int = Field(
        default=0,
        ge=0,
        description="Time spent in zone in milliseconds. 0 for instantaneous events.",
    )
    is_staff: bool = Field(
        default=False,
        description="True if this person is identified as store staff.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Detection confidence score in [0.0, 1.0].",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary metadata (e.g., {'queue_depth': 3}).",
    )
    session_seq: int = Field(
        ge=0,
        description="Monotonically increasing sequence number within a visitor session.",
    )

    @field_validator("timestamp", mode="before")
    @classmethod
    def parse_and_enforce_utc(cls, v: Any) -> datetime:
        """Accept ISO-8601 strings and enforce UTC timezone."""
        if isinstance(v, str):
            try:
                from dateutil import parser as du_parser

                v = du_parser.isoparse(v)
            except Exception:
                raise ValueError(f"Cannot parse timestamp: {v!r}")

        if isinstance(v, datetime):
            if v.tzinfo is None:
                # Treat naive datetimes as UTC
                return v.replace(tzinfo=timezone.utc)
            return v.astimezone(timezone.utc)

        raise ValueError("timestamp must be a datetime or ISO-8601 string.")

    @model_validator(mode="after")
    def zone_required_for_zone_events(self) -> StoreEvent:
        """zone_id must be present for zone-scoped event types."""
        zone_required = {
            EventType.ZONE_ENTER,
            EventType.ZONE_EXIT,
            EventType.ZONE_DWELL,
            EventType.BILLING_QUEUE_JOIN,
            EventType.BILLING_QUEUE_ABANDON,
        }
        if self.event_type in zone_required and not self.zone_id:
            raise ValueError(
                f"zone_id is required for event_type={self.event_type.value}"
            )
        return self

    model_config = {"json_schema_extra": {"example": {
        "event_id": "550e8400-e29b-41d4-a716-446655440000",
        "store_id": "store-001",
        "camera_id": "cam-entrance-01",
        "visitor_id": "vis-abc123",
        "event_type": "ENTRY",
        "timestamp": "2026-06-01T09:00:00Z",
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.97,
        "metadata": {},
        "session_seq": 0,
    }}}

# Batch Request / Response Models

MAX_BATCH_SIZE = 500


class IngestRequest(BaseModel):
    """Payload envelope for the POST /events/ingest endpoint."""

    events: list[StoreEvent] = Field(
        min_length=1,
        max_length=MAX_BATCH_SIZE,
        description=f"Batch of 1–{MAX_BATCH_SIZE} events.",
    )


class EventError(BaseModel):
    """Details about a single event that failed validation or processing."""

    index: int = Field(description="0-based index of the failed event in the batch.")
    event_id: str | None = Field(
        default=None,
        description="event_id if it was parseable, else null.",
    )
    reason: str = Field(description="Human-readable error description.")


class IngestResponse(BaseModel):
    """Partial-success response from POST /events/ingest."""

    accepted: int = Field(description="Number of events successfully ingested.")
    duplicates: int = Field(description="Number of events skipped as duplicates.")
    rejected: int = Field(description="Number of events that failed validation.")
    errors: list[EventError] = Field(
        default_factory=list,
        description="Per-event error details for rejected events.",
    )
