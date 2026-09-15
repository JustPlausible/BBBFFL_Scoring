"""Issue #208: `POST /api/admin/finals/{bracket_id}/weeks/{week_number}/
advance-to-review` -- before this route existed, nothing on the HTTP
surface could reach `FinalsBracketRepository.advance_week_to_review`, so
`publish_finals_round` (which requires the round already be `review`) was
unreachable through any supported operator workflow once a finals week had
opened. Also proves the generic ordinary `/signoff` route now refuses a
finals round rather than silently publishing it through the wrong
(ordinary) boundary -- see `app/routes/round_review.py`'s `_require_
ordinary_stream` guard added alongside this route."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.authorization import Principal, Role
from tests.test_scorer_dashboard_finals_superscore import _open_finals_week1_and_superscore1


@pytest.fixture
def finals_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _admin(client):
    from app.authorization import resolve_principal

    principal = Principal(Role.ADMIN, "admin-1", "Admin")
    client.app.dependency_overrides[resolve_principal] = lambda: principal


def test_advance_to_review_moves_an_open_finals_week_to_review(finals_client):
    client = finals_client
    built = _open_finals_week1_and_superscore1(year=9601, database=client.app.state.database)
    _admin(client)

    response = client.post(
        f"/api/admin/finals/{built['bracket'].bracket_id}/weeks/1/advance-to-review", params={"reason": "test"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "review"
    assert body["already_advanced"] is False

    again = client.post(
        f"/api/admin/finals/{built['bracket'].bracket_id}/weeks/1/advance-to-review", params={"reason": "repeat"}
    )
    assert again.status_code == 200
    assert again.json()["already_advanced"] is True


def test_advance_to_review_refuses_a_week_that_has_not_opened(finals_client):
    client = finals_client
    built = _open_finals_week1_and_superscore1(year=9602, database=client.app.state.database)
    _admin(client)

    response = client.post(
        f"/api/admin/finals/{built['bracket'].bracket_id}/weeks/2/advance-to-review", params={"reason": "test"}
    )
    assert response.status_code == 409


def test_generic_signoff_route_refuses_a_finals_round(finals_client):
    client = finals_client
    built = _open_finals_week1_and_superscore1(year=9603, database=client.app.state.database)
    _admin(client)
    client.post(f"/api/admin/finals/{built['bracket'].bracket_id}/weeks/1/advance-to-review", params={"reason": "t"})

    response = client.post(f"/api/admin/round-review/{built['week1_round_id']}/signoff", json={"reason": "test"})
    assert response.status_code == 409
    assert "stream-specific" in response.json()["detail"] or "boundary" in response.json()["detail"]


def test_superscore_advance_to_review_moves_an_open_round_to_review(finals_client):
    """Issue #208 review finding (P1): `SuperScoreLeaderboardService.
    _persist` requires the round already be `review`/`final`, but before
    this route existed nothing on the HTTP surface could reach
    `app.superscore_round.advance_round_to_review` -- only the standalone
    `scripts/superscore_round_2026.py` CLI could -- so the browser Scorer
    workflow's "Publish leaderboard" action always 409'd."""
    client = finals_client
    built = _open_finals_week1_and_superscore1(year=9604, database=client.app.state.database)
    _admin(client)

    response = client.post(
        f"/api/season-superscore/scorer/rounds/{built['ss1_round_id']}/advance-to-review", json={"reason": "test"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "review"

    again = client.post(
        f"/api/season-superscore/scorer/rounds/{built['ss1_round_id']}/advance-to-review", json={"reason": "repeat"}
    )
    assert again.status_code == 200
    assert again.json()["state"] == "review"
