# Store Intelligence API — Architecture Decision Log

### FastAPI over Flask/Django
**Decision**: Use FastAPI as the web framework.
**Rationale**: Native async support, first-class Pydantic v2 integration,
auto-generated OpenAPI docs out of the box. This is critical for a high-throughput
event ingestion pipeline that will receive bursts from multiple camera feeds.

### Database: PostgreSQL + SQLAlchemy (Sync)

**Decision**: Use PostgreSQL via SQLAlchemy 2.x with synchronous sessions.

**Rationale**:
- PostgreSQL gives us proper `UNIQUE` constraints on `event_id`, enforcing
  idempotency at the DB level (`INSERT ... ON CONFLICT DO NOTHING` semantics via
  pre-query deduplication).
- SQLAlchemy sync sessions are simpler to reason about and test. The ingestion
  endpoint does not need sub-millisecond latency — it batches events. Async can
  be introduced in later stages when we need horizontal scaling.
- SQLite is used in tests via `conftest.py` fixture so no Postgres instance is
  required to run the test suite.

**Why not async SQLAlchemy yet?**
Async SQLAlchemy requires `asyncpg` and more complex lifespan management.
Defer this until the CV pipeline introduces high-concurrency
event bursts that actually stress the event loop.

### POS Correlation Logic

**Rule**: A visitor counts as "converted" if they had a `BILLING_QUEUE_JOIN`
or `ZONE_ENTER` event within the 5-minute window **before** a POS transaction
timestamp at the same store.

**Why 5 minutes?**
This is the typical time between queue entry and cashier transaction completion
for a mid-sized retail checkout. It is exposed as a configurable constant
(`CORRELATION_WINDOW_MINUTES = 5`) so it can be tuned per store type.

**Re-entry deduplication**: All visitor counts use `DISTINCT visitor_id`,
so a customer entering, leaving, and re-entering counts as one unique visitor.
