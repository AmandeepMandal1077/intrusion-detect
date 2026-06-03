# Store Intelligence API — Design Document

## Overview
This API processes structured events emitted by a computer vision pipeline
watching CCTV footage. It exposes endpoints for live retail conversion metrics.

## Service Skeleton
- Framework: FastAPI (async, auto-OpenAPI docs, Pydantic v2 validation)
- Python version: 3.11+
- Entry point: `app/main.py`
- Virtual environment: `venv/` (not committed)
- Health check: `GET /health` returns structured JSON with status and version

## Event Schema & Ingestion API

### Event Schema (`app/models.py`)
The `StoreEvent` Pydantic v2 model enforces:
- **UUID v4** `event_id` as the primary idempotency key.
- **ISO-8601 UTC** timestamps (naive datetimes are coerced to UTC).
- **Cross-field validation**: `zone_id` is required for zone-scoped event types
  (ZONE_ENTER, ZONE_EXIT, ZONE_DWELL, BILLING_QUEUE_JOIN, BILLING_QUEUE_ABANDON).

### Idempotency Strategy
`POST /events/ingest` uses an **in-memory dict keyed by `event_id` (UUID)**
for O(1) duplicate detection. Duplicate submissions are counted but silently
dropped — the endpoint always returns HTTP 200. This means:
- CV pipeline nodes can safely retry on network failure with no side-effects.
- Callers can detect retry success/failure by inspecting the `duplicates` counter.

### Partial Success (`POST /events/ingest/raw`)
The `/raw` endpoint validates each item individually. This allows a pipeline
recovery path where a partially-corrupted batch does not lose all good events.
Each failed item is reported with its 0-based `index` and a human-readable `reason`.
