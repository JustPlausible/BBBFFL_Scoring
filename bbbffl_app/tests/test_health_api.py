"""Issue #243: `/health` stays a pure liveness check; `/health/ready` is the
new, separate dependency-readiness check (database + afl-api, the latter
only when afl_mode == "live"). No test here ever makes a real network call
-- afl-api connectivity is simulated entirely through `FakeAflClient.
connectivity_error` / a fake transport, per app/routes/health.py's own
docstring on why replay/live gating keeps this deterministic.
"""

import os
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeAflClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BBBFFL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AFL_API_BASE_URL", "http://unused.invalid")
    monkeypatch.setenv("BBBFFL_ADMIN_TOKEN", "secret123")
    monkeypatch.setenv(
        "BBBFFL_TEAMS_CONFIG_PATH",
        os.path.join(os.path.dirname(__file__), "..", "data", "grand_final_teams.json"),
    )

    from app.main import app

    with TestClient(app) as test_client:
        fake = FakeAflClient([], {})
        app.state.afl_client = fake
        app.state.fake_afl_client = fake
        yield test_client


def test_health_is_a_pure_liveness_check(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_ok_when_database_and_afl_api_are_healthy(client):
    response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == {"status": "ok"}
    assert body["checks"]["afl_api"] == {"status": "ok"}


def test_readiness_fails_when_database_is_unavailable(client):
    from app.main import app

    class _BrokenDatabase:
        def execute_bounded(self, statement, parameters=(), timeout_seconds=None):
            raise RuntimeError("connection refused")

    app.state.database = _BrokenDatabase()

    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    # Only the exception *type* is reported for a database failure -- never
    # its message, which is not guaranteed secret-safe (see health.py's
    # module docstring).
    assert body["checks"]["database"] == {"status": "error", "detail": "RuntimeError"}
    assert body["checks"]["afl_api"] == {"status": "ok"}


def test_readiness_fails_when_afl_api_is_unavailable_and_afl_mode_is_live(client):
    from app.main import app

    app.state.fake_afl_client.connectivity_error = ConnectionError("afl-api connection failed: GET /api/v1")

    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["checks"]["database"] == {"status": "ok"}
    assert body["checks"]["afl_api"]["status"] == "error"
    assert "afl-api connection failed" in body["checks"]["afl_api"]["detail"]


def test_readiness_never_calls_afl_api_in_replay_mode(client):
    """Replay/deterministic execution must never make a live afl-api call --
    including from the readiness check itself. A transport with no
    check_connectivity() at all proves this: calling it would raise
    AttributeError, which would surface as a 503 database-shaped failure, not
    a passing "skipped" afl_api check."""
    from app.main import app

    class _NoConnectivityMethodTransport:
        pass

    app.state.settings = replace(app.state.settings, afl_mode="replay")
    app.state.afl_client = _NoConnectivityMethodTransport()

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["checks"]["afl_api"] == {"status": "skipped", "detail": "afl_mode=replay"}


def test_readiness_is_bounded_by_a_timeout_and_does_not_hang(client):
    from app.main import app

    class _SlowDatabase:
        def execute_bounded(self, statement, parameters=(), timeout_seconds=None):
            time.sleep(1.0)

    app.state.database = _SlowDatabase()
    app.state.settings = replace(app.state.settings, readiness_timeout_seconds=0.05)

    started = time.monotonic()
    response = client.get("/health/ready")
    elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json()["checks"]["database"] == {"status": "error", "detail": "TimeoutError"}
    # Comfortably below the 1s the fake database sleeps for -- proves the
    # response itself was bounded by the configured timeout, not by the
    # slow dependency.
    assert elapsed < 0.5
