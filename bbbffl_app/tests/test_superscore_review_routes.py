"""Issue #208: HTTP surface for `app.superscore_review.
SuperScoreReviewRepository` (`app/routes/superscore_review.py`) -- before
this route module existed, a SuperScore DNP/Interchange/override ruling
had no route at all, so a Scorer could never actually record one through
the browser. Proves the entry-scoped endpoints work for a real SuperScore
round/entry, reject an entry that does not belong to that round's review
state, and enforce a stale `expected_review_version`."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.authorization import Principal, Role
from app.season import SeasonCompletedError
from app.superscore_review import SuperScoreReviewRepository
from tests.test_scorer_dashboard_finals_superscore import _open_finals_week1_and_superscore1


@pytest.fixture
def review_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _seed(client, year):
    return _open_finals_week1_and_superscore1(year=year, database=client.app.state.database)


def _admin(client):
    from app.authorization import resolve_principal

    # `session_id=None` (the legacy/no-session credential shape) means the
    # CSRF double-submit check is skipped -- matching every other route
    # module's own test convention for a dependency-overridden principal
    # (see e.g. `app.routes.superscore_results`'s `_csrf`).
    principal = Principal(Role.ADMIN, "admin-1", "Admin")
    client.app.dependency_overrides[resolve_principal] = lambda: principal
    return principal


def test_record_dnp_ruling_for_a_superscore_entry(review_client):
    client = review_client
    built = _seed(client, 9501)
    _admin(client)
    entry_id = built["entries"][0].season_entry_id

    response = client.post(
        f"/api/scorer/superscore/rounds/{built['ss1_round_id']}/entries/{entry_id}/dnp",
        json={"slot": "F1", "dnp": True, "expected_review_version": 0, "reason": "test ruling"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["review_version"] == 1
    assert body["slot_rulings"]["F1"]["dnp"] is True

    repo = SuperScoreReviewRepository(built["database"])
    persisted = repo.get_slot_rulings(built["ss1_round_id"], entry_id)
    assert persisted["F1"].dnp is True


def test_record_interchange_ruling_for_a_superscore_entry(review_client):
    client = review_client
    built = _seed(client, 9502)
    _admin(client)
    entry_id = built["entries"][1].season_entry_id

    response = client.post(
        f"/api/scorer/superscore/rounds/{built['ss1_round_id']}/entries/{entry_id}/interchange",
        json={"target_position": "F1", "expected_review_version": 0, "reason": "test assignment"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["interchange_ruling"]["target_position"] == "F1"


def test_record_override_requires_authorised_role_and_reason(review_client):
    client = review_client
    built = _seed(client, 9503)
    _admin(client)
    entry_id = built["entries"][2].season_entry_id

    missing_reason = client.post(
        f"/api/scorer/superscore/rounds/{built['ss1_round_id']}/entries/{entry_id}/override",
        json={"position": "F1", "override_score": 12.5, "expected_review_version": 0},
    )
    assert missing_reason.status_code == 422

    ok = client.post(
        f"/api/scorer/superscore/rounds/{built['ss1_round_id']}/entries/{entry_id}/override",
        json={"position": "F1", "override_score": 12.5, "reason": "manual correction", "expected_review_version": 0},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["overrides"]["F1"]["override_score"] == 12.5


def test_stale_expected_review_version_is_rejected(review_client):
    client = review_client
    built = _seed(client, 9504)
    _admin(client)
    entry_id = built["entries"][3].season_entry_id

    first = client.post(
        f"/api/scorer/superscore/rounds/{built['ss1_round_id']}/entries/{entry_id}/dnp",
        json={"slot": "F1", "dnp": True, "expected_review_version": 0, "reason": "first"},
    )
    assert first.status_code == 200

    stale = client.post(
        f"/api/scorer/superscore/rounds/{built['ss1_round_id']}/entries/{entry_id}/dnp",
        json={"slot": "F1", "dnp": False, "expected_review_version": 0, "reason": "stale retry"},
    )
    assert stale.status_code == 409


def test_ruling_for_an_entry_outside_the_round_is_rejected(review_client):
    """An entry the round's own `superscore_entry_review_state` set does
    not include (e.g. a foreign season_entry_id) must be refused, never
    silently accepted just because the round_id itself is a real SuperScore
    round."""
    client = review_client
    built = _seed(client, 9505)
    _admin(client)

    response = client.post(
        f"/api/scorer/superscore/rounds/{built['ss1_round_id']}/entries/not-a-real-entry/dnp",
        json={"slot": "F1", "dnp": True, "expected_review_version": 0, "reason": "test"},
    )
    assert response.status_code == 404


def test_ruling_endpoints_translate_the_completed_season_write_fence_to_423(review_client, monkeypatch):
    """Issue #208 review finding (P2): `SuperScoreReviewRepository`'s
    `_guard_season_writable` can raise `SeasonCompletedError` for any of
    these three writes, but the routes previously caught only validation,
    unknown-state and stale-version errors -- letting it escape as an
    unhandled 500 instead of the 423 the existing calculate/publish routes
    already return for the identical fence."""
    client = review_client
    built = _seed(client, 9507)
    _admin(client)
    entry_id = built["entries"][0].season_entry_id

    def _raise(*args, **kwargs):
        raise SeasonCompletedError("season is completed")

    monkeypatch.setattr(SuperScoreReviewRepository, "record_dnp_ruling", _raise)
    monkeypatch.setattr(SuperScoreReviewRepository, "record_interchange_ruling", _raise)
    monkeypatch.setattr(SuperScoreReviewRepository, "record_override", _raise)

    dnp = client.post(
        f"/api/scorer/superscore/rounds/{built['ss1_round_id']}/entries/{entry_id}/dnp",
        json={"slot": "F1", "dnp": True, "expected_review_version": 0, "reason": "test"},
    )
    assert dnp.status_code == 423

    interchange = client.post(
        f"/api/scorer/superscore/rounds/{built['ss1_round_id']}/entries/{entry_id}/interchange",
        json={"target_position": "F1", "expected_review_version": 0},
    )
    assert interchange.status_code == 423

    override = client.post(
        f"/api/scorer/superscore/rounds/{built['ss1_round_id']}/entries/{entry_id}/override",
        json={"position": "F1", "override_score": 1.0, "reason": "test", "expected_review_version": 0},
    )
    assert override.status_code == 423


def test_ruling_endpoints_reject_a_finals_round(review_client):
    """These entry-scoped endpoints belong to SuperScore alone -- a finals
    round_id (which has no `superscore_entry_review_state` rows at all)
    must 404, never silently accept it."""
    client = review_client
    built = _seed(client, 9506)
    _admin(client)
    entry_id = built["entries"][0].season_entry_id

    response = client.get(f"/api/scorer/superscore/rounds/{built['week1_round_id']}/entries/{entry_id}")
    assert response.status_code == 404
