"""Domain-level coverage for the Scorer Operations Dashboard (issue #147).

Builds directly on the same fixtures `app.round_review`/`app.lockouts`
already use (`tests/round_review_helpers.py`, `tests/test_lockouts.py`,
`tests/test_competition_lifecycle.py`) rather than inventing a second
seeding convention -- this proves `app.scorer_dashboard.build_scorer_dashboard`
reads the same authoritative facts those domains already expose, never a
parallel read model.
"""

from app.audit import ActorContext, AuditEventRepository
from app.calculations import MatchupCalculationService
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.identity import IdentityRepository
from app.lineup_correction import LineupCorrectionService
from app.lineups import WeeklyLineupRepository
from app.lockouts import LockoutRepository, LockoutTriggerRepository, RoundMatchFactsProvider
from app.player_pool import OwnershipRepository
from app.round_mapping import RoundMappingRepository
from app.round_review import RoundReviewRepository, attempt_signoff
from app.scorer_dashboard import (
    SUBMISSION_DIVERGED,
    SUBMISSION_DRAFT_ONLY,
    SUBMISSION_INCOMPLETE,
    SUBMISSION_MISSING,
    build_scorer_dashboard,
    select_current_round,
)
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection
from tests.round_review_helpers import Facts, full_round, progress_to_review
from tests.test_competition_lifecycle import operational
from tests.test_lockouts import (
    ALL_MATCHES,
    EARLY_MATCH_ID,
    EARLY_START,
    acquire,
    configure_main,
    early_match,
    establish,
)
from tests.test_lockouts import context as lockout_context


def _season_id(lifecycle, round_) -> str:
    """`round_` (a `BBBFFLRound`, from `SeasonRepository.create_round`) has
    no `season_id` of its own -- only the persisted lifecycle round
    (`CompetitionLifecycleRepository.get_round`) does."""
    return lifecycle.get_round(round_.bbbffl_round_id).season_id


def _dashboard(db, afl_client, season_id, *, round_id=None):
    lifecycle = CompetitionLifecycleRepository(db)
    identities = IdentityRepository(db)
    seasons = SeasonRepository(db)
    review_repo = RoundReviewRepository(db)
    audit_events = AuditEventRepository(db)
    return build_scorer_dashboard(
        db, lifecycle, identities, seasons, review_repo, audit_events, afl_client, season_id, round_id=round_id
    )


def test_select_current_round_prefers_earliest_non_final_round():
    rounds = [
        {"bbbffl_round_id": "a", "round_state": "final", "sequence": 1},
        {"bbbffl_round_id": "b", "round_state": "open", "sequence": 2},
        {"bbbffl_round_id": "c", "round_state": None, "sequence": 3},
    ]
    assert select_current_round(rounds, None)["bbbffl_round_id"] == "b"


def test_select_current_round_falls_back_to_most_recent_when_all_final():
    rounds = [
        {"bbbffl_round_id": "a", "round_state": "final", "sequence": 1},
        {"bbbffl_round_id": "b", "round_state": "final", "sequence": 2},
    ]
    assert select_current_round(rounds, None)["bbbffl_round_id"] == "b"


def test_select_current_round_honours_an_explicit_requested_round():
    rounds = [
        {"bbbffl_round_id": "a", "round_state": "final", "sequence": 1},
        {"bbbffl_round_id": "b", "round_state": "open", "sequence": 2},
    ]
    assert select_current_round(rounds, "a")["bbbffl_round_id"] == "a"


def test_upcoming_round_next_action_is_complete_preflight():
    lifecycle, round_, entries = operational(migrated_connection(), year=8901, afl_round=100)
    db = lifecycle.database
    view = _dashboard(db, Facts({}), _season_id(lifecycle, round_))
    assert view["round"]["state"] == "upcoming"
    assert view["next_action"]["code"] == "complete_preflight"
    assert any(item["category"] == "blocking" for item in view["attention"])


def test_human_readable_team_identity_not_uuids():
    db, lifecycle, round_, entries, stats, canon = full_round(year=8902, afl_round=100)
    afl = Facts(stats)
    view = _dashboard(db, afl, _season_id(lifecycle, round_))
    names = {row["team_name"] for row in view["lineups"]}
    assert names == {f"Team {i}" for i in range(10)}
    for row in view["lineups"]:
        assert row["season_entry_id"] not in (row["team_name"] or "")


