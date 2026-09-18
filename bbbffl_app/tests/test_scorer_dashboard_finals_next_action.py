"""Issue #216: Scorer Round selector discovery across ordinary + Finals
weeks, the Finals-phase bracket-progression cue on the composed dashboard,
Finals-phase next-action guidance (bridging the ordinary dashboard's own
"season complete" dead-end), and the collapsed SuperScore row's
action-required indication. Builds on the same finals/dashboard fixtures
tests/test_finals.py and tests/test_scorer_dashboard_finals_superscore.py
already use -- never a new seeding convention."""

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import AuditEventRepository
from app.authorization import Principal, Role
from app.coach_lineup import CoachLineupService
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.finals import FinalsBracketRepository
from app.finals_preflight import open_finals_week
from app.finals_superscore_dashboard import _entry_action_required
from app.identity import IdentityRepository
from app.round_review import RoundReviewRepository
from app.scorer_dashboard import build_scorer_dashboard, season_round_options
from app.season import SeasonRepository
from app.superscore_results import SuperScoreLeaderboardService
from app.superscore_round import advance_round_to_review
from tests.finals_helpers import mark_finals_round_final, publish_finals_week_with_vacant_lineups, seed_official_result
from tests.finals_seeding_helpers import build_2026_replay_season
from tests.test_finals import ACTOR, _advance_to_week3, _advance_week1, _bracket_with_mappings, _open_and_seed_week1
from tests.test_scorer_dashboard_finals_superscore import (
    _coach_id,
    _open_finals_week1_and_superscore1,
    _StubAflClient,
)


def _dashboard(database, season_id, *, round_id=None):
    return build_scorer_dashboard(
        database,
        CompetitionLifecycleRepository(database),
        IdentityRepository(database),
        SeasonRepository(database),
        RoundReviewRepository(database),
        AuditEventRepository(database),
        _StubAflClient(),
        season_id,
        round_id=round_id,
    )


@pytest.fixture
def dashboard_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)
    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _admin(client):
    from app.routes.scorer_dashboard import require_scorer_dashboard

    principal = Principal(Role.ADMIN, "admin-1", "Admin", session_id="s1")
    client.app.dependency_overrides[require_scorer_dashboard] = lambda: principal
    return principal


# -- 1. Full ordinary + Finals selector discovery, human-readable labels ----


def test_season_round_options_lists_only_ordinary_rounds_before_a_finals_bracket_exists():
    built = build_2026_replay_season(year=2801)
    options = season_round_options(built["database"], built["season"].season_id)
    assert len(options) == 20
    assert all(o["stream"] == "ordinary" for o in options)
    assert [o["round_label"] for o in options][:2] == ["Round 1", "Round 2"]


def test_season_round_options_adds_human_readable_finals_weeks_once_a_bracket_exists():
    built, _bracket = _bracket_with_mappings(year=2802)
    options = season_round_options(built["database"], built["season"].season_id)
    assert len(options) == 24
    ordinary_labels = [o["round_label"] for o in options if o["stream"] == "ordinary"]
    assert ordinary_labels == [f"Round {n}" for n in range(1, 21)]
    finals_options = [o for o in options if o["stream"] == "finals"]
    assert [o["round_label"] for o in finals_options] == [
        "Finals Week 1",
        "Finals Week 2",
        "Preliminary Final",
        "Grand Final",
    ]
    # No UUID knowledge required -- every entry names a real, resolvable round.
    assert all(o["bbbffl_round_id"] for o in finals_options)
    assert finals_options[0]["state"] == "not_created"


def test_season_round_options_reflects_an_opened_finals_weeks_persisted_state():
    built, bracket = _bracket_with_mappings(year=2803)
    open_finals_week(built["database"], bracket.bracket_id, 1, actor=ACTOR)
    options = season_round_options(built["database"], built["season"].season_id)
    week1 = next(o for o in options if o["round_label"] == "Finals Week 1")
    assert week1["state"] == "open"


def test_ordinary_dashboard_round_options_include_finals_weeks_and_ordinary_rounds_still_20():
    """Ordinary Round 1-20 behaviour is unaffected by the Finals entries
    added to the selector: the dashboard still resolves the same current/
    published ordinary round it always did."""
    built, _bracket = _bracket_with_mappings(year=2804)
    dashboard = _dashboard(built["database"], built["season"].season_id)
    labels = {o["round_label"] for o in dashboard["round_options"]}
    assert "Finals Week 1" in labels and "Grand Final" in labels
    assert sum(1 for o in dashboard["round_options"] if o["stream"] == "ordinary") == 20
    assert dashboard["round"]["sequence"] == 20
    assert dashboard["round"]["state"] == "final"


