# PROMPT: Write pytest tests for GET /stores/{id}/metrics and GET /stores/{id}/heatmap
#         using a SQLite in-memory database via the conftest api_client fixture.
#         Seed EventORM and POSTransactionORM rows directly to verify response logic.
# CHANGES MADE: Fixed datetime.utcnow() → datetime.now(timezone.utc) to fix
#               timezone-naive/aware mismatch breaking SQLite timestamp comparisons.
#               Added StaticPool note in conftest dependency.

import uuid
from datetime import datetime, timezone, timedelta

import pytest

from app.database import EventORM, POSTransactionORM


# Seed helpers

def _ts(minutes_ago: int = 0) -> datetime:
    """Timezone-aware UTC datetime, optionally offset into the past."""
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


def _add_event(db_session, **kwargs) -> EventORM:
    defaults = dict(
        event_id=str(uuid.uuid4()),
        store_id="store-001",
        camera_id="cam-01",
        visitor_id=str(uuid.uuid4()),
        event_type="ENTRY",
        timestamp=_ts(),
        zone_id=None,
        dwell_ms=0,
        is_staff=False,
        confidence=0.9,
        event_metadata={},
        session_seq=0,
    )
    defaults.update(kwargs)
    row = EventORM(**defaults)
    db_session.add(row)
    db_session.commit()
    return row


def _add_transaction(db_session, **kwargs) -> POSTransactionORM:
    defaults = dict(
        transaction_id=str(uuid.uuid4()),
        store_id="store-001",
        timestamp=_ts(),
        amount=50.0,
        items_count=3,
    )
    defaults.update(kwargs)
    row = POSTransactionORM(**defaults)
    db_session.add(row)
    db_session.commit()
    return row


# Metrics endpoint tests

class TestMetrics:
    def test_empty_store_returns_zeroes(self, api_client):
        r = api_client.get("/stores/store-001/metrics")
        assert r.status_code == 200
        data = r.json()
        assert data["unique_visitors"] == 0
        assert data["conversion_rate"] == 0.0
        assert data["abandonment_rate"] == 0.0
        assert data["current_queue_depth"] == 0
        assert data["avg_dwell_per_zone"] == []

    def test_unique_visitor_count(self, api_client, db_session):
        vid = str(uuid.uuid4())
        _add_event(db_session, visitor_id=vid, event_type="ENTRY")
        _add_event(db_session, visitor_id=vid, event_type="REENTRY")   # same visitor
        _add_event(db_session, visitor_id=str(uuid.uuid4()), event_type="ENTRY")

        r = api_client.get("/stores/store-001/metrics")
        assert r.json()["unique_visitors"] == 2

    def test_staff_excluded_from_unique_visitors(self, api_client, db_session):
        _add_event(db_session, is_staff=True)
        _add_event(db_session, is_staff=False)

        r = api_client.get("/stores/store-001/metrics")
        assert r.json()["unique_visitors"] == 1

    def test_abandonment_rate_calculation(self, api_client, db_session):
        for _ in range(4):
            _add_event(db_session, event_type="BILLING_QUEUE_JOIN",
                       zone_id="zone-billing", timestamp=_ts(30))
        for _ in range(2):
            _add_event(db_session, event_type="BILLING_QUEUE_ABANDON",
                       zone_id="zone-billing", timestamp=_ts(25))

        r = api_client.get("/stores/store-001/metrics")
        assert r.json()["abandonment_rate"] == pytest.approx(0.5, abs=0.01)

    def test_queue_depth_joins_minus_abandons(self, api_client, db_session):
        for _ in range(5):
            _add_event(db_session, event_type="BILLING_QUEUE_JOIN",
                       zone_id="zone-billing", timestamp=_ts(30))
        for _ in range(2):
            _add_event(db_session, event_type="BILLING_QUEUE_ABANDON",
                       zone_id="zone-billing", timestamp=_ts(20))

        r = api_client.get("/stores/store-001/metrics")
        assert r.json()["current_queue_depth"] == 3

    def test_avg_dwell_per_zone(self, api_client, db_session):
        _add_event(db_session, event_type="ZONE_DWELL", zone_id="zone-a", dwell_ms=1000)
        _add_event(db_session, event_type="ZONE_DWELL", zone_id="zone-a", dwell_ms=3000)

        r = api_client.get("/stores/store-001/metrics")
        zones = r.json()["avg_dwell_per_zone"]
        assert len(zones) == 1
        assert zones[0]["zone_id"] == "zone-a"
        assert zones[0]["avg_dwell_ms"] == pytest.approx(2000.0, abs=1)

    def test_window_hours_query_param(self, api_client, db_session):
        _add_event(db_session, timestamp=datetime.now(timezone.utc) - timedelta(days=3))
        _add_event(db_session, timestamp=_ts(10))

        r = api_client.get("/stores/store-001/metrics?window_hours=24")
        assert r.json()["unique_visitors"] == 1

    def test_store_isolation(self, api_client, db_session):
        _add_event(db_session, store_id="store-999")
        r = api_client.get("/stores/store-001/metrics")
        assert r.json()["unique_visitors"] == 0


# Heatmap endpoint tests

class TestHeatmap:
    def test_empty_store_returns_empty_zones(self, api_client):
        r = api_client.get("/stores/store-001/heatmap")
        assert r.status_code == 200
        assert r.json()["zones"] == []

    def test_normalized_score_of_highest_zone_is_100(self, api_client, db_session):
        for _ in range(10):
            _add_event(db_session, event_type="ZONE_ENTER", zone_id="zone-a")
        for _ in range(5):
            _add_event(db_session, event_type="ZONE_ENTER", zone_id="zone-b")

        r = api_client.get("/stores/store-001/heatmap")
        zones = r.json()["zones"]
        top = zones[0]
        assert top["zone_id"] == "zone-a"
        assert top["normalized_score"] == 100.0
        assert zones[1]["normalized_score"] == pytest.approx(50.0, abs=1)

    def test_data_confidence_false_below_20_sessions(self, api_client, db_session):
        for _ in range(5):
            _add_event(db_session, event_type="ZONE_ENTER", zone_id="zone-x",
                       visitor_id=str(uuid.uuid4()))

        r = api_client.get("/stores/store-001/heatmap")
        zone = next(z for z in r.json()["zones"] if z["zone_id"] == "zone-x")
        assert zone["data_confidence"] is False

    def test_data_confidence_true_at_20_sessions(self, api_client, db_session):
        for _ in range(20):
            _add_event(db_session, event_type="ZONE_ENTER", zone_id="zone-y",
                       visitor_id=str(uuid.uuid4()))

        r = api_client.get("/stores/store-001/heatmap")
        zone = next(z for z in r.json()["zones"] if z["zone_id"] == "zone-y")
        assert zone["data_confidence"] is True

    def test_staff_excluded_from_heatmap(self, api_client, db_session):
        _add_event(db_session, event_type="ZONE_ENTER", zone_id="zone-staff", is_staff=True)
        r = api_client.get("/stores/store-001/heatmap")
        zone_ids = [z["zone_id"] for z in r.json()["zones"]]
        assert "zone-staff" not in zone_ids
