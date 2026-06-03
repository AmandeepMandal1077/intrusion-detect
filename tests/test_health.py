# PROMPT: Write pytest tests for a FastAPI /health and / root endpoint.
#         The app must return 200 with structured JSON payloads.
# CHANGES MADE: Added root endpoint test; assert exact keys in health response.

import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_root_returns_200():
    """Root endpoint should return 200 with a welcome message."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "message" in data
    assert data["message"] == "Store Intelligence API is live."


def test_health_returns_200():
    """Health endpoint should return 200."""
    response = client.get("/health")
    assert response.status_code == 200


def test_health_response_structure():
    """Health response must contain status, version, and service keys."""
    response = client.get("/health")
    data = response.json()
    assert data["status"] == "healthy"
    assert "version" in data
    assert "service" in data
    assert data["service"] == "store-intelligence-api"


def test_health_version_format():
    """Version should follow semver format (e.g., 0.1.0)."""
    response = client.get("/health")
    version = response.json()["version"]
    parts = version.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