# -- 2. Selecting Finals produces the combined Finals/SS context ------------


def test_selecting_a_finals_week_round_id_yields_the_unified_selector_never_a_separate_ss_entry(dashboard_client):
    client = dashboard_client
    built = _open_finals_week1_and_superscore1(year=2805, database=client.app.state.database)
    _admin(client)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": built["week1_round_id"]}
    )
    assert response.status_code == 200, response.text
    dashboard = response.json()["dashboard"]
    assert dashboard["stream"] == "finals_week"
    labels = [o["round_label"] for o in dashboard["round_options"]]
    assert "Finals Week 1" in labels
    assert "Round 20" in labels
    ss_round_id = built["ss1_round_id"]
    assert all(o["bbbffl_round_id"] != ss_round_id for o in dashboard["round_options"])


# -- 3/4. Bracket-progression cue + actionable guidance when pairing missing


def test_finals_dashboard_flags_pairing_missing_progression_with_an_actionable_cue(dashboard_client):
    client = dashboard_client
    database = client.app.state.database
    built, bracket = _bracket_with_mappings(year=2806, database=database)
    _open_and_seed_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    week1_round_id = FinalsBracketRepository(database).get_week_round_id(bracket.bracket_id, 1)
    mark_finals_round_final(database, week1_round_id)
    week2_round_id = FinalsBracketRepository(database).get_week_round_id(bracket.bracket_id, 2)
    _admin(client)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": week2_round_id}
    )
    assert response.status_code == 200, response.text
    body = response.json()["dashboard"]
    progression = body["finals"]["progression"]
    assert progression["reason"] == "pairing_missing"
    assert progression["from_week_label"] == "Finals Week 1"
    assert progression["from_week_state"] == "final"
    assert progression["target_week_label"] == "Finals Week 2"
    assert progression["preview_url"] == f"/api/admin/finals/{bracket.bracket_id}/advance/1/preview"
    assert progression["apply_url"] == f"/api/admin/finals/{bracket.bracket_id}/advance/1"
    assert progression["actionable_by_you"] is True
    assert body["next_action"]["code"] == "finals_bracket_progression_required"


def test_finals_dashboard_offers_progression_right_after_a_week_publishes(dashboard_client):
    """Uses the real lineup-driven `open -> live -> review -> final`
    publish pipeline (`tests.finals_helpers.publish_finals_week_with_
    vacant_lineups`), not a seeded-result shortcut, so this proves the
    genuinely-persisted `final` state the issue's own replay narrative
    describes ("After Finals Week 2 was finalised/published...") is what
    the new progression cue actually reacts to."""
    client = dashboard_client
    database = client.app.state.database
    built, bracket = _bracket_with_mappings(year=2807, database=database)
    open_finals_week(database, bracket.bracket_id, 1, actor=ACTOR)
    publish_finals_week_with_vacant_lineups(
        database, _StubAflClient(), bracket, 1, built["season"].season_id, actor=ACTOR
    )
    week1_round_id = FinalsBracketRepository(database).get_week_round_id(bracket.bracket_id, 1)
    _admin(client)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": week1_round_id}
    )
    body = response.json()["dashboard"]
    assert body["finals"]["lifecycle_state"] == "final"
    progression = body["finals"]["progression"]
    assert progression["reason"] == "ready_to_progress"
    assert progression["target_week_label"] == "Finals Week 2"
    assert body["next_action"]["code"] == "finals_bracket_progression_ready"


def test_replay_operator_sees_progression_but_not_as_actionable_for_them(dashboard_client):
    from app.routes.scorer_dashboard import require_scorer_dashboard

    client = dashboard_client
    database = client.app.state.database
    built, bracket = _bracket_with_mappings(year=2813, database=database)
    _open_and_seed_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    mark_finals_round_final(database, FinalsBracketRepository(database).get_week_round_id(bracket.bracket_id, 1))
    week2_round_id = FinalsBracketRepository(database).get_week_round_id(bracket.bracket_id, 2)
    operator = Principal(Role.REPLAY_OPERATOR, display_name="Operator")
    client.app.dependency_overrides[require_scorer_dashboard] = lambda: operator

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": week2_round_id}
    )
    assert response.status_code == 200, response.text
    assert response.json()["dashboard"]["finals"]["progression"]["actionable_by_you"] is False