def test_open_round_with_missing_lineup_is_waiting_before_lockout():
    # A main trigger is configured but has not activated yet (a far-future
    # scheduled start), so a team that never touched this round's lineup
    # is "waiting on the coach", never yet eligible for missed-submission
    # adjudication.
    db, lifecycle, round_, entries, scope, pool, ownership = lockout_context(year=8903)
    lineups = WeeklyLineupRepository(db)
    triggers = LockoutTriggerRepository(db)
    configure_main(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID])
    entry_id = entries[0].season_entry_id
    for entry in entries[1:]:
        establish(lineups, round_, entry, scope, {})
    far_future = EARLY_START.replace(year=EARLY_START.year + 20)
    afl = type("Afl", (), {"get_matches": lambda self, rid: [early_match(status="UPCOMING", start=far_future)]})()
    view = _dashboard(db, afl, _season_id(lifecycle, round_))
    row = next(r for r in view["lineups"] if r["season_entry_id"] == entry_id)
    assert row["submission_state"] == SUBMISSION_MISSING
    assert row["adjudication_available"] is False
    assert view["next_action"]["code"] in ("complete_remaining_lineups", "await_first_lockout")


def test_missed_submission_becomes_adjudication_eligible_once_a_trigger_activates():
    db, lifecycle, round_, entries, scope, pool, ownership = lockout_context(year=8904)
    lineups = WeeklyLineupRepository(db)
    triggers = LockoutTriggerRepository(db)
    configure_main(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID])
    entry_zero = entries[0]
    acquire(pool, ownership, scope, entry_zero, 90001, ALL_MATCHES[0].home_team)
    establish(lineups, round_, entries[1], scope, {"F1": None})
    # entries[0] never submits at all.
    afl = type("Afl", (), {"get_matches": lambda self, rid: [early_match(status="CONCLUDED", start=EARLY_START)]})()
    lifecycle.transition(round_.bbbffl_round_id, "live")
    view = _dashboard(db, afl, _season_id(lifecycle, round_), round_id=round_.bbbffl_round_id)
    row = next(r for r in view["lineups"] if r["season_entry_id"] == entry_zero.season_entry_id)
    assert row["submission_state"] == SUBMISSION_MISSING
    assert row["adjudication_available"] is True
    assert view["next_action"]["code"] == "review_missed_submission_adjudication"
    assert any(item["code"] == "lineup:missed_submission" for item in view["attention"])


def test_trigger_observed_status_distinct_from_persisted_activation():
    db, lifecycle, round_, entries, scope, pool, ownership = lockout_context(year=8905)
    triggers = LockoutTriggerRepository(db)
    configure_main(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID])
    # The match's own observed status is still UPCOMING, but evaluation
    # time has already reached its scheduled start -- the trigger must
    # still durably activate (match_time_reached), exactly as
    # docs/lockouts.md documents.
    afl = type(
        "Afl",
        (),
        {"get_matches": lambda self, rid: [early_match(status="UPCOMING", start=EARLY_START)]},
    )()
    evaluation_at = EARLY_START.replace(minute=EARLY_START.minute + 5)

    class FixedClockMatchFacts(RoundMatchFactsProvider):
        def evaluation_at(self):
            return evaluation_at

    match_facts = FixedClockMatchFacts(RoundMappingRepository(db), afl)
    LockoutRepository(db).materialize_round_triggers(round_.bbbffl_round_id, match_facts=match_facts)
    view = _dashboard(db, afl, _season_id(lifecycle, round_), round_id=round_.bbbffl_round_id)
    trigger_row = view["lockout"]["triggers"][0]
    assert trigger_row["activated"] is True
    assert trigger_row["activation_reason"] == "match_time_reached"
    assert trigger_row["observed_status"] == "UPCOMING"


def test_review_state_ready_for_signoff_when_no_blockers():
    db, lifecycle, round_, entries, stats, canon = full_round(year=8906, afl_round=100)
    progress_to_review(lifecycle, round_.bbbffl_round_id)
    afl = Facts(stats)
    MatchupCalculationService(db, afl).calculate_round(round_.bbbffl_round_id)
    view = _dashboard(db, afl, _season_id(lifecycle, round_), round_id=round_.bbbffl_round_id)
    assert view["review"]["ready_for_signoff"] is True
    assert view["next_action"]["code"] == "ready_for_signoff"


def test_review_state_not_ready_reports_decision_required_blockers():
    db, lifecycle, round_, entries, stats, canon = full_round(year=8907, afl_round=100, vacant_slots={(0, "F2")})
    progress_to_review(lifecycle, round_.bbbffl_round_id)
    afl = Facts(stats)
    MatchupCalculationService(db, afl).calculate_round(round_.bbbffl_round_id)
    view = _dashboard(db, afl, _season_id(lifecycle, round_), round_id=round_.bbbffl_round_id)
    assert view["review"]["ready_for_signoff"] is False
    assert view["next_action"]["code"] == "resolve_scorer_decisions"
    assert any(item["category"] == "decision_required" for item in view["attention"])


