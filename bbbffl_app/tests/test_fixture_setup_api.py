"""End-to-end behaviour for the fixture draw operator workflow (issue #104)."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.fixtures import BASE_ROTATION


@pytest.fixture
def fixture_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)
    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _season_entries(client):
    season_id = client.post("/api/admin/season-centre/seasons", json={"year": 2026, "label": "Replay"}).json()[
        "season_id"
    ]
    ids = []
    for number in range(1, 11):
        coach = client.post(
            "/api/admin/season-centre/coaches", json={"display_name": f"Recognisable Coach {number}"}
        ).json()
        view = client.post(
            f"/api/admin/season-centre/{season_id}/entries",
            json={"coach_id": coach["coach_id"], "team_name": f"Recognisable Team {number}"},
        ).json()
        ids = [entry["season_entry_id"] for entry in view["entries"]]
    return season_id, ids


def test_authorised_operator_sees_ten_named_entries_and_page_explains_independent_draw(fixture_client):
    season_id, _ = _season_entries(fixture_client)
    response = fixture_client.get(f"/api/admin/fixture-setup/{season_id}")
    assert response.status_code == 200
    assert len(response.json()["entries"]) == 10
    assert {entry["coach_display_name"] for entry in response.json()["entries"]} == {
        f"Recognisable Coach {number}" for number in range(1, 11)
    }
    page = fixture_client.get(f"/admin/fixture-setup/{season_id}")
    assert "Fixture-number order is independent of pre-season draft order" in page.text


def test_preview_validates_assignment_and_uses_authoritative_rotation(fixture_client):
    season_id, entry_ids = _season_entries(fixture_client)
    assert (
        fixture_client.post(
            f"/api/admin/fixture-setup/{season_id}/preview",
            json={"entries_by_fixture_number": entry_ids[:-1]},
        ).status_code
        == 422
    )
    assert (
        fixture_client.post(
            f"/api/admin/fixture-setup/{season_id}/preview",
            json={"entries_by_fixture_number": entry_ids[:-1] + [entry_ids[0]]},
        ).status_code
        == 422
    )

    preview = fixture_client.post(
        f"/api/admin/fixture-setup/{season_id}/preview", json={"entries_by_fixture_number": entry_ids}
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["draw"]["state"] == "draft"
    assert all(len(round_["matchups"]) == 5 for round_ in body["rounds"][:9])
    expected_first = [(entry_ids[home - 1], entry_ids[away - 1]) for home, away in BASE_ROTATION[0]]
    assert [
        (match["home_season_entry_id"], match["away_season_entry_id"]) for match in body["rounds"][0]["matchups"]
    ] == expected_first


def test_explicit_freeze_persists_reopens_and_rejects_ordinary_change(fixture_client):
    season_id, entry_ids = _season_entries(fixture_client)
    fixture_client.post(f"/api/admin/fixture-setup/{season_id}/preview", json={"entries_by_fixture_number": entry_ids})
    frozen = fixture_client.post(f"/api/admin/fixture-setup/{season_id}/freeze", json={})
    assert frozen.status_code == 200
    assert frozen.json()["draw"]["state"] == "frozen"
    rejected = fixture_client.post(
        f"/api/admin/fixture-setup/{season_id}/preview",
        json={"entries_by_fixture_number": list(reversed(entry_ids))},
    )
    assert rejected.status_code == 422
    reopened = fixture_client.get(f"/api/admin/fixture-setup/{season_id}").json()
    assert reopened["draw"]["state"] == "frozen"
    assert reopened["fixture_numbers"] == frozen.json()["fixture_numbers"]


def test_scorer_authority_comes_from_shared_active_role_capability(fixture_client):
    season_id, _ = _season_entries(fixture_client)
    response = fixture_client.get(
        f"/api/admin/fixture-setup/{season_id}",
        headers={"X-Admin-Token": "open-mode-operator", "X-Authority-Role": "scorer"},
    )
    assert response.status_code == 200