def test_successful_progression_apply_navigates_directly_to_the_prepared_next_week(dashboard_client):
    client = dashboard_client
    database = client.app.state.database
    built, bracket = _bracket_with_mappings(year=2808, database=database)
    _open_and_seed_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    _admin(client)

    preview = client.get(f"/api/admin/finals/{bracket.bracket_id}/advance/1/preview")
    assert preview.status_code == 200
    preview_body = preview.json()
    assert preview_body["ready"] is True

    apply = client.post(
        f"/api/admin/finals/{bracket.bracket_id}/advance/1",
        params={
            "reason": "scorer-confirmed progression",
            "expected_versions": json.dumps(preview_body["expected_versions"]),
        },
    )
    assert apply.status_code == 200, apply.text
    target_round_id = apply.json()["target_round_id"]
    repo = FinalsBracketRepository(database)
    assert target_round_id == repo.get_week_round_id(bracket.bracket_id, 2)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": target_round_id}
    )
    assert response.status_code == 200, response.text
    dashboard = response.json()["dashboard"]
    assert dashboard["stream"] == "finals_week"
    assert dashboard["finals"]["round_id"] == target_round_id
    assert dashboard["finals"]["progression"] is None


# -- 5. Finals-phase next-action guidance ------------------------------------


def test_ordinary_dashboard_still_reports_season_complete_without_a_finals_bracket():
    built = build_2026_replay_season(year=2809)
    dashboard = _dashboard(built["database"], built["season"].season_id)
    assert dashboard["next_action"]["code"] == "published_season_complete"
    assert dashboard["next_action"]["category"] == "advisory"


def test_ordinary_dashboard_bridges_to_finals_week_ready_to_open_once_a_bracket_exists():
    built, bracket = _bracket_with_mappings(year=2810)
    database = built["database"]
    dashboard = _dashboard(database, built["season"].season_id)
    week1_round_id = FinalsBracketRepository(database).get_week_round_id(bracket.bracket_id, 1)
    assert dashboard["next_action"]["code"] == "finals_week_ready_to_open"
    assert dashboard["next_action"]["url"] == f"/scorer?season_id={built['season'].season_id}&round_id={week1_round_id}"


def test_ordinary_dashboard_reports_finals_complete_once_every_week_is_published():
    """Runs the whole bracket to the Grand Final via the exact domain
    sequence `test_finals.py::test_grand_final_pairing_and_tie_progression`
    already exercises for the pairing/elimination derivation itself, then
    marks each week's already-opened round `final` (`tests.finals_helpers.
    mark_finals_round_final` -- the same raw-SQL technique `correct_
    official_result` already uses) rather than building four real per-
    player scoring fixtures, and confirms the ordinary dashboard's
    Finals-phase bridge (issue #216) reports "finals complete" only once
    every week, including the Grand Final itself, is final."""
    built, bracket = _bracket_with_mappings(year=2811)
    database = built["database"]
    repo = FinalsBracketRepository(database)
    _advance_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    mark_finals_round_final(database, repo.get_week_round_id(bracket.bracket_id, 1))
    _advance_to_week3(built, bracket, ss2_result=(100, 50), fs_result=(50, 100))
    mark_finals_round_final(database, repo.get_week_round_id(bracket.bracket_id, 2))
    open_finals_week(database, bracket.bracket_id, 3, actor=ACTOR)
    pf = repo.list_pairings(bracket.bracket_id, week_number=3)[0]
    seed_official_result(database, pf.matchup_id, 100, 50)
    repo.advance_bracket(bracket.bracket_id, 3, actor=ACTOR, reason="advance from week 3")
    mark_finals_round_final(database, repo.get_week_round_id(bracket.bracket_id, 3))
    open_finals_week(database, bracket.bracket_id, 4, actor=ACTOR)
    gf = repo.list_pairings(bracket.bracket_id, week_number=4)[0]
    seed_official_result(database, gf.matchup_id, 100, 50)
    mark_finals_round_final(database, repo.get_week_round_id(bracket.bracket_id, 4))

    dashboard = _dashboard(database, built["season"].season_id)
    assert dashboard["next_action"]["code"] == "finals_complete"


