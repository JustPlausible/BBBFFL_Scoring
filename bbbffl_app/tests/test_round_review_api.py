"""The scorer round-review/sign-off/correction workflow driven entirely
through the real HTTP admin API (roadmap package 28, issue #58) -- proves
the wiring in app.main/app.routes.round_review works end-to-end, not just
that app.round_review's functions do (see tests/test_round_review.py for
the exhaustive domain-level coverage this complements).

Uses its own isolated SQLite database, exactly like
tests/test_preseason_api.py, so it cannot contaminate any other test's
state.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.calculations import MatchupCalculationService
from tests.round_review_helpers import Facts, full_round, progress_to_review


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
    database = client.app.state.database
    db, lifecycle, round_, entries, stats, canon = full_round(database, year=year)
    progress_to_review(lifecycle, round_.bbbffl_round_id)
    fake_calculations = MatchupCalculationService(database, Facts(stats))
    fake_calculations.calculate_round(round_.bbbffl_round_id)
    client.app.state.calculations = fake_calculations
    client.app.state.afl_client = Facts(stats)
    return round_, lifecycle, entries, canon


def test_round_review_workflow_end_to_end_via_the_admin_api(review_client):
    client = review_client
    round_, lifecycle, entries, canon = _seed(client, 8801)
    round_id = round_.bbbffl_round_id
    api = f"/api/admin/round-review/{round_id}"

    review = client.get(api).json()
    assert len(review["matchups"]) == 5
    assert review["ready_for_signoff"] is True

    matchup = review["matchups"][0]
    home_entry = matchup["home"]["season_entry_id"]

    dnp_resp = client.post(
        f"{api}/dnp",
        json={
            "matchup_id": matchup["matchup_id"],
            "season_entry_id": home_entry,
            "slot": "F2",
            "dnp": True,
            "expected_review_version": matchup["review_version"],
            "reason": "late withdrawal",
        },
    )
    assert dnp_resp.status_code == 200, dnp_resp.text
    updated_matchup = next(m for m in dnp_resp.json()["matchups"] if m["matchup_id"] == matchup["matchup_id"])
    assert updated_matchup["review_version"] == matchup["review_version"] + 1

    override_resp = client.post(
        f"{api}/override",
        json={
            "matchup_id": matchup["matchup_id"],
            "season_entry_id": home_entry,
            "position": "F3",
            "override_score": 42.0,
            "calculated_score": 20.0,
            "reason": "adjudicated correction",
            "expected_review_version": updated_matchup["review_version"],
            "actor_role": "scorer",
        },
    )
    assert override_resp.status_code == 200, override_resp.text

    unauthorised = client.post(
        f"{api}/override",
        json={
            "matchup_id": matchup["matchup_id"],
            "season_entry_id": home_entry,
            "position": "Ruck",
            "override_score": 10.0,
            "calculated_score": 5.0,
            "reason": "not allowed",
            "expected_review_version": override_resp.json()["matchups"][0]["review_version"],
            "actor_role": "coach",
        },
    )
    assert unauthorised.status_code == 403

    missing_reason = client.post(
        f"{api}/override",
        json={
            "matchup_id": matchup["matchup_id"],
            "season_entry_id": home_entry,
            "position": "Ruck",
            "override_score": 10.0,
            "calculated_score": 5.0,
            "expected_review_version": override_resp.json()["matchups"][0]["review_version"],
        },
    )
    assert missing_reason.status_code == 400

    still_blocked = client.get(api).json()
    assert still_blocked["ready_for_signoff"] is False  # F2 DNP still needs an interchange decision

    reviewed_matchup = next(m for m in still_blocked["matchups"] if m["matchup_id"] == matchup["matchup_id"])
    interchange_resp = client.post(
        f"{api}/interchange",
        json={
            "matchup_id": matchup["matchup_id"],
            "season_entry_id": home_entry,
            "target_position": "F2",
            "expected_review_version": reviewed_matchup["review_version"],
            "reason": "cover with interchange",
        },
    )
    assert interchange_resp.status_code == 200, interchange_resp.text

    ready = client.get(api).json()
    assert ready["ready_for_signoff"] is True

    signoff_resp = client.post(f"{api}/signoff", json={"reason": "round complete", "scorer_name": "Alex"})
    assert signoff_resp.status_code == 200, signoff_resp.text
    assert signoff_resp.json()["state"] == "final"

    history_url = f"/api/admin/round-review/matchup/{matchup['matchup_id']}/history"
    history_resp = client.get(history_url)
    assert [h["version"] for h in history_resp.json()] == [1]
    assert history_resp.json()[0]["input_snapshot"] is not None

    stale_signoff = client.post(f"{api}/signoff", json={"reason": "double sign-off"})
    assert stale_signoff.status_code == 409

    correct_resp = client.post(
        f"/api/admin/round-review/matchup/{matchup['matchup_id']}/correct",
        json={"reason": "post-publication transcription fix", "scorer_name": "Alex"},
    )
    assert correct_resp.status_code == 200, correct_resp.text
    assert correct_resp.json()["version"] == 2

    history_after = client.get(history_url).json()
    assert [h["version"] for h in history_after] == [1, 2]
    assert history_after[0]["home_score"] == history_resp.json()[0]["home_score"]


def test_incomplete_round_signoff_returns_409_with_blockers(review_client):
    client = review_client
    round_, lifecycle, entries, canon = _seed(client, 8802)
    round_id = round_.bbbffl_round_id
    # Skip the recalculation the fixture normally does at signoff time by
    # pointing state.calculations at a service with no persisted snapshot.
    from app.db import transaction

    matchup = lifecycle.list_matchups(round_id)[0]
    with transaction(client.app.state.database) as conn:
        conn.execute("DELETE FROM bbbffl_matchup_calculation WHERE matchup_id=?", (matchup.matchup_id,))

    class NoRecalculation:
        def calculate_round(self, round_id):
            return []

    client.app.state.calculations = NoRecalculation()
    resp = client.post(f"/api/admin/round-review/{round_id}/signoff", json={"reason": "try anyway"})
    assert resp.status_code == 409
    body = resp.json()
    assert matchup.matchup_id in body["blockers"]


def test_correction_recomputes_and_checks_evidence_freshness_before_publishing(review_client):
    """Regression: the /correct endpoint used to freeze a new official
    version straight from whatever was last calculated, without the
    /signoff endpoint's recompute-and-check-freshness step -- so a stale
    afl-api fact could be corrected into official history unnoticed."""
    client = review_client
    round_, lifecycle, entries, canon = _seed(client, 8803)
    round_id = round_.bbbffl_round_id
    api = f"/api/admin/round-review/{round_id}"

    signoff_resp = client.post(f"{api}/signoff", json={"reason": "round complete"})
    assert signoff_resp.status_code == 200, signoff_resp.text
    matchup_id = lifecycle.list_matchups(round_id)[0].matchup_id

    # afl-api evidence is now stale for any further calculation.
    client.app.state.afl_client.is_evidence_fresh = lambda: False

    resp = client.post(
        f"/api/admin/round-review/matchup/{matchup_id}/correct",
        json={"reason": "attempted correction on stale evidence"},
    )
    assert resp.status_code == 409
    assert "evidence" in resp.json()["blockers"][matchup_id][0]
    # Nothing was published: version 1 remains the only, effective version.
    history = client.get(f"/api/admin/round-review/matchup/{matchup_id}/history").json()
    assert [h["version"] for h in history] == [1]


def test_ruling_for_a_matchup_from_a_different_round_is_rejected(review_client):
    """Regression: the URL's round_id used to be decorative -- a payload
    naming a matchup that belongs to a *different* round would still be
    mutated, and the response (rebuilt from the URL's round_id) would not
    even show it."""
    client = review_client
    round_a, lifecycle, entries, canon = _seed(client, 8804)
    round_b, _, _, _ = _seed(client, 8805)
    other_round_matchup = lifecycle.list_matchups(round_b.bbbffl_round_id)[0]

    resp = client.post(
        f"/api/admin/round-review/{round_a.bbbffl_round_id}/dnp",
        json={
            "matchup_id": other_round_matchup.matchup_id,
            "season_entry_id": other_round_matchup.home_season_entry_id,
            "slot": "F1",
            "dnp": True,
            "expected_review_version": 1,
            "reason": "should be rejected",
        },
    )
    assert resp.status_code == 404
    # The mutation must not have committed against the other round's matchup.
    from app.round_review import RoundReviewRepository

    assert RoundReviewRepository(client.app.state.database).get_slot_rulings(other_round_matchup.matchup_id) == {}
