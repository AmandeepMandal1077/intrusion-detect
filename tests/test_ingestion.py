# PROMPT: Write pytest tests for a FastAPI POST /events/ingest endpoint that
#         validates Pydantic event models, handles batches of up to 500 events,
#         enforces idempotency by event_id, and returns partial-success responses
#         for the raw ingest endpoint.
# CHANGES MADE: Added zone_id validation tests; added raw endpoint partial-failure
#               tests; added batch-size-limit test; used clear_event_store() fixture.

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.ingestion import clear_event_store
from app.main import app

client = TestClient(app)

# Helpers

def make_event(**overrides) -> dict:
    """Return a valid event dict, optionally overriding any field."""
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


# Fixtures

@pytest.fixture(autouse=True)
def reset_store():
    """Wipe the in-memory event store before every test for isolation."""
    clear_event_store()
    yield
    clear_event_store()


# Structured ingest — POST /events/ingest

class TestIngestStructured:
    def test_single_valid_event_accepted(self):
        response = client.post("/events/ingest", json={"events": [make_event()]})
        assert response.status_code == 200
        data = response.json()
        assert data["accepted"] == 1
        assert data["duplicates"] == 0
        assert data["rejected"] == 0
        assert data["errors"] == []

    def test_batch_of_multiple_valid_events(self):
        events = [make_event() for _ in range(5)]
        response = client.post("/events/ingest", json={"events": events})
        assert response.status_code == 200
        assert response.json()["accepted"] == 5

    def test_idempotency_duplicate_event_not_double_stored(self):
        event = make_event()
        # First submission
        r1 = client.post("/events/ingest", json={"events": [event]})
        assert r1.json()["accepted"] == 1

        # Second submission — same event_id
        r2 = client.post("/events/ingest", json={"events": [event]})
        data = r2.json()
        assert data["accepted"] == 0
        assert data["duplicates"] == 1
        assert data["rejected"] == 0

    def test_idempotency_mixed_batch(self):
        """One duplicate + two new events in a single batch."""
        existing = make_event()
        client.post("/events/ingest", json={"events": [existing]})

        new_events = [make_event(), make_event()]
        batch = [existing] + new_events
        response = client.post("/events/ingest", json={"events": batch})
        data = response.json()
        assert data["accepted"] == 2
        assert data["duplicates"] == 1

    def test_empty_batch_rejected(self):
        """events list must have at least 1 item."""
        response = client.post("/events/ingest", json={"events": []})
        assert response.status_code == 422

    def test_batch_exceeds_500_rejected(self):
        events = [make_event() for _ in range(501)]
        response = client.post("/events/ingest", json={"events": events})
        assert response.status_code == 422

    def test_missing_required_field_rejected(self):
        bad = make_event()
        del bad["store_id"]
        response = client.post("/events/ingest", json={"events": [bad]})
        assert response.status_code == 422

    def test_invalid_confidence_rejected(self):
        """Confidence must be in [0.0, 1.0]."""
        bad = make_event(confidence=1.5)
        response = client.post("/events/ingest", json={"events": [bad]})
        assert response.status_code == 422

    def test_invalid_event_type_rejected(self):
        bad = make_event(event_type="TELEPORT")
        response = client.post("/events/ingest", json={"events": [bad]})
        assert response.status_code == 422

    def test_zone_event_without_zone_id_rejected(self):
        """ZONE_ENTER without zone_id must fail model validation."""
        bad = make_event(event_type="ZONE_ENTER", zone_id=None)
        response = client.post("/events/ingest", json={"events": [bad]})
        assert response.status_code == 422

    def test_zone_event_with_zone_id_accepted(self):
        event = make_event(event_type="ZONE_ENTER", zone_id="zone-produce")
        response = client.post("/events/ingest", json={"events": [event]})
        assert response.status_code == 200
        assert response.json()["accepted"] == 1

    def test_billing_queue_join_requires_zone_id(self):
        bad = make_event(event_type="BILLING_QUEUE_JOIN", zone_id=None)
        response = client.post("/events/ingest", json={"events": [bad]})
        assert response.status_code == 422

    def test_staff_event_accepted(self):
        event = make_event(is_staff=True)
        response = client.post("/events/ingest", json={"events": [event]})
        assert response.json()["accepted"] == 1

    def test_metadata_dict_accepted(self):
        event = make_event(metadata={"queue_depth": 3, "lane": "A"})
        response = client.post("/events/ingest", json={"events": [event]})
        assert response.json()["accepted"] == 1

    def test_negative_dwell_ms_rejected(self):
        bad = make_event(dwell_ms=-1)
        response = client.post("/events/ingest", json={"events": [bad]})
        assert response.status_code == 422

    def test_naive_timestamp_accepted_as_utc(self):
        """Naive timestamps should be coerced to UTC without error."""
        event = make_event(timestamp="2026-06-01T09:00:00")
        response = client.post("/events/ingest", json={"events": [event]})
        assert response.json()["accepted"] == 1


# Raw ingest — POST /events/ingest/raw  (partial success)

class TestIngestRaw:
    def test_all_valid_events_accepted(self):
        events = [make_event() for _ in range(3)]
        response = client.post("/events/ingest/raw", json=events)
        data = response.json()
        assert response.status_code == 200
        assert data["accepted"] == 3
        assert data["rejected"] == 0

    def test_partial_failure_valid_events_still_stored(self):
        good = make_event()
        bad = make_event()
        del bad["store_id"]  # will fail validation
        response = client.post("/events/ingest/raw", json=[good, bad])
        data = response.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 1
        assert len(data["errors"]) == 1
        assert data["errors"][0]["index"] == 1

    def test_all_bad_events_returns_all_errors(self):
        bad1 = make_event(confidence=99)
        bad2 = make_event(event_type="INVALID")
        response = client.post("/events/ingest/raw", json=[bad1, bad2])
        data = response.json()
        assert data["accepted"] == 0
        assert data["rejected"] == 2

    def test_empty_list_returns_zeros(self):
        response = client.post("/events/ingest/raw", json=[])
        data = response.json()
        assert data["accepted"] == 0
        assert data["rejected"] == 0

    def test_oversized_raw_batch_soft_rejected(self):
        events = [make_event() for _ in range(501)]
        response = client.post("/events/ingest/raw", json=events)
        data = response.json()
        assert response.status_code == 200
        assert data["rejected"] == 501
        assert "500" in data["errors"][0]["reason"]

    def test_raw_idempotency(self):
        event = make_event()
        client.post("/events/ingest/raw", json=[event])
        r2 = client.post("/events/ingest/raw", json=[event])
        data = r2.json()
        assert data["accepted"] == 0
        assert data["duplicates"] == 1