def test_ordinary_dashboard_bridge_does_not_skip_a_published_week_with_incomplete_superscore():
    """Codex review (PR #217, P2): the ordinary-dashboard Finals-phase
    bridge must not silently `continue` past a `final` finals week whose
    concurrent SuperScore round still needs review/publication -- mirrors
    the equivalent fix already made to the composed Finals-week
    dashboard's own next-action guidance (`_finals_week_next_action`)."""
    built = _open_finals_week1_and_superscore1(year=2818)
    database = built["database"]
    mark_finals_round_final(database, built["week1_round_id"])

    dashboard = _dashboard(database, built["season"].season_id)
    assert dashboard["next_action"]["code"] == "finals_week_superscore_incomplete"
    assert dashboard["next_action"]["url"] == (
        f"/scorer?season_id={built['season'].season_id}&round_id={built['week1_round_id']}"
    )


# -- 6. Collapsed SuperScore rows expose required attention -----------------


def test_superscore_entries_with_no_submission_are_flagged_action_required(dashboard_client):
    client = dashboard_client
    built = _open_finals_week1_and_superscore1(year=2812, database=client.app.state.database)
    _admin(client)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": built["ss1_round_id"]}
    )
    entries = response.json()["dashboard"]["superscore"]["entries"]
    assert len(entries) == 10
    assert all(e["review_status"] == "not_submitted" for e in entries)
    assert all(e["action_required"] is True for e in entries)


def test_published_superscore_entries_are_never_flagged_action_required(dashboard_client):
    client = dashboard_client
    built = _open_finals_week1_and_superscore1(year=2814, database=client.app.state.database)
    database = built["database"]

    service = CoachLineupService(database, afl_client=_StubAflClient())
    for entry_obj in built["entries"]:
        coach_id = _coach_id(database, entry_obj.season_entry_id)
        entry = service.resolve(coach_id, built["season"].season_id, built["ss1_round_id"])
        draft = service.ensure_draft(built["season"].season_id, built["ss1_round_id"], entry)
        service.submit(draft, submission_version=0, coach_id=coach_id)
    advance_round_to_review(database, built["ss1_round_id"], actor=ACTOR, reason="advance for publish")
    SuperScoreLeaderboardService(database, _StubAflClient()).publish(
        built["ss1_round_id"], actor=ACTOR, reason="publish for test"
    )

    _admin(client)
    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": built["ss1_round_id"]}
    )
    entries = response.json()["dashboard"]["superscore"]["entries"]
    assert all(e["review_status"] == "published" for e in entries)
    assert all(e["action_required"] is False for e in entries)


# -- Codex review, PR #217 (P2): gate progression on the source week being
# ready, and account for unfinished SuperScore work before calling a
# published Finals week "done".


def test_pairing_missing_without_a_final_source_week_points_to_completing_it_instead(dashboard_client):
    """A progression preview/apply can only ever succeed once the source
    week is genuinely `final` (`FinalsBracketRepository.advance_bracket`'s
    own precondition). Since the round selector now exposes every finals
    week from bracket creation, an operator can select Finals Week 2 while
    Finals Week 1 is still merely `open` -- the dashboard must not offer a
    progression action that can only report a diagnostic, and must instead
    point at finishing Week 1."""
    client = dashboard_client
    database = client.app.state.database
    built, bracket = _bracket_with_mappings(year=2815, database=database)
    _open_and_seed_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    week1_round_id = FinalsBracketRepository(database).get_week_round_id(bracket.bracket_id, 1)
    week2_round_id = FinalsBracketRepository(database).get_week_round_id(bracket.bracket_id, 2)
    _admin(client)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": week2_round_id}
    )
    assert response.status_code == 200, response.text
    body = response.json()["dashboard"]
    assert body["finals"]["progression"] is None
    blocked_by = body["finals"]["blocked_by_week"]
    assert blocked_by["week_number"] == 1
    assert blocked_by["week_label"] == "Finals Week 1"
    assert blocked_by["state"] == "open"
    assert blocked_by["round_id"] == week1_round_id
    next_action = body["next_action"]
    assert next_action["code"] == "finals_prior_week_incomplete"
    assert next_action["url"] == f"/scorer?season_id={built['season'].season_id}&round_id={week1_round_id}"


