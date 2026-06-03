"""
Database engine, session factory, ORM models, and POS CSV seeder.
"""

from __future__ import annotations

import csv
import os
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv()

DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/store_intelligence",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


# ORM: Events

class EventORM(Base):
    """Mirrors the StoreEvent Pydantic schema.

    event_id is stored as a VARCHAR(36) UUID string so the table is portable
    between PostgreSQL and the SQLite test database.
    """

    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("event_id", name="uq_events_event_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(36), nullable=False, index=True)
    store_id = Column(String(100), nullable=False, index=True)
    camera_id = Column(String(100), nullable=False)
    visitor_id = Column(String(100), nullable=False, index=True)
    event_type = Column(String(50), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    zone_id = Column(String(100), nullable=True)
    dwell_ms = Column(Integer, default=0, nullable=False)
    is_staff = Column(Boolean, default=False, nullable=False)
    confidence = Column(Float, nullable=False)
    event_metadata = Column(JSON, default=dict, nullable=False)
    session_seq = Column(Integer, nullable=False)


# ORM: POS Transactions

class POSTransactionORM(Base):
    """A retail point-of-sale transaction record, seeded from pos_transactions.csv."""

    __tablename__ = "pos_transactions"
    __table_args__ = (
        UniqueConstraint("transaction_id", name="uq_pos_transaction_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    transaction_id = Column(String(100), nullable=False, index=True)
    store_id = Column(String(100), nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    amount = Column(Float, nullable=True)
    items_count = Column(Integer, nullable=True)


# DB lifecycle helpers

def init_db() -> None:
    """Create all tables. Safe to call multiple times (CREATE IF NOT EXISTS)."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency: provide a session and guarantee it is closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# POS CSV Seeder

def seed_pos_from_csv(
    csv_path: str,
    store_id_override: str | None = None,
) -> dict[str, int]:
    """
    Idempotently seed pos_transactions from a CSV file.

    Expected columns (header row required):
        transaction_id, store_id, timestamp, amount, items_count

    Args:
        csv_path: Path to the CSV file.
        store_id_override: Used when the CSV lacks a store_id column.

    Returns:
        {"inserted": int, "skipped": int}
    """
    from dateutil import parser as du_parser

    db = SessionLocal()
    inserted = 0
    skipped = 0

    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sid = (row.get("store_id") or "").strip() or store_id_override
                tid = (row.get("transaction_id") or "").strip()

                if not sid or not tid:
                    skipped += 1
                    continue

                # Idempotency check
                if db.query(POSTransactionORM).filter_by(transaction_id=tid).first():
                    skipped += 1
                    continue

                ts_raw = (row.get("timestamp") or "").strip()
                try:
                    ts = du_parser.isoparse(ts_raw)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except Exception:
                    skipped += 1
                    continue

                amount = None
                try:
                    amount = float(row.get("amount") or 0)
                except (ValueError, TypeError):
                    pass

                items = None
                try:
                    items = int(row.get("items_count") or 0)
                except (ValueError, TypeError):
                    pass

                db.add(
                    POSTransactionORM(
                        transaction_id=tid,
                        store_id=sid,
                        timestamp=ts,
                        amount=amount,
                        items_count=items,
                    )
                )
                inserted += 1

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return {"inserted": inserted, "skipped": skipped}
