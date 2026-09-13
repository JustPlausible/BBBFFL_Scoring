"""Issue #192: HTTP-level coverage for SuperScore --

- the authenticated coach lineup route (`app/routes/coach_lineup.py`, made
  stream-aware for `superscore` by `app.coach_lineup`);
- the SS1 cross-stream carry-forward fallback, through both
  `app.lineup_adjudication.LineupAdjudicationService` directly and the
  `/api/admin/lineup-adjudication` HTTP route;
- SS2's same-stream fallback continuing to work unchanged;
- entry/round authorization for SuperScore on the adjudication and
  correction routes, including that neither ever needs a `matchup_id`.

Reuses `tests.test_lineup_correction_api`'s authenticated acting-context
helpers (the same season-scoped Scorer/coach session flow already
established for the ordinary/finals surfaces) and `tests.
test_lineup_adjudication_api`'s always-live-match AFL client double.
"""

import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.audit import ActorContext
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.lineup_adjudication import LineupAdjudicationService
from app.lineups import WeeklyLineupRepository
from app.lockouts import LockoutTriggerRepository
from app.superscore_round import open_round
from tests.finals_seeding_helpers import build_2026_replay_season
from tests.superscore_helpers import build_superscore_ready_season
from tests.test_lineup_adjudication_api import FUTURE_LIVE_MATCH_ID, _OneAlreadyLiveMatchAflClient
from tests.test_lineup_correction_api import _authenticate_scorer, _login_coach

ADMIN = ActorContext.anonymous_operator("admin")


COACH_TEST_CLUB_ID = 424242


class _CoachSubmissionAflClient:
    """One real, scheduled-but-not-yet-started match for the test club --
    an ordinary `open`-round coach submission scenario, where every
    position is legitimately still editable -- plus `get_rounds` (needed
    by `app.lineup_validation`'s availability check, which the coach
    submission path always exercises, unlike adjudication)."""

    def __init__(self, *, afl_season_id, afl_round_id):
        self._afl_season_id = afl_season_id
        self._afl_round_id = afl_round_id

    def get_matches(self, round_id):
        from datetime import datetime, timedelta, timezone

        from app.afl_client import Match, Team

        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        return [
            Match(
                match_id=1,
                home_team=Team(COACH_TEST_CLUB_ID, "Coach Test FC"),
                away_team=Team(COACH_TEST_CLUB_ID + 1, "Coach Test Opp"),
                status="SCHEDULED",
                start_time_utc=future,
            )
        ]

    def get_rounds(self, season_id):
        from app.afl_client import Round

        return [Round(round_id=self._afl_round_id, round_number=1, byes=())]


@pytest.fixture
def superscore_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        monkeypatch.setattr(client.app.state, "afl_client", _OneAlreadyLiveMatchAflClient())
        yield client
    db_path.unlink(missing_ok=True)


