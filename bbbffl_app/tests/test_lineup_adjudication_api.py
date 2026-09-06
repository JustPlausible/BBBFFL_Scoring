"""HTTP-level coverage for issue #146's Scorer/Admin missed-submission
adjudication surface (`app/routes/lineup_adjudication.py`), exercised
through the real `/api/admin/lineup-adjudication` API via `app.main.app`.
Reuses `tests/test_lineup_correction_api.py`'s authenticated acting-context
helpers -- the same season-scoped Scorer/coach session flow, a different
operations surface. Lockout/evidence mechanics themselves are covered
exhaustively at the domain level in tests/test_lineup_adjudication.py; this
file is only auth/CSRF/season-scoping/response-shape wiring.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.afl_client import Match, Team
from app.audit import ActorContext
from app.lineups import WeeklyLineupRepository
from app.lockouts import LockoutTriggerRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from tests.test_lineup_correction_api import (
    _authenticate_plain_coach,
    _authenticate_scorer,
    _setup_round,
)

ADMIN = ActorContext.anonymous_operator("admin")
FUTURE_LIVE_MATCH_ID = 555001
FUTURE_LIVE_TEAM_ID = 555002


class _OneAlreadyLiveMatchAflClient:
    """A single AFL match, scheduled in the future but already reporting
    status LIVE -- `evaluate_match_lock` locks on status regardless of
    scheduled time (see app/lockouts.py), so the configured trigger
    activates deterministically without depending on wall-clock timing in
    this test."""

    def get_matches(self, round_id):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        return [
            Match(
                match_id=FUTURE_LIVE_MATCH_ID,
                home_team=Team(FUTURE_LIVE_TEAM_ID, "Adjudication FC"),
                away_team=Team(FUTURE_LIVE_TEAM_ID + 1, "Adjudication Opp"),
                status="LIVE",
                start_time_utc=future,
            )
        ]


@pytest.fixture
def adjudication_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        monkeypatch.setattr(client.app.state, "afl_client", _OneAlreadyLiveMatchAflClient())
        yield client
    db_path.unlink(missing_ok=True)


def _prepare_missed_submission(db, scope, round_id, entry, lifecycle):
    """A `live` round, one selective trigger on the always-locked match,
    a saved private draft naming that match's player, and no effective
    submission -- issue #146's missed-submission scenario, ready for the
    API to adjudicate."""
    OwnershipRepository(db).configure_squad_limit(scope["season_id"], 10)
    player = PlayerPoolRepository(db).refresh_player(
        scope["season_id"], 900001, "API Fixture Player", afl_team_id=FUTURE_LIVE_TEAM_ID
    )
    OwnershipRepository(db).acquire(player.season_player_id, entry.season_entry_id)
    lineups = WeeklyLineupRepository(db)
    lineups.save_draft(
        scope["season_id"],
        scope["competition_id"],
        round_id,
        entry.season_entry_id,
        {"F1": player.season_player_id},
        expected_revision=0,
    )
    LockoutTriggerRepository(db).create(
        round_id, "early-1", "selective", 1, [FUTURE_LIVE_MATCH_ID], reason="api fixture"
    )
    lifecycle.transition(round_id, "live")
    return player


def test_scorer_can_view_and_accept_evidenced_draft_end_to_end(adjudication_client):
    client = adjudication_client
    db = client.app.state.database
    round_, entries, scope, lifecycle = _setup_round(db, 2901, 2901)
    entry = entries[0]
    player = _prepare_missed_submission(db, scope, round_.bbbffl_round_id, entry, lifecycle)

    _operator, cookies, headers = _authenticate_scorer(client, scope["season_id"])

    listing = client.get(f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}", cookies=cookies, headers=headers)
    assert listing.status_code == 200
    listed_entry = next(e for e in listing.json()["entries"] if e["season_entry_id"] == entry.season_entry_id)
    assert listed_entry["has_effective_submission"] is False

    detail = client.get(
        f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}/{entry.season_entry_id}",
        cookies=cookies,
        headers=headers,
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["eligible"] is True
    assert len(body["activated_triggers"]) == 1
    f1_slot = next(s for s in body["evidenced_preview"] if s["position"] == "F1")
    assert f1_slot["evidence_status"] == "proven_pre_lock"
    assert f1_slot["season_player_id"] == player.season_player_id

    accepted = client.post(
        f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}/{entry.season_entry_id}/accept-evidenced-draft",
        json={"reason": "League chat confirmed the coach's pre-lockout draft; quorum accepted it"},
        cookies=cookies,
        headers=headers,
    )
    assert accepted.status_code == 200
    result = accepted.json()
    assert result["submission"]["version"] == 1
    assert result["submission"]["source_type"] == "scorer_late_capture"
    assert result["submission"]["positions"]["F1"] == player.season_player_id
    assert result["adjudication"]["decision_type"] == "accept_evidenced_draft"

    # A second attempt against the now-submitted lineup is refused.
    second = client.post(
        f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}/{entry.season_entry_id}/accept-evidenced-draft",
        json={"reason": "attempted again after a submission already exists"},
        cookies=cookies,
        headers=headers,
    )
    assert second.status_code == 409


def test_missing_reason_is_rejected_by_the_api(adjudication_client):
    client = adjudication_client
    db = client.app.state.database
    round_, entries, scope, lifecycle = _setup_round(db, 2902, 2902)
    entry = entries[0]
    _prepare_missed_submission(db, scope, round_.bbbffl_round_id, entry, lifecycle)

    _operator, cookies, headers = _authenticate_scorer(client, scope["season_id"])
    response = client.post(
        f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}/{entry.season_entry_id}/accept-evidenced-draft",
        json={"reason": "   "},
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 400


def test_unauthorized_coach_cannot_access_adjudication_endpoints(adjudication_client):
    client = adjudication_client
    db = client.app.state.database
    round_, entries, scope, lifecycle = _setup_round(db, 2903, 2903)
    entry = entries[0]

    _coach, cookies, headers = _authenticate_plain_coach(client)

    listing = client.get(f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}", cookies=cookies, headers=headers)
    assert listing.status_code == 403

    accepted = client.post(
        f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}/{entry.season_entry_id}/accept-evidenced-draft",
        json={"reason": "should never be authorised"},
        cookies=cookies,
        headers=headers,
    )
    assert accepted.status_code == 403


def test_wrong_season_scoped_grant_is_refused(adjudication_client):
    client = adjudication_client
    db = client.app.state.database
    round_, _entries, scope, _lifecycle = _setup_round(db, 2904, 2904)
    _other_round, _other_entries, other_scope, _other_lifecycle = _setup_round(db, 2905, 2905)
    assert other_scope["season_id"] != scope["season_id"]

    _operator, cookies, headers = _authenticate_scorer(client, other_scope["season_id"])

    response = client.get(f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}", cookies=cookies, headers=headers)
    assert response.status_code == 403


def test_csrf_failure_is_rejected(adjudication_client):
    client = adjudication_client
    db = client.app.state.database
    round_, entries, scope, lifecycle = _setup_round(db, 2906, 2906)
    entry = entries[0]
    _prepare_missed_submission(db, scope, round_.bbbffl_round_id, entry, lifecycle)

    _operator, cookies, _headers = _authenticate_scorer(client, scope["season_id"])
    response = client.post(
        f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}/{entry.season_entry_id}/accept-evidenced-draft",
        json={"reason": "attempted without a valid CSRF token"},
        cookies=cookies,
        headers={"X-CSRF-Token": "not-the-real-token"},
    )
    assert response.status_code == 403
    detail = client.get(
        f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}/{entry.season_entry_id}",
        cookies=cookies,
        headers={"X-CSRF-Token": "not-the-real-token"},
    )
    assert detail.status_code == 200
    assert detail.json()["eligible"] is True  # nothing was mutated


def test_adjudication_page_renders_and_issues_csrf_cookie(adjudication_client):
    client = adjudication_client
    page = client.get("/scorer/lineup-adjudication")
    assert page.status_code == 200
    assert "bbbffl_csrf" in page.cookies
    assert "Missed-Submission Adjudication" in page.text