def test_published_round_reports_final_and_prepares_next_round():
    db, lifecycle, round_, entries, stats, canon = full_round(year=8908, afl_round=100)
    progress_to_review(lifecycle, round_.bbbffl_round_id)
    afl = Facts(stats)
    MatchupCalculationService(db, afl).calculate_round(round_.bbbffl_round_id)
    identities = IdentityRepository(db)
    review_repo = RoundReviewRepository(db)
    attempt_signoff(
        lifecycle, review_repo, identities, round_.bbbffl_round_id, actor=ActorContext.anonymous_operator("scorer")
    )
    view = _dashboard(db, afl, _season_id(lifecycle, round_), round_id=round_.bbbffl_round_id)
    assert view["round"]["state"] == "final"
    assert view["next_action"]["category"] == "advisory"


def test_private_draft_diverging_from_submission_is_flagged_not_authoritative():
    db, lifecycle, round_, entries, stats, canon = full_round(year=8909, afl_round=100)
    lineups = WeeklyLineupRepository(db)
    entry_id = entries[0].season_entry_id
    draft = lineups.get_draft(_season_id(lifecycle, round_), round_.competition_id, round_.bbbffl_round_id, entry_id)
    lineups.save_draft(
        _season_id(lifecycle, round_),
        round_.competition_id,
        round_.bbbffl_round_id,
        entry_id,
        {**draft.positions, "F1": None},
        expected_revision=draft.revision,
    )
    afl = Facts(stats)
    view = _dashboard(db, afl, _season_id(lifecycle, round_), round_id=round_.bbbffl_round_id)
    row = next(r for r in view["lineups"] if r["season_entry_id"] == entry_id)
    assert row["submission_state"] == SUBMISSION_DIVERGED
    assert any(item["code"] == "lineup:draft_diverges" for item in view["attention"])


def test_calculation_staleness_after_correction_requires_recalculation():
    db, lifecycle, round_, entries, stats, canon = full_round(year=8910, afl_round=100)
    entry_id = entries[0].season_entry_id
    lineup_row = db.execute(
        "SELECT lineup_id FROM weekly_lineup WHERE bbbffl_round_id=? AND season_entry_id=?",
        (round_.bbbffl_round_id, entry_id),
    ).fetchone()
    # `full_round` seeds submitted slots directly and never establishes
    # `player_ownership_period` rows -- issue #137's correction workflow
    # validates ownership (`WeeklyLineupRepository._validate_ownership`),
    # so this test's correction target players must be genuinely owned.
    ownership = OwnershipRepository(db)
    ownership.configure_squad_limit(_season_id(lifecycle, round_), 10)
    for slot in db.execute(
        "SELECT position, season_player_id FROM weekly_lineup_submission_slot WHERE lineup_id=? AND version=1",
        (lineup_row["lineup_id"],),
    ).fetchall():
        if slot["season_player_id"] is not None:
            ownership.acquire(slot["season_player_id"], entry_id)
    progress_to_review(lifecycle, round_.bbbffl_round_id)
    afl = Facts(stats)
    MatchupCalculationService(db, afl).calculate_round(round_.bbbffl_round_id)
    service = LineupCorrectionService(db, afl)
    candidate = service.describe(_season_id(lifecycle, round_), round_.competition_id, round_.bbbffl_round_id, entry_id)
    changed_position, other_player = None, None
    for slot in candidate.slots:
        if slot.position != "F1":
            other_player = slot.season_player_id
            changed_position = slot.position
            break
    service.correct(
        _season_id(lifecycle, round_),
        round_.competition_id,
        round_.bbbffl_round_id,
        entry_id,
        {"F1": other_player, changed_position: None},
        expected_submission_version=candidate.expected_submission_version,
        actor=ActorContext.anonymous_operator("scorer"),
        reason="Test correction",
    )
    view = _dashboard(db, afl, _season_id(lifecycle, round_), round_id=round_.bbbffl_round_id)
    row = next(r for r in view["lineups"] if r["season_entry_id"] == entry_id)
    assert row["calculation_stale"] is True
    assert any(item["code"] == "lineup:calculation_stale" for item in view["attention"])
    assert view["next_action"]["code"] == "recalculate_after_correction"


def test_incomplete_submission_is_distinguished_from_missing():
    db, lifecycle, round_, entries, stats, canon = full_round(year=8911, afl_round=100, vacant_slots={(0, "F2")})
    afl = Facts(stats)
    view = _dashboard(db, afl, _season_id(lifecycle, round_), round_id=round_.bbbffl_round_id)
    row = next(r for r in view["lineups"] if r["season_entry_id"] == entries[0].season_entry_id)
    assert row["submission_state"] == SUBMISSION_INCOMPLETE


