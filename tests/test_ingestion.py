# PROMPT: Write pytest tests for a FastAPI POST /events/ingest endpoint backed by
#         PostgreSQL (SQLite in tests). Validate Pydantic event models, batch sizes,
#         DB-level idempotency, and partial-success on the raw endpoint.
# CHANGES MADE: Replaced in-memory clear_event_store with conftest api_client fixture;
#               added intra-batch duplicate test; all tests now use function-scoped SQLite DB.

import uuid
import pytest
from fastapi.testclient import TestClient


# Helpers

def make_event(**overrides) -> dict:
    base = {
        "event_id": str(uuid.uuid4()),
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
    }
    base.update(overrides)
    return base


# Structured ingest — POST /events/ingest

class TestIngestStructured:
    def test_single_valid_event_accepted(self, api_client):
        r = api_client.post("/events/ingest", json={"events": [make_event()]})
        assert r.status_code == 200
        data = r.json()
        assert data["accepted"] == 1
        assert data["duplicates"] == 0
        assert data["rejected"] == 0

    def test_batch_of_multiple_valid_events(self, api_client):
        events = [make_event() for _ in range(5)]
        r = api_client.post("/events/ingest", json={"events": events})
        assert r.json()["accepted"] == 5

    def test_idempotency_same_event_submitted_twice(self, api_client):
        event = make_event()
        api_client.post("/events/ingest", json={"events": [event]})
        r2 = api_client.post("/events/ingest", json={"events": [event]})
        data = r2.json()
        assert data["accepted"] == 0
        assert data["duplicates"] == 1

    def test_idempotency_mixed_batch(self, api_client):
        existing = make_event()
        api_client.post("/events/ingest", json={"events": [existing]})
        batch = [existing, make_event(), make_event()]
        r = api_client.post("/events/ingest", json={"events": batch})
        assert r.json()["accepted"] == 2
        assert r.json()["duplicates"] == 1

    def test_intra_batch_duplicate_collapsed(self, api_client):
        """Two identical event_ids in the same batch → only one stored."""
        event = make_event()
        r = api_client.post("/events/ingest", json={"events": [event, event]})
        data = r.json()
        assert data["accepted"] == 1
        assert data["duplicates"] == 1

    def test_empty_batch_rejected(self, api_client):
        r = api_client.post("/events/ingest", json={"events": []})
        assert r.status_code == 422

    def test_batch_exceeds_500_rejected(self, api_client):
        events = [make_event() for _ in range(501)]
        r = api_client.post("/events/ingest", json={"events": events})
        assert r.status_code == 422

    def test_missing_required_field_rejected(self, api_client):
        bad = make_event()
        del bad["store_id"]
        r = api_client.post("/events/ingest", json={"events": [bad]})
        assert r.status_code == 422

    def test_invalid_confidence_rejected(self, api_client):
        r = api_client.post("/events/ingest", json={"events": [make_event(confidence=1.5)]})
        assert r.status_code == 422

    def test_invalid_event_type_rejected(self, api_client):
        r = api_client.post("/events/ingest", json={"events": [make_event(event_type="TELEPORT")]})
        assert r.status_code == 422

    def test_zone_event_without_zone_id_rejected(self, api_client):
        r = api_client.post(
            "/events/ingest",
            json={"events": [make_event(event_type="ZONE_ENTER", zone_id=None)]},
        )
        assert r.status_code == 422

    def test_zone_event_with_zone_id_accepted(self, api_client):
        r = api_client.post(
            "/events/ingest",
            json={"events": [make_event(event_type="ZONE_ENTER", zone_id="zone-produce")]},
        )
        assert r.json()["accepted"] == 1

    def test_billing_queue_join_requires_zone_id(self, api_client):
        r = api_client.post(
            "/events/ingest",
            json={"events": [make_event(event_type="BILLING_QUEUE_JOIN", zone_id=None)]},
        )
        assert r.status_code == 422

    def test_staff_flag_accepted(self, api_client):
        r = api_client.post("/events/ingest", json={"events": [make_event(is_staff=True)]})
        assert r.json()["accepted"] == 1

    def test_negative_dwell_ms_rejected(self, api_client):
        r = api_client.post("/events/ingest", json={"events": [make_event(dwell_ms=-1)]})
        assert r.status_code == 422

    def test_naive_timestamp_coerced_to_utc(self, api_client):
        r = api_client.post(
            "/events/ingest",
            json={"events": [make_event(timestamp="2026-06-01T09:00:00")]},
        )
        assert r.json()["accepted"] == 1


# Raw ingest — POST /events/ingest/raw

class TestIngestRaw:
    def test_all_valid_events_accepted(self, api_client):
        r = api_client.post("/events/ingest/raw", json=[make_event() for _ in range(3)])
        assert r.json()["accepted"] == 3

    def test_partial_failure_valid_events_still_stored(self, api_client):
        good = make_event()
        bad = make_event()
        del bad["store_id"]
        r = api_client.post("/events/ingest/raw", json=[good, bad])
        data = r.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 1
        assert data["errors"][0]["index"] == 1

    def test_all_bad_events_returns_all_errors(self, api_client):
        bad1 = make_event(confidence=99)
        bad2 = make_event(event_type="INVALID")
        r = api_client.post("/events/ingest/raw", json=[bad1, bad2])
        assert r.json()["accepted"] == 0
        assert r.json()["rejected"] == 2

    def test_empty_list_returns_zeros(self, api_client):
        r = api_client.post("/events/ingest/raw", json=[])
        assert r.json()["accepted"] == 0

    def test_oversized_batch_soft_rejected(self, api_client):
        events = [make_event() for _ in range(501)]
        r = api_client.post("/events/ingest/raw", json=events)
        assert r.json()["rejected"] == 501

    def test_raw_idempotency(self, api_client):
        event = make_event()
        api_client.post("/events/ingest/raw", json=[event])
        r2 = api_client.post("/events/ingest/raw", json=[event])
        assert r2.json()["duplicates"] == 1
