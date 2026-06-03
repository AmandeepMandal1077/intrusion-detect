# Store Intelligence API — Architecture Decision Log

### FastAPI over Flask/Django
**Decision**: Use FastAPI as the web framework.
**Rationale**: Native async support, first-class Pydantic v2 integration,
auto-generated OpenAPI docs out of the box. This is critical for a high-throughput
event ingestion pipeline that will receive bursts from multiple camera feeds.
