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
    _submit_lineup,
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


class _CalculableAflClient(_OneAlreadyLiveMatchAflClient):
    """The same always-live match, plus a (deliberately empty) player-stats
    source -- `_OneAlreadyLiveMatchAflClient` alone is sufficient for
    adjudication (which never calculates), but a calculation that resolves
    a slot to this match needs `get_match_player_stats` to exist at all."""

    def get_match_player_stats(self, match_id):
        return {}


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
    # Issue #151: Resolution A's preview must show the player's name for
    # every populated position -- the season_player_id stays present too,
    # but only as a secondary/diagnostic identifier.
    assert f1_slot["player_name"] == player.display_name

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
    assert result["submission"]["positions_detail"]["F1"]["player_name"] == player.display_name
    assert result["adjudication"]["decision_type"] == "accept_evidenced_draft"
    accepted_f1_slot = next(s for s in result["adjudication"]["slots"] if s["position"] == "F1")
    assert accepted_f1_slot["player_name"] == player.display_name

    # A second attempt against the now-submitted lineup is refused.
    second = client.post(
        f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}/{entry.season_entry_id}/accept-evidenced-draft",
        json={"reason": "attempted again after a submission already exists"},
        cookies=cookies,
        headers=headers,
    )
    assert second.status_code == 409


def test_adjudication_response_reports_calculation_status_immediately(adjudication_client):
    """Issue #153 acceptance: adjudication must show, in the same response
    that reveals the new effective submission, whether calculation is
    required for this team -- this matchup could never have been
    calculated before (the calculation engine requires an effective
    submission on both sides, which by definition didn't exist for a
    missed-initial-submission team), so the honest, immediately-visible
    signal both before and right after acceptance is `calculated: False`
    -- never silently omitted, and never a stale value left over from
    reading the candidate before the submission existed. Calculating the
    matchup afterwards clears it, visible on the very next read."""
    from app.calculations import MatchupCalculationService

    client = adjudication_client
    db = client.app.state.database
    round_, entries, scope, lifecycle = _setup_round(db, 2902, 2902)
    entry = entries[0]

    # The calculation engine requires an effective submission on *both*
    # sides of a matchup -- give the opponent one now, while the round is
    # still `open` (an ordinary ungoverned submission), before the round
    # goes `live` below and would require a lock guard for any further
    # submission.
    matchup = next(
        m
        for m in lifecycle.list_matchups(round_.bbbffl_round_id)
        if entry.season_entry_id in (m.home_season_entry_id, m.away_season_entry_id)
    )
    opponent_id = (
        matchup.away_season_entry_id
        if matchup.home_season_entry_id == entry.season_entry_id
        else matchup.home_season_entry_id
    )
    opponent = next(e for e in entries if e.season_entry_id == opponent_id)
    o1 = PlayerPoolRepository(db).refresh_player(scope["season_id"], 900101, "Opponent One")
    o2 = PlayerPoolRepository(db).refresh_player(scope["season_id"], 900102, "Opponent Two")
    OwnershipRepository(db).configure_squad_limit(scope["season_id"], 10)
    OwnershipRepository(db).acquire(o1.season_player_id, opponent.season_entry_id)
    OwnershipRepository(db).acquire(o2.season_player_id, opponent.season_entry_id)
    _submit_lineup(db, scope, round_.bbbffl_round_id, opponent, [o1, o2])

    player = _prepare_missed_submission(db, scope, round_.bbbffl_round_id, entry, lifecycle)

    _operator, cookies, headers = _authenticate_scorer(client, scope["season_id"])

    before = client.get(
        f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}/{entry.season_entry_id}",
        cookies=cookies,
        headers=headers,
    )
    assert before.status_code == 200
    assert before.json()["calculation"] == {
        "calculated": False,
        "calculation_revision": None,
        "calculated_lineup_version": None,
        "current_lineup_version": None,
        "stale": False,
        "message": "This matchup has not been calculated yet.",
    }

    accepted = client.post(
        f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}/{entry.season_entry_id}/accept-evidenced-draft",
        json={"reason": "League chat confirmed the coach's pre-lockout draft; quorum accepted it"},
        cookies=cookies,
        headers=headers,
    )
    assert accepted.status_code == 200
    result = accepted.json()
    assert result["submission"]["positions"]["F1"] == player.season_player_id
    assert result["calculation"]["calculated"] is False

    MatchupCalculationService(db, _CalculableAflClient()).calculate_matchup(matchup.matchup_id)

    after = client.get(
        f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}/{entry.season_entry_id}",
        cookies=cookies,
        headers=headers,
    )
    assert after.json()["calculation"]["calculated"] is True
    assert after.json()["calculation"]["stale"] is False


