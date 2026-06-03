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