def test_finals_published_but_superscore_incomplete_is_not_reported_as_fully_done(dashboard_client):
    """Finals and SuperScore have independent review/publication
    lifecycles -- Finals reaching `final` must not report the whole week
    as "published" while the concurrent SuperScore round still needs
    calculation/review/publication."""
    client = dashboard_client
    database = client.app.state.database
    built = _open_finals_week1_and_superscore1(year=2816, database=database)
    bracket = built["bracket"]
    repo = FinalsBracketRepository(database)
    pairings = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=1)}
    seed_official_result(database, pairings["qf"].matchup_id, 100, 50)
    seed_official_result(database, pairings["ef"].matchup_id, 50, 100)
    # Materialise Week 2's pairing so Finals Week 1's own next action isn't
    # instead "ready to progress the bracket" -- this test isolates the
    # SuperScore-incompleteness gap specifically.
    repo.advance_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="materialise week 2 for SS-incomplete test")
    mark_finals_round_final(database, built["week1_round_id"])
    _admin(client)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": built["week1_round_id"]}
    )
    assert response.status_code == 200, response.text
    body = response.json()["dashboard"]
    assert body["finals"]["lifecycle_state"] == "final"
    assert body["finals"]["progression"] is None
    assert body["superscore"]["available"] is True
    assert body["superscore"]["lifecycle_state"] != "final"
    assert body["next_action"]["code"] == "finals_week_superscore_incomplete"


def test_finals_and_superscore_both_final_reports_the_week_as_published(dashboard_client):
    """The positive counterpart: once SuperScore is also published, the
    week-level next action reverts to the simple "published" advisory."""
    client = dashboard_client
    database = client.app.state.database
    built = _open_finals_week1_and_superscore1(year=2817, database=database)
    bracket = built["bracket"]
    repo = FinalsBracketRepository(database)
    pairings = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=1)}
    seed_official_result(database, pairings["qf"].matchup_id, 100, 50)
    seed_official_result(database, pairings["ef"].matchup_id, 50, 100)
    repo.advance_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="materialise week 2 for SS-complete test")
    mark_finals_round_final(database, built["week1_round_id"])

    service = CoachLineupService(database, afl_client=_StubAflClient())
    for entry_obj in built["entries"]:
        coach_id = _coach_id(database, entry_obj.season_entry_id)
        entry = service.resolve(coach_id, built["season"].season_id, built["ss1_round_id"])
        draft = service.ensure_draft(built["season"].season_id, built["ss1_round_id"], entry)
        service.submit(draft, submission_version=0, coach_id=coach_id)
    advance_round_to_review(database, built["ss1_round_id"], actor=ACTOR, reason="advance for publish")
    SuperScoreLeaderboardService(database, _StubAflClient()).publish(
        built["ss1_round_id"], actor=ACTOR, reason="publish for test"
    )
    _admin(client)

    response = client.get(
        "/api/scorer/dashboard", params={"season_id": built["season"].season_id, "round_id": built["week1_round_id"]}
    )
    assert response.status_code == 200, response.text
    body = response.json()["dashboard"]
    assert body["superscore"]["lifecycle_state"] == "final"
    assert body["next_action"]["code"] == "finals_week_published"


def test_entry_action_required_consumes_the_authoritative_review_blockers_list():
    """Codex review (PR #217, P2): a vacant starter slot with a real,
    available Interchange candidate but no recorded assignment ruling is a
    genuine publish-blocking condition (`app.round_review._side_review`'s
    own `review_blockers`, the exact list `SuperScoreLeaderboardService.
    publish` refuses to publish through) even when no starter slot itself
    carries an unresolved DNP recommendation. An earlier version of
    `_entry_action_required` only re-scanned starter-slot `dnp_ruling`/
    `dnp_recommendation` pairs and missed this blocker class entirely --
    it must instead consume the authoritative `review_blockers` list
    directly, covering every blocker category by construction."""
    unresolved = ["Team X F3: interchange recommendation unresolved for vacant position(s) F3"]
    assert _entry_action_required("calculated", False, unresolved) is True
    assert _entry_action_required("calculated", False, []) is False
    assert _entry_action_required("calculated", False, None) is False
    # Published entries are frozen -- never flagged regardless of blockers.
    assert _entry_action_required("published", False, unresolved) is False
    # Pre-calculation states are unaffected by this change.
    assert _entry_action_required("not_submitted", False, None) is True
    assert _entry_action_required("stale_calculation", False, None) is True
    assert _entry_action_required("calculated", True, None) is True