def _hidden(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match, f"{name!r} hidden field not found"
    return match.group(1)


def _give_players_a_resolvable_club(database, season_player_ids, afl_team_id=COACH_TEST_CLUB_ID):
    """`build_season`'s squad players carry no `afl_team_id` at all, so
    `app.lockouts`' lock evaluation cannot resolve any AFL match for them
    and refuses (fails closed to INDETERMINATE) rather than treat an
    unresolvable club as editable. A coach submission (unlike adjudication/
    correction, which never evaluate `lock_guard` as a *rejecting*
    callable) always goes through this evaluation, so it needs a
    resolvable-but-inert club (one no configured match/trigger ever
    references) to remain genuinely editable."""
    from app.player_pool import PlayerPoolRepository

    pool = PlayerPoolRepository(database)
    for season_player_id in season_player_ids:
        player = pool.get_by_id(season_player_id)
        pool.refresh_player(player.season_id, player.canonical_player_id, player.display_name, afl_team_id=afl_team_id)


def _set_coach_login(client, coach_id, email, password="correct horse battery staple"):
    with client.app.state.database.engine.begin() as conn:
        conn.execute(
            text("UPDATE coach SET email=:email WHERE coach_id=:coach_id"), {"email": email, "coach_id": coach_id}
        )
    client.app.state.credentials.set_password(coach_id, password, actor=ADMIN)
    return password


def _activate_a_trigger_and_go_live(db, round_id):
    """Mirrors `tests.test_lineup_adjudication_api._prepare_missed_submission`'s
    trigger setup -- an activated lockout trigger is a precondition of
    `LineupAdjudicationService`'s eligibility check, identical for every
    stream."""
    open_round(db, round_id, actor=ADMIN, reason="open before going live")
    LockoutTriggerRepository(db).create(round_id, "early-1", "selective", 1, [FUTURE_LIVE_MATCH_ID], reason="ss test")
    CompetitionLifecycleRepository(db).transition(round_id, "live")


def _build_with_open_round_20(year, **kwargs):
    """`build_2026_replay_season` finalises every round through
    `trigger_round` -- a `final` round can never accept a fresh ordinary
    submission again (`ORDINARY_SUBMISSION_ALLOWED_STATES` excludes
    `final`). `trigger_round=19` leaves round 20 (still fully fixture-drawn
    and AFL-mapped, per `build_season`) at `upcoming`, so this opens it for
    a real submission -- the "coach's most recently named ordinary lineup"
    SS1's cross-stream fallback must find."""
    built = build_superscore_ready_season(year=year, trigger_round=19, **kwargs)
    round_20_id = built["logical_rounds"][20].bbbffl_round_id
    CompetitionLifecycleRepository(built["database"]).create_ordinary_round(round_20_id)
    CompetitionLifecycleRepository(built["database"]).transition(round_20_id, "open")
    return built


def _submit_ordinary_round20(built, entry):
    """A real, effective ordinary-competition submission for `entry` in
    round 20 (opened by `_build_with_open_round_20`)."""
    database = built["database"]
    round_id = built["logical_rounds"][20].bbbffl_round_id
    squad = built["ownership"].current_squad(entry.season_entry_id)
    lineups = WeeklyLineupRepository(database)
    draft = lineups.save_draft(
        built["season"].season_id,
        built["competition"].competition_id,
        round_id,
        entry.season_entry_id,
        {"F1": squad[0].season_player_id, "F2": squad[1].season_player_id},
        expected_revision=0,
    )
    return lineups.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0)


# -- Coach route end-to-end -------------------------------------------------


def test_coach_can_view_and_submit_a_superscore_lineup_end_to_end(superscore_client, monkeypatch):
    client = superscore_client
    built = build_superscore_ready_season(database=client.app.state.database, year=5101)
    database = built["database"]
    round_id = built["superscore_rounds"][1]
    open_round(database, round_id, actor=ADMIN, reason="open SS1 for coach submission")
    monkeypatch.setattr(
        client.app.state,
        "afl_client",
        _CoachSubmissionAflClient(afl_season_id=built["season"].year, afl_round_id=built["superscore_afl_rounds"][1]),
    )
    # A round needs a configured lockout plan (even an unactivated one) for
    # `app.lockouts` to evaluate a position as editable at all -- otherwise
    # it fails closed as indeterminate ("lockout_plan_not_configured"),
    # exactly like an ordinary round would without round preflight's own
    # trigger configuration.
    LockoutTriggerRepository(database).create(round_id, "early-1", "selective", 1, [1], reason="ss coach test")

    entry = built["entries"][0]
    squad = built["ownership"].current_squad(entry.season_entry_id)
    _give_players_a_resolvable_club(database, [period.season_player_id for period in squad])
    coach = client.app.state.identities.get_current_coach(entry.season_entry_id)
    password = _set_coach_login(client, coach.coach_id, "ss-coach-5101@example.com")
    # Only the session cookie -- `_login_coach`'s own `bbbffl_csrf` cookie
    # is scoped to the `/account` page it visited to obtain it, not this
    # (different) coach-lineup page's own freshly issued token/cookie pair.
    cookies = {"bbbffl_session": _login_coach(client, "ss-coach-5101@example.com", password)[0]["bbbffl_session"]}

    url = f"/coach/seasons/{built['season'].season_id}/rounds/{round_id}/lineup"
    page = client.get(url, cookies=cookies, follow_redirects=False)
    assert page.status_code == 200
    # SuperScore has no opponent/matchup presentation -- the template's
    # `{% if lineup.opponent %}` line must render nothing.
    assert " vs " not in page.text

    squad = built["ownership"].current_squad(entry.season_entry_id)
    form = {
        f"position_{position}": ""
        for position in ["F1", "F2", "F3", "M1", "M2", "M3", "Ruck", "Tackler", "Interchange"]
    }
    form["position_F1"] = squad[0].season_player_id
    form["position_F2"] = squad[1].season_player_id
    form.update(
        csrf_token=_hidden(page.text, "csrf_token"),
        draft_revision=_hidden(page.text, "draft_revision"),
        submission_version=_hidden(page.text, "submission_version"),
        action="submit",
        confirm_vacancies="1",
    )
    submitted = client.post(url, data=form, cookies=cookies, follow_redirects=False)
    assert submitted.status_code == 303
    assert "notice=submitted" in submitted.headers["location"]

    lineup_row = database.execute(
        "SELECT effective_submission_version FROM weekly_lineup WHERE bbbffl_round_id=? AND season_entry_id=?",
        (round_id, entry.season_entry_id),
    ).fetchone()
    assert lineup_row["effective_submission_version"] == 1
    review_row = database.execute(
        "SELECT review_version FROM superscore_entry_review_state WHERE bbbffl_round_id=? AND season_entry_id=?",
        (round_id, entry.season_entry_id),
    ).fetchone()
    assert review_row["review_version"] == 1