def test_candidate_view_resolves_player_names_and_club_for_resolution_a_and_b_previews():
    """Issue #151: `_candidate_view` (the shared read model behind the
    Scorer/Admin missed-submission adjudication API) must show player name
    and AFL club, where known, for every populated position in both
    Resolution A's evidenced-draft preview and Resolution B's carry-forward
    preview -- a vacant position resolves to no player, never a misleading
    fallback name."""
    from types import SimpleNamespace

    from app.competition_lifecycle import CompetitionLifecycleRepository
    from app.lineup_adjudication import AdjudicationCandidate, AdjudicationSlotRecord, CarryForwardPreview
    from app.player_pool import PlayerPoolRepository
    from app.round_review import RoundReviewRepository
    from app.routes.lineup_adjudication import _candidate_view, _collect_player_ids
    from app.season import SeasonRepository
    from tests.db_helpers import migrated_connection

    db = migrated_connection()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(lifecycle=CompetitionLifecycleRepository(db), round_review=RoundReviewRepository(db))
        )
    )
    season = SeasonRepository(db).create_season(2027, "2027")
    pool = PlayerPoolRepository(db)
    evidenced_player = pool.refresh_player(season.season_id, 1, "Evidenced Player", afl_team_name="Fitzroy")
    carry_forward_player = pool.refresh_player(season.season_id, 2, "Carried Player", afl_team_name="Richmond")

    candidate = AdjudicationCandidate(
        lineup_id="lineup-1",
        season_id=season.season_id,
        competition_id="comp-1",
        bbbffl_round_id="round-1",
        season_entry_id="entry-1",
        round_state="live",
        eligible=True,
        ineligible_reason=None,
        activated_triggers=(),
        draft_revision=1,
        draft_updated_at="2027-01-01T00:00:00+00:00",
        evidenced_preview=(
            AdjudicationSlotRecord(
                position="F1",
                season_player_id=evidenced_player.season_player_id,
                was_locked=True,
                evidence_status="proven_pre_lock",
                lock_reason=None,
                afl_match_id=None,
                effective_lock_at=None,
                observed_status=None,
                evidence_saved_at="2027-01-01T00:00:00+00:00",
            ),
        ),
        carry_forward_preview=CarryForwardPreview(
            source_bbbffl_round_id="round-0",
            source_lineup_id="lineup-0",
            source_submission_version=1,
            positions={"F1": carry_forward_player.season_player_id, "F2": None},
        ),
    )
    player_ids = _collect_player_ids(
        evidenced_preview=candidate.evidenced_preview, carry_forward_preview=candidate.carry_forward_preview
    )
    player_labels = pool.labels_by_id(player_ids)
    view = _candidate_view(request, candidate, {"team_name": "Fitzroy Phoenix", "coach_name": "Barry"}, player_labels)

    assert view["evidenced_preview"][0]["player_name"] == "Evidenced Player"
    assert view["evidenced_preview"][0]["afl_club"] == "Fitzroy"
    # season_player_id/canonical identifiers remain present -- secondary,
    # never removed.
    assert view["evidenced_preview"][0]["season_player_id"] == evidenced_player.season_player_id

    detail = view["carry_forward_preview"]["positions_detail"]
    assert detail["F1"]["player_name"] == "Carried Player"
    assert detail["F1"]["afl_club"] == "Richmond"
    assert detail["F2"]["player_name"] is None  # a vacant position resolves to no player, not a misleading fallback
    # The stable positions map is retained verbatim alongside the new detail.
    assert view["carry_forward_preview"]["positions"] == {"F1": carry_forward_player.season_player_id, "F2": None}


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


def test_accept_evidenced_draft_refuses_when_no_draft_exists(adjudication_client):
    """Codex review (PR #149): an entry that never saved a private draft
    has no evidence to capture -- the API must refuse with a controlled
    client error, never proceed to create a synthetic empty submission."""
    client = adjudication_client
    db = client.app.state.database
    round_, entries, scope, lifecycle = _setup_round(db, 2907, 2907)
    entry = entries[0]
    other_entry = entries[1]
    _prepare_missed_submission(db, scope, round_.bbbffl_round_id, entry, lifecycle)

    _operator, cookies, headers = _authenticate_scorer(client, scope["season_id"])
    candidate = client.get(
        f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}/{other_entry.season_entry_id}",
        cookies=cookies,
        headers=headers,
    )
    assert candidate.status_code == 200
    assert candidate.json()["evidenced_preview"] is None

    response = client.post(
        f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}/{other_entry.season_entry_id}/accept-evidenced-draft",
        json={"reason": "attempted against an entry with no saved draft"},
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 409


def test_carry_forward_with_no_source_round_returns_a_controlled_error(adjudication_client):
    """Codex review (PR #149): a first-round entry (or any team with no
    previous submitted lineup) has no carry-forward source -- calling the
    endpoint directly must return a controlled 4xx, never a bare 500."""
    client = adjudication_client
    db = client.app.state.database
    round_, entries, scope, lifecycle = _setup_round(db, 2908, 2908)
    entry = entries[0]
    _prepare_missed_submission(db, scope, round_.bbbffl_round_id, entry, lifecycle)

    _operator, cookies, headers = _authenticate_scorer(client, scope["season_id"])
    candidate = client.get(
        f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}/{entry.season_entry_id}",
        cookies=cookies,
        headers=headers,
    )
    assert candidate.json()["carry_forward_preview"] is None

    response = client.post(
        f"/api/admin/lineup-adjudication/{round_.bbbffl_round_id}/{entry.season_entry_id}/apply-carry-forward",
        json={"reason": "attempted with no previous-round source available"},
        cookies=cookies,
        headers=headers,
    )
    assert 400 <= response.status_code < 500


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
    # Issue #151: the browser Round A/B preview tables must render the API's
    # player_name/afl_club fields, not the raw season_player_id -- proven
    # here by asserting the template's own field references, since this
    # repo has no headless-JS execution harness for these vanilla-JS pages.
    assert "s.player_name" in page.text
    assert "s.season_player_id" not in page.text
    assert "positions_detail" in page.text
