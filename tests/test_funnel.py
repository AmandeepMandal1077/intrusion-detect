# File path: tests/test_funnel.py

# PROMPT: Write pytest tests for GET /stores/{id}/funnel using SQLite in-memory DB.
#         Seed EventORM and POSTransactionORM rows to verify funnel step counts and
#         drop-off percentage calculations, including the POS correlation logic.
# CHANGES MADE: Fixed datetime.utcnow() → datetime.now(timezone.utc) to fix
#               timezone mismatch with SQLite string comparison; added timezone import.

import uuid
from datetime import datetime, timezone, timedelta

import pytest

from app.database import EventORM, POSTransactionORM


def _ts(minutes_ago: int = 0) -> datetime:
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


class TestFunnel:
    def test_empty_store_all_zeros(self, api_client):
        r = api_client.get("/stores/store-001/funnel")
        assert r.status_code == 200
        for step in r.json()["funnel"]:
            assert step["count"] == 0

    def test_funnel_step_structure(self, api_client):
        r = api_client.get("/stores/store-001/funnel")
        steps = [s["step"] for s in r.json()["funnel"]]
        assert steps == ["Entry", "Zone Visit", "Billing Queue", "Purchase"]

    def test_entry_count(self, api_client, db_session):
        vid = str(uuid.uuid4())
        _add_event(db_session, visitor_id=vid, event_type="ENTRY")
        _add_event(db_session, visitor_id=vid, event_type="REENTRY")
        _add_event(db_session, visitor_id=str(uuid.uuid4()), event_type="ENTRY")

        r = api_client.get("/stores/store-001/funnel")
        assert r.json()["funnel"][0]["count"] == 2

    def test_zone_visit_count(self, api_client, db_session):
        vid = str(uuid.uuid4())
        _add_event(db_session, visitor_id=vid, event_type="ZONE_ENTER", zone_id="zone-a")
        _add_event(db_session, visitor_id=vid, event_type="ZONE_ENTER", zone_id="zone-b")

        r = api_client.get("/stores/store-001/funnel")
        assert r.json()["funnel"][1]["count"] == 1

    def test_purchase_correlation_within_window(self, api_client, db_session):
        vid = str(uuid.uuid4())
        trx_time = datetime.now(timezone.utc)
        _add_event(db_session, visitor_id=vid, event_type="BILLING_QUEUE_JOIN",
                   zone_id="zone-billing",
                   timestamp=trx_time - timedelta(minutes=3))
        _add_transaction(db_session, timestamp=trx_time)

        r = api_client.get("/stores/store-001/funnel")
        assert r.json()["funnel"][3]["count"] == 1

    def test_purchase_outside_correlation_window_not_counted(self, api_client, db_session):
        vid = str(uuid.uuid4())
        trx_time = datetime.now(timezone.utc)
        _add_event(db_session, visitor_id=vid, event_type="BILLING_QUEUE_JOIN",
                   zone_id="zone-billing",
                   timestamp=trx_time - timedelta(minutes=10))
        _add_transaction(db_session, timestamp=trx_time)

        r = api_client.get("/stores/store-001/funnel")
        assert r.json()["funnel"][3]["count"] == 0

    def test_drop_off_first_step_is_zero(self, api_client, db_session):
        _add_event(db_session, event_type="ENTRY")
        r = api_client.get("/stores/store-001/funnel")
        assert r.json()["funnel"][0]["drop_off_pct"] == 0.0

    def test_drop_off_calculation(self, api_client, db_session):
        for _ in range(10):
            _add_event(db_session, event_type="ENTRY")
        for _ in range(5):
            _add_event(db_session, event_type="ZONE_ENTER", zone_id="zone-a")

        r = api_client.get("/stores/store-001/funnel")
        assert r.json()["funnel"][1]["drop_off_pct"] == pytest.approx(50.0, abs=1)

    def test_staff_excluded_from_funnel(self, api_client, db_session):
        _add_event(db_session, event_type="ENTRY", is_staff=True)
        r = api_client.get("/stores/store-001/funnel")
        assert r.json()["funnel"][0]["count"] == 0

    def test_correlation_window_minutes_in_response(self, api_client):
        r = api_client.get("/stores/store-001/funnel")
        assert r.json()["correlation_window_minutes"] == 5