# -- SS1 cross-stream carry-forward fallback -------------------------------


def test_ss1_cross_stream_fallback_via_service(superscore_client):
    client = superscore_client
    built = _build_with_open_round_20(5102, database=client.app.state.database)
    database = built["database"]
    entry = built["entries"][0]
    ordinary_submission = _submit_ordinary_round20(built, entry)

    ss1_round_id = built["superscore_rounds"][1]
    _activate_a_trigger_and_go_live(database, ss1_round_id)

    service = LineupAdjudicationService(database, client.app.state.afl_client)
    submission, adjudication = service.apply_carry_forward_fallback(
        built["season"].season_id,
        built["superscore_stream"].competition_id,
        ss1_round_id,
        entry.season_entry_id,
        actor=ActorContext.anonymous_operator("scorer"),
        reason="SS1: no submission ever existed for this coach; falling back to their last ordinary lineup",
    )
    assert submission.source_type == "scorer_adjudicated_carry_forward"
    assert submission.positions == ordinary_submission.positions
    assert adjudication.source_bbbffl_round_id == built["logical_rounds"][20].bbbffl_round_id

    review_row = database.execute(
        "SELECT review_version FROM superscore_entry_review_state WHERE bbbffl_round_id=? AND season_entry_id=?",
        (ss1_round_id, entry.season_entry_id),
    ).fetchone()
    assert review_row["review_version"] == 1