def test_draft_only_state_never_shown_as_submitted():
    lifecycle, round_, entries = operational(migrated_connection(), year=8912, afl_round=100)
    db = lifecycle.database
    lifecycle.transition(round_.bbbffl_round_id, "open")
    lineups = WeeklyLineupRepository(db)
    entry_id = entries[0].season_entry_id
    lineups.save_draft(
        _season_id(lifecycle, round_), round_.competition_id, round_.bbbffl_round_id, entry_id, {}, expected_revision=0
    )
    afl = Facts({})
    view = _dashboard(db, afl, _season_id(lifecycle, round_), round_id=round_.bbbffl_round_id)
    row = next(r for r in view["lineups"] if r["season_entry_id"] == entry_id)
    assert row["submission_state"] == SUBMISSION_DRAFT_ONLY
    assert row["submission_version"] is None


def test_refresh_reflects_authoritative_state_after_a_linked_mutation():
    """Issue #153: a second `build_scorer_dashboard` call after a mutation
    performed entirely through the owning workflow (never through this
    module) must show the new authoritative state -- proving nothing here
    caches across calls."""
    db, lifecycle, round_, entries, stats, canon = full_round(year=8913, afl_round=100)
    progress_to_review(lifecycle, round_.bbbffl_round_id)
    afl = Facts(stats)
    MatchupCalculationService(db, afl).calculate_round(round_.bbbffl_round_id)
    before = _dashboard(db, afl, _season_id(lifecycle, round_), round_id=round_.bbbffl_round_id)
    assert before["round"]["state"] == "review"
    identities = IdentityRepository(db)
    review_repo = RoundReviewRepository(db)
    attempt_signoff(
        lifecycle, review_repo, identities, round_.bbbffl_round_id, actor=ActorContext.anonymous_operator("scorer")
    )
    after = _dashboard(db, afl, _season_id(lifecycle, round_), round_id=round_.bbbffl_round_id)
    assert after["round"]["state"] == "final"
    assert after["round_options"][0]["state"] == "final"


class _CountingFacts(Facts):
    """`Facts` with a call counter (Codex review, PR #159): proves the
    dashboard fetches this round's AFL match list once per build, never
    once per team, even though every team's lineup readiness independently
    needs it for its own position-lock read."""

    def __init__(self, stats, status="CONCLUDED"):
        super().__init__(stats, status)
        self.calls = 0

    def get_matches(self, round_id):
        self.calls += 1
        return super().get_matches(round_id)


def test_dashboard_build_fetches_match_evidence_once_regardless_of_team_count():
    db, lifecycle, round_, entries, stats, canon = full_round(year=8914, afl_round=100)
    afl = _CountingFacts(stats)
    view = _dashboard(db, afl, _season_id(lifecycle, round_), round_id=round_.bbbffl_round_id)
    assert len(view["lineups"]) == 10
    assert afl.calls == 1


def test_live_round_with_matches_still_in_progress_waits_for_completion():
    db, lifecycle, round_, entries, scope, pool, ownership = lockout_context(year=8915)
    lineups = WeeklyLineupRepository(db)
    triggers = LockoutTriggerRepository(db)
    configure_main(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID])
    for entry in entries:
        establish(lineups, round_, entry, scope, {})
    lifecycle.transition(round_.bbbffl_round_id, "live")
    afl = type("Afl", (), {"get_matches": lambda self, rid: [early_match(status="LIVE", start=EARLY_START)]})()
    view = _dashboard(db, afl, _season_id(lifecycle, round_), round_id=round_.bbbffl_round_id)
    assert view["next_action"]["code"] == "await_match_completion"
    assert view["next_action"]["category"] == "waiting"


def test_live_round_with_all_matches_finished_offers_advance_to_review():
    db, lifecycle, round_, entries, scope, pool, ownership = lockout_context(year=8916)
    lineups = WeeklyLineupRepository(db)
    triggers = LockoutTriggerRepository(db)
    configure_main(triggers, round_.bbbffl_round_id, [EARLY_MATCH_ID])
    for entry in entries:
        establish(lineups, round_, entry, scope, {})
    lifecycle.transition(round_.bbbffl_round_id, "live")
    afl = type("Afl", (), {"get_matches": lambda self, rid: [early_match(status="CONCLUDED", start=EARLY_START)]})()
    view = _dashboard(db, afl, _season_id(lifecycle, round_), round_id=round_.bbbffl_round_id)
    assert view["next_action"]["code"] == "advance_to_review"
    assert view["next_action"]["capability"] == "round.review"
