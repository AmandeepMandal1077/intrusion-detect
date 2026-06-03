# PROMPT: Write pytest tests for GET /stores/{id}/anomalies using SQLite in-memory DB.
#         Seed EventORM rows to trigger QUEUE_SPIKE, DEAD_ZONE, and CONVERSION_DROP.
# CHANGES MADE: Fixed datetime.utcnow() → datetime.now(timezone.utc) to fix
#               timezone mismatch causing dead-zone comparisons to fail silently.

import uuid
from datetime import datetime, timezone, timedelta

import pytest

from app.database import EventORM, POSTransactionORM


def _ts(hours_ago: float = 0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours_ago)


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


def _anomaly_types(response_json: dict) -> list[str]:
    return [a["anomaly_type"] for a in response_json["active_anomalies"]]


class TestAnomalies:
    def test_no_anomalies_for_empty_store(self, api_client):
        r = api_client.get("/stores/store-001/anomalies")
        assert r.status_code == 200
        assert r.json()["active_anomalies"] == []

    def test_response_includes_checked_at(self, api_client):
        r = api_client.get("/stores/store-001/anomalies")
        assert "checked_at" in r.json()

    def test_queue_spike_warn_above_10(self, api_client, db_session):
        for _ in range(12):
            _add_event(db_session, event_type="BILLING_QUEUE_JOIN",
                       zone_id="zone-billing", timestamp=_ts(0.5))

        r = api_client.get("/stores/store-001/anomalies")
        queue = next((a for a in r.json()["active_anomalies"]
                      if a["anomaly_type"] == "QUEUE_SPIKE"), None)
        assert queue is not None
        assert queue["severity"] == "WARN"
        assert "suggested_action" in queue

    def test_queue_spike_critical_above_20(self, api_client, db_session):
        for _ in range(22):
            _add_event(db_session, event_type="BILLING_QUEUE_JOIN",
                       zone_id="zone-billing", timestamp=_ts(0.5))

        r = api_client.get("/stores/store-001/anomalies")
        queue = next((a for a in r.json()["active_anomalies"]
                      if a["anomaly_type"] == "QUEUE_SPIKE"), None)
        assert queue is not None
        assert queue["severity"] == "CRITICAL"

    def test_abandons_reduce_queue_depth(self, api_client, db_session):
        for _ in range(15):
            _add_event(db_session, event_type="BILLING_QUEUE_JOIN",
                       zone_id="zone-billing", timestamp=_ts(0.5))
        for _ in range(10):
            _add_event(db_session, event_type="BILLING_QUEUE_ABANDON",
                       zone_id="zone-billing", timestamp=_ts(0.3))

        r = api_client.get("/stores/store-001/anomalies")
        assert "QUEUE_SPIKE" not in _anomaly_types(r.json())

    def test_no_queue_spike_below_threshold(self, api_client, db_session):
        for _ in range(5):
            _add_event(db_session, event_type="BILLING_QUEUE_JOIN",
                       zone_id="zone-billing", timestamp=_ts(0.5))
        r = api_client.get("/stores/store-001/anomalies")
        assert "QUEUE_SPIKE" not in _anomaly_types(r.json())

    def test_dead_zone_detected_after_4_hours(self, api_client, db_session):
        _add_event(db_session, event_type="ZONE_ENTER", zone_id="zone-dead",
                   timestamp=_ts(hours_ago=5))

        r = api_client.get("/stores/store-001/anomalies")
        dead = next((a for a in r.json()["active_anomalies"]
                     if a["anomaly_type"] == "DEAD_ZONE"), None)
        assert dead is not None
        assert dead["severity"] == "INFO"
        assert "zone-dead" in dead["description"]

    def test_active_zone_not_flagged_as_dead(self, api_client, db_session):
        _add_event(db_session, event_type="ZONE_ENTER", zone_id="zone-active",
                   timestamp=_ts(hours_ago=1))

        r = api_client.get("/stores/store-001/anomalies")
        dead_zones = [a for a in r.json()["active_anomalies"]
                      if a["anomaly_type"] == "DEAD_ZONE" and "zone-active" in a["description"]]
        assert dead_zones == []

    def test_staff_visits_dont_prevent_dead_zone(self, api_client, db_session):
        _add_event(db_session, event_type="ZONE_ENTER", zone_id="zone-staffonly",
                   is_staff=True, timestamp=_ts(hours_ago=1))
        _add_event(db_session, event_type="ZONE_ENTER", zone_id="zone-staffonly",
                   is_staff=False, timestamp=_ts(hours_ago=6))

        r = api_client.get("/stores/store-001/anomalies")
        dead = next((a for a in r.json()["active_anomalies"]
                     if a["anomaly_type"] == "DEAD_ZONE"
                     and "zone-staffonly" in a["description"]), None)
        assert dead is not None

    def test_anomaly_has_all_required_fields(self, api_client, db_session):
        for _ in range(22):
            _add_event(db_session, event_type="BILLING_QUEUE_JOIN",
                       zone_id="zone-billing", timestamp=_ts(0.5))
        r = api_client.get("/stores/store-001/anomalies")
        anomaly = r.json()["active_anomalies"][0]
        for field in ("anomaly_type", "severity", "description", "suggested_action", "detected_at"):
            assert field in anomaly