def test_ss1_cross_stream_fallback_via_http_route(superscore_client):
    client = superscore_client
    built = _build_with_open_round_20(5103, database=client.app.state.database)
    database = built["database"]
    entry = built["entries"][0]
    ordinary_submission = _submit_ordinary_round20(built, entry)

    ss1_round_id = built["superscore_rounds"][1]
    _activate_a_trigger_and_go_live(database, ss1_round_id)

    _operator, cookies, headers = _authenticate_scorer(client, built["season"].season_id)
    response = client.post(
        f"/api/admin/lineup-adjudication/{ss1_round_id}/{entry.season_entry_id}/apply-carry-forward",
        json={"reason": "SS1 cross-stream fallback via the HTTP route"},
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["submission"]["source_type"] == "scorer_adjudicated_carry_forward"
    assert body["submission"]["positions"] == ordinary_submission.positions


def test_ss2_same_stream_fallback_still_uses_ordinary_carry_forward_unchanged(superscore_client):
    """SS2 must never reach the cross-stream branch: once SS1 has an
    effective submission, SS2's fallback source is that SS1 submission
    (same-stream, `app.carry_forward.CarryForwardService.resolve_source`),
    exactly like an ordinary round -- never the entry's ordinary Round 20
    lineup, even though one exists."""
    client = superscore_client
    built = _build_with_open_round_20(5104, database=client.app.state.database)
    database = built["database"]
    entry = built["entries"][0]
    _submit_ordinary_round20(built, entry)

    ss1_round_id = built["superscore_rounds"][1]
    ss2_round_id = built["superscore_rounds"][2]
    open_round(database, ss1_round_id, actor=ADMIN, reason="open SS1")
    squad = built["ownership"].current_squad(entry.season_entry_id)
    lineups = WeeklyLineupRepository(database)
    draft = lineups.save_draft(
        built["season"].season_id,
        built["superscore_stream"].competition_id,
        ss1_round_id,
        entry.season_entry_id,
        {"F1": squad[2].season_player_id},
        expected_revision=0,
    )
    ss1_submission = lineups.submit(
        draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0
    )

    _activate_a_trigger_and_go_live(database, ss2_round_id)
    service = LineupAdjudicationService(database, client.app.state.afl_client)
    submission, adjudication = service.apply_carry_forward_fallback(
        built["season"].season_id,
        built["superscore_stream"].competition_id,
        ss2_round_id,
        entry.season_entry_id,
        actor=ActorContext.anonymous_operator("scorer"),
        reason="SS2: coach missed SS2, falls back to their own SS1 lineup",
    )
    assert submission.positions == ss1_submission.positions
    assert adjudication.source_bbbffl_round_id == ss1_round_id  # same-stream, not ordinary Round 20


# -- Entry/round authorization, and no matchup_id required ------------------


def test_adjudication_route_lists_all_ten_entries_with_no_matchup(superscore_client):
    client = superscore_client
    database = client.app.state.database
    built = build_superscore_ready_season(database=database, year=5105)
    round_id = built["superscore_rounds"][1]
    _operator, cookies, headers = _authenticate_scorer(client, built["season"].season_id)

    listing = client.get(f"/api/admin/lineup-adjudication/{round_id}", cookies=cookies, headers=headers)
    assert listing.status_code == 200
    body = listing.json()
    assert {e["season_entry_id"] for e in body["entries"]} == {e.season_entry_id for e in built["entries"]}
    assert (
        database.execute("SELECT COUNT(*) AS n FROM bbbffl_matchup WHERE bbbffl_round_id=?", (round_id,)).fetchone()[
            "n"
        ]
        == 0
    )


def test_adjudication_entry_authorization_rejects_a_foreign_season_entry(superscore_client):
    client = superscore_client
    database = client.app.state.database
    built = build_superscore_ready_season(database=database, year=5106)
    other_season = build_2026_replay_season(database=database, year=5107)
    round_id = built["superscore_rounds"][1]
    _activate_a_trigger_and_go_live(database, round_id)
    _operator, cookies, headers = _authenticate_scorer(client, built["season"].season_id)

    foreign_entry = other_season["entries"][0]
    response = client.get(
        f"/api/admin/lineup-adjudication/{round_id}/{foreign_entry.season_entry_id}", cookies=cookies, headers=headers
    )
    assert response.status_code == 404


def test_correction_route_authorization_for_superscore(superscore_client):
    client = superscore_client
    database = client.app.state.database
    built = build_superscore_ready_season(database=database, year=5108)
    round_id = built["superscore_rounds"][1]
    open_round(database, round_id, actor=ADMIN, reason="open for correction")
    entry = built["entries"][0]
    squad = built["ownership"].current_squad(entry.season_entry_id)
    lineups = WeeklyLineupRepository(database)
    draft = lineups.save_draft(
        built["season"].season_id,
        built["superscore_stream"].competition_id,
        round_id,
        entry.season_entry_id,
        {"F1": squad[0].season_player_id},
        expected_revision=0,
    )
    lineups.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0)

    _operator, cookies, headers = _authenticate_scorer(client, built["season"].season_id)
    listing = client.get(f"/api/admin/lineup-correction/{round_id}", cookies=cookies, headers=headers)
    assert listing.status_code == 200
    assert {e["season_entry_id"] for e in listing.json()["entries"]} == {e.season_entry_id for e in built["entries"]}

    detail = client.get(
        f"/api/admin/lineup-correction/{round_id}/{entry.season_entry_id}", cookies=cookies, headers=headers
    )
    assert detail.status_code == 200

    corrected = client.post(
        f"/api/admin/lineup-correction/{round_id}/{entry.season_entry_id}/correct",
        json={
            "expected_submission_version": 1,
            "position_changes": {"F1": squad[1].season_player_id},
            "reason": "SuperScore correction route reachable end-to-end",
        },
        cookies=cookies,
        headers=headers,
    )
    assert corrected.status_code == 200
    review_row = database.execute(
        "SELECT review_version FROM superscore_entry_review_state WHERE bbbffl_round_id=? AND season_entry_id=?",
        (round_id, entry.season_entry_id),
    ).fetchone()
    assert review_row["review_version"] == 2
