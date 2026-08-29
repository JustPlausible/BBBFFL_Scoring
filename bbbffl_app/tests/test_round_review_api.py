"""The scorer round-review/sign-off/correction workflow driven entirely
through the real HTTP admin API (roadmap package 28, issue #58) -- proves
the wiring in app.main/app.routes.round_review works end-to-end, not just
that app.round_review's functions do (see tests/test_round_review.py for
the exhaustive domain-level coverage this complements).

Uses its own isolated SQLite database, exactly like
tests/test_preseason_api.py, so it cannot contaminate any other test's
state.
"""

import json
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
            # Client input cannot elevate the authenticated scorer in audit
            # provenance; the route accepts this legacy field but ignores it.
            "actor_role": "admin",
        },
        headers={"X-Admin-Token": "open-mode", "X-Authority-Role": "scorer"},
    )
    assert override_resp.status_code == 200, override_resp.text
    provenance = client.app.state.database.execute(
        "SELECT decided_by_role FROM bbbffl_matchup_override "
        "WHERE matchup_id=? AND season_entry_id=? AND position='F3'",
        (matchup["matchup_id"], home_entry),
    ).fetchone()
    assert provenance["decided_by_role"] == "scorer"

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


def test_regular_season_round_centre_browser_shell_and_authoritative_context(review_client):
    """Smoke the browser entry point and the authoritative model it renders."""
    client = review_client
    round_, _, _, _ = _seed(client, 8810)
    round_id = round_.bbbffl_round_id

    page = client.get(f"/scorer/round-centre/{round_id}")
    assert page.status_code == 200
    assert "Regular-season Round Centre" in page.text
    assert "Legacy Grand Final admin" in page.text
    assert "expected_review_version" in page.text
    assert "Conflict — this page was stale" in page.text

    available = client.get("/api/admin/round-review").json()
    assert [item["bbbffl_round_id"] for item in available] == [round_id]
    review = client.get(f"/api/admin/round-review/{round_id}").json()
    assert len(review["matchups"]) == 5
    assert review["identity"]["fixture_round_number"] == 1
    assert review["replay"]["classification"] == "live evidence"
    assert all("official_history" in matchup for matchup in review["matchups"])

    signed = client.post(f"/api/admin/round-review/{round_id}/signoff", json={"reason": "browser smoke"})
    assert signed.status_code == 200, signed.text
    published = client.get(f"/api/admin/round-review/{round_id}").json()
    assert published["state"] == "final"
    assert all(matchup["official_result"]["version"] == 1 for matchup in published["matchups"])
    assert published["ladder"]["through_round"] == 1
    assert len(published["ladder"]["rows"]) == 10


def test_round_centre_api_rejects_stale_browser_ruling_and_returns_current_state(review_client):
    client = review_client
    round_, _, _, _ = _seed(client, 8811)
    api = f"/api/admin/round-review/{round_.bbbffl_round_id}"
    matchup = client.get(api).json()["matchups"][0]
    payload = {
        "matchup_id": matchup["matchup_id"],
        "season_entry_id": matchup["home"]["season_entry_id"],
        "slot": "F1",
        "dnp": False,
        "expected_review_version": matchup["review_version"],
        "reason": "browser ruling",
    }
    assert client.post(f"{api}/dnp", json=payload).status_code == 200
    conflict = client.post(f"{api}/dnp", json=payload)
    assert conflict.status_code == 409
    assert "not the expected" in conflict.json()["detail"]
    current = client.get(api).json()["matchups"][0]
    assert current["review_version"] == matchup["review_version"] + 1


def test_round_centre_resolves_ambiguous_interchange_dnp_through_existing_ruling_api(review_client):
    client = review_client
    round_, _, entries, canon = _seed(client, 8812)
    round_id = round_.bbbffl_round_id
    entry_id = entries[0].season_entry_id
    interchange_player = canon[(entry_id, "Interchange")]
    client.app.state.afl_client.stats.pop(interchange_player)
    client.app.state.calculations.calculate_round(round_id)

    api = f"/api/admin/round-review/{round_id}"
    blocked = client.get(api).json()
    matchup = next(
        m for m in blocked["matchups"] if entry_id in (m["home"]["season_entry_id"], m["away"]["season_entry_id"])
    )
    side_key = "home" if matchup["home"]["season_entry_id"] == entry_id else "away"
    side = matchup[side_key]
    assert side["interchange"]["dnp_recommendation"] == "review_required"
    assert side["interchange"]["dnp_ruling"] is None
    assert any("Interchange: DNP status unresolved" in blocker for blocker in matchup["blockers"])
    page = client.get(f"/scorer/round-centre/{round_id}")
    assert 'data-slot="Interchange"' in page.text
    assert "Confirm Interchange DNP" in page.text

    resolved = client.post(
        f"{api}/dnp",
        json={
            "matchup_id": matchup["matchup_id"],
            "season_entry_id": entry_id,
            "slot": "Interchange",
            "dnp": False,
            "expected_review_version": matchup["review_version"],
            "reason": "confirmed as participating by scorer",
        },
    )
    assert resolved.status_code == 200, resolved.text
    updated = next(m for m in resolved.json()["matchups"] if m["matchup_id"] == matchup["matchup_id"])
    assert updated[side_key]["interchange"]["dnp_ruling"] is False
    assert not any("Interchange: DNP status unresolved" in blocker for blocker in updated["blockers"])
    assert resolved.json()["ready_for_signoff"] is True
    assert client.post(f"{api}/signoff", json={"reason": "all blockers resolved"}).status_code == 200


def test_round_centre_exposes_actual_mixed_slot_source_not_only_round_mapping(review_client):
    client = review_client
    round_, lifecycle, _, _ = _seed(client, 8813)
    round_id = round_.bbbffl_round_id
    matchup = lifecycle.list_matchups(round_id)[0]
    row = client.app.state.database.execute(
        "SELECT snapshot FROM bbbffl_matchup_calculation WHERE matchup_id=?", (matchup.matchup_id,)
    ).fetchone()
    snapshot = json.loads(row["snapshot"])
    deferred = snapshot["home"]["slots"][0]
    deferred.update(
        scoring_source="opening_round_deferred",
        source_afl_round_id=88,
        afl_match_id=88001,
    )
    client.app.state.database.execute(
        "UPDATE bbbffl_matchup_calculation SET snapshot=? WHERE matchup_id=?",
        (json.dumps(snapshot), matchup.matchup_id),
    )

    review = client.get(f"/api/admin/round-review/{round_id}").json()
    displayed = review["matchups"][0]["home"]["slots"][0]
    assert review["identity"]["afl_round_id"] == 100
    assert displayed["scoring_source"] == "opening_round_deferred"
    assert displayed["source_afl_round_id"] == 88
    assert displayed["source_afl_match_id"] == 88001
    assert displayed["source_afl_round_id"] != review["identity"]["afl_round_id"]

    page = client.get(f"/scorer/round-centre/{round_id}")
    assert "each player card shows its actual evidence source" in page.text
    assert "source_afl_round_id" in page.text
