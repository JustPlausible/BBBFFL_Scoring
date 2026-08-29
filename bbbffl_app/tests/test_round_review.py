"""Scorer round-review, sign-off and correction workflow (roadmap package
28, issue #58)."""

import pytest

from app.audit import ActorContext, AuditEventRepository
from app.calculations import MatchupCalculationService
from app.competition_lifecycle import StaleRoundVersionError
from app.identity import IdentityRepository
from app.round_review import (
    InvalidOverridePositionError,
    MissingOverrideReasonError,
    RoundReviewRepository,
    SignoffValidationError,
    UnauthorisedActorError,
    UnknownEntryError,
    UnknownMatchupError,
    attempt_correction,
    attempt_signoff,
    build_round_review,
)
from tests.round_review_helpers import Facts, full_round, progress_to_review

SCORER = ActorContext.anonymous_operator(role="scorer")
ADMIN = ActorContext.anonymous_operator(role="admin")


def _setup(year, *, stat_line=None, calculate=True):
    db, lifecycle, round_, entries, stats, canon = full_round(year=year, stat_line=stat_line)
    progress_to_review(lifecycle, round_.bbbffl_round_id)
    if calculate:
        MatchupCalculationService(db, Facts(stats)).calculate_round(round_.bbbffl_round_id)
    review_repo = RoundReviewRepository(db)
    identities = IdentityRepository(db)
    return db, lifecycle, round_, entries, stats, canon, review_repo, identities


# -- Review read model ----------------------------------------------------


def test_round_review_exposes_five_matchups_with_scores_and_lineup_versions():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3001)
    review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    assert len(review.matchups) == 5
    matchup_ids = {m.matchup_id for m in review.matchups}
    assert matchup_ids == {m.matchup_id for m in lifecycle.list_matchups(round_.bbbffl_round_id)}
    for m in review.matchups:
        assert m.calculation_revision == 1
        assert m.home.calculated_score > 0
        assert m.away.calculated_score > 0
        assert m.home.lineup_version == 1 and m.away.lineup_version == 1
        assert m.home.team_name and m.away.team_name
        assert m.home.coach_name and m.away.coach_name
        assert len(m.home.slots) == 8  # eight scorable slots, Interchange reported separately


def test_round_review_is_ready_when_every_matchup_has_unambiguous_evidence():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3002)
    review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    assert review.ready_for_signoff is True
    assert review.blockers == []
    assert all(m.eligible_for_signoff and not m.blockers for m in review.matchups)


def test_missing_calculation_blocks_that_matchup_and_the_round():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3003, calculate=False)
    review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    assert review.ready_for_signoff is False
    assert all(not m.eligible_for_signoff for m in review.matchups)
    assert all("no calculated result" in m.blockers[0] for m in review.matchups)


def test_ambiguous_evidence_surfaces_as_an_unresolved_ruling_blocker():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3004, calculate=False)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    missing_canonical = canon[(matchup.home_season_entry_id, "F1")]
    ambiguous_stats = dict(stats)
    del ambiguous_stats[missing_canonical]
    MatchupCalculationService(db, Facts(ambiguous_stats)).calculate_round(round_.bbbffl_round_id)

    review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    reviewed = next(m for m in review.matchups if m.matchup_id == matchup.matchup_id)
    assert reviewed.eligible_for_signoff is False
    assert any("F1" in reason and "unresolved" in reason for reason in reviewed.blockers)
    assert review.ready_for_signoff is False

    f1 = next(s for s in reviewed.home.slots if s.slot == "F1")
    assert f1.dnp_ruling is None
    assert f1.dnp_recommendation == "review_required"


def test_interchange_ruling_targeting_an_occupied_position_never_overrides_it():
    """A stale/invalid interchange ruling naming a position that is still
    occupied by its own non-DNP starter must never silently discard that
    starter's real score -- it must block sign-off instead."""
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3006)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    entry = matchup.home_season_entry_id
    review_repo.record_interchange_ruling(
        matchup.matchup_id, entry, "F1", expected_review_version=1, actor=SCORER, reason="stale/incorrect"
    )
    review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    reviewed = next(m for m in review.matchups if m.matchup_id == matchup.matchup_id)
    assert reviewed.eligible_for_signoff is False
    assert any("occupied" in reason for reason in reviewed.blockers)
    f1 = next(s for s in reviewed.home.slots if s.slot == "F1")
    assert f1.effective_score == f1.calculated_score  # untouched by the stale ruling


def test_existing_overrides_and_official_history_are_visible_on_the_review():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3005)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    review_repo.record_override(
        matchup.matchup_id,
        matchup.home_season_entry_id,
        "F2",
        77.0,
        20.0,
        "transcription correction",
        expected_review_version=1,
        actor=SCORER,
    )
    review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    reviewed = next(m for m in review.matchups if m.matchup_id == matchup.matchup_id)
    f2 = next(s for s in reviewed.home.slots if s.slot == "F2")
    assert f2.override_score == 77.0
    assert f2.override_reason == "transcription correction"
    assert f2.effective_score == 77.0
    assert reviewed.review_version == 2


# -- Rulings ----------------------------------------------------------------


def test_dnp_ruling_can_be_confirmed_with_actor_and_reason_retained():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3101)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    review_repo.record_dnp_ruling(
        matchup.matchup_id,
        matchup.home_season_entry_id,
        "F1",
        True,
        expected_review_version=1,
        actor=SCORER,
        reason="withdrew pregame",
    )
    rulings = review_repo.get_slot_rulings(matchup.matchup_id)
    ruling = rulings[(matchup.home_season_entry_id, "F1")]
    assert ruling.dnp is True
    assert ruling.decided_by_role == "scorer"
    assert ruling.reason == "withdrew pregame"
    assert ruling.decided_at

    events = AuditEventRepository(db).list_events(entity_type="review.slot_ruling")
    assert len(events) == 1
    assert events[0].action == "review.dnp_ruling.recorded"
    assert events[0].after_state == {"dnp": True}


def test_interchange_recommendation_can_be_confirmed_then_rejected():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3102)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    entry = matchup.home_season_entry_id
    v = review_repo.record_dnp_ruling(
        matchup.matchup_id, entry, "F1", True, expected_review_version=1, actor=SCORER, reason="dnp"
    )
    v = review_repo.record_interchange_ruling(
        matchup.matchup_id, entry, "F1", expected_review_version=v, actor=SCORER, reason="cover F1"
    )
    assert review_repo.get_interchange_rulings(matchup.matchup_id)[entry].target_position == "F1"

    # Reject/replace: an explicit "no coverage" ruling (target_position=None).
    v = review_repo.record_interchange_ruling(
        matchup.matchup_id, entry, None, expected_review_version=v, actor=SCORER, reason="leave vacant"
    )
    ruling = review_repo.get_interchange_rulings(matchup.matchup_id)[entry]
    assert ruling.target_position is None
    assert ruling.reason == "leave vacant"

    events = AuditEventRepository(db).list_events(entity_type="review.interchange_ruling")
    assert [e.after_state["target_position"] for e in events] == ["F1", None]


def test_stale_ruling_update_is_rejected_and_does_not_overwrite():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3103)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    entry = matchup.home_season_entry_id
    review_repo.record_dnp_ruling(
        matchup.matchup_id, entry, "F1", True, expected_review_version=1, actor=SCORER, reason="scorer A"
    )
    with pytest.raises(StaleRoundVersionError):
        review_repo.record_dnp_ruling(
            matchup.matchup_id, entry, "F1", False, expected_review_version=1, actor=SCORER, reason="scorer B, stale"
        )
    ruling = review_repo.get_slot_rulings(matchup.matchup_id)[(entry, "F1")]
    assert ruling.dnp is True and ruling.reason == "scorer A"


def test_ruling_for_unknown_entry_is_rejected():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3104)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    other_entry = next(
        e.season_entry_id
        for e in entries
        if e.season_entry_id not in (matchup.home_season_entry_id, matchup.away_season_entry_id)
    )
    with pytest.raises(UnknownEntryError):
        review_repo.record_dnp_ruling(
            matchup.matchup_id, other_entry, "F1", True, expected_review_version=1, actor=SCORER
        )


def test_ruling_rejects_a_matchup_from_a_different_round():
    """Regression: a caller scoping a ruling by round_id must never be
    able to mutate a matchup that actually belongs to a different round --
    see app/routes/round_review.py's URL-scoped endpoints."""
    db, lifecycle, round_a, entries_a, stats_a, canon_a, review_repo, identities = _setup(3105)
    _, lifecycle_b, round_b, entries_b, stats_b, canon_b = full_round(db, year=3106)
    other_round_matchup = lifecycle_b.list_matchups(round_b.bbbffl_round_id)[0]
    with pytest.raises(UnknownMatchupError):
        review_repo.record_dnp_ruling(
            other_round_matchup.matchup_id,
            other_round_matchup.home_season_entry_id,
            "F1",
            True,
            expected_review_version=1,
            actor=SCORER,
            round_id=round_a.bbbffl_round_id,
        )
    assert review_repo.get_slot_rulings(other_round_matchup.matchup_id) == {}


# -- Overrides ----------------------------------------------------------


def test_authorised_override_with_reason_succeeds_and_retains_both_values():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3201)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    review_repo.record_override(
        matchup.matchup_id,
        matchup.home_season_entry_id,
        "Ruck",
        88.5,
        41.0,
        "transcription correction after review",
        expected_review_version=1,
        actor=ADMIN,
    )
    override = review_repo.get_overrides(matchup.matchup_id)[(matchup.home_season_entry_id, "Ruck")]
    assert override.override_score == 88.5
    assert override.calculated_score == 41.0
    assert override.reason == "transcription correction after review"
    assert override.decided_by_role == "admin"


def test_override_missing_reason_fails():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3202)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    with pytest.raises(MissingOverrideReasonError):
        review_repo.record_override(
            matchup.matchup_id,
            matchup.home_season_entry_id,
            "Ruck",
            88.5,
            41.0,
            None,
            expected_review_version=1,
            actor=ADMIN,
        )
    assert review_repo.get_overrides(matchup.matchup_id) == {}


def test_override_by_unauthorised_actor_fails():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3203)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    with pytest.raises(UnauthorisedActorError):
        review_repo.record_override(
            matchup.matchup_id,
            matchup.home_season_entry_id,
            "Ruck",
            88.5,
            41.0,
            "reason",
            expected_review_version=1,
            actor=ActorContext.anonymous_operator(role="coach"),
        )
    assert review_repo.get_overrides(matchup.matchup_id) == {}


def test_override_on_invalid_position_fails():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3204)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    with pytest.raises(InvalidOverridePositionError):
        review_repo.record_override(
            matchup.matchup_id,
            matchup.home_season_entry_id,
            "Interchange",
            10.0,
            5.0,
            "reason",
            expected_review_version=1,
            actor=ADMIN,
        )


# -- Sign-off -----------------------------------------------------------


def test_complete_valid_round_publishes_atomically_with_frozen_inputs():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3301)
    matchups = lifecycle.list_matchups(round_.bbbffl_round_id)
    result = attempt_signoff(
        lifecycle, review_repo, identities, round_.bbbffl_round_id, actor=SCORER, reason="round complete"
    )
    assert result.state == "final"
    for matchup in matchups:
        history = lifecycle.result_history(matchup.matchup_id)
        assert [h.version for h in history] == [1]
        assert history[0].input_snapshot is not None
        assert history[0].input_snapshot["matchup_id"] == matchup.matchup_id
        assert history[0].input_snapshot["home"]["season_entry_id"] == matchup.home_season_entry_id
        assert lifecycle.get_matchup(matchup.matchup_id).effective_official_version == 1


def test_incomplete_evidence_blocks_publication_of_the_whole_round():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3302, calculate=False)
    with pytest.raises(SignoffValidationError) as excinfo:
        attempt_signoff(lifecycle, review_repo, identities, round_.bbbffl_round_id, actor=SCORER, reason="try anyway")
    assert len(excinfo.value.blockers) == 5
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "review"
    assert all(lifecycle.result_history(m.matchup_id) == [] for m in lifecycle.list_matchups(round_.bbbffl_round_id))


def test_unresolved_mandatory_ruling_blocks_publication():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3303, calculate=False)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    missing_canonical = canon[(matchup.home_season_entry_id, "F1")]
    ambiguous_stats = dict(stats)
    del ambiguous_stats[missing_canonical]
    MatchupCalculationService(db, Facts(ambiguous_stats)).calculate_round(round_.bbbffl_round_id)

    with pytest.raises(SignoffValidationError) as excinfo:
        attempt_signoff(lifecycle, review_repo, identities, round_.bbbffl_round_id, actor=SCORER, reason="try anyway")
    assert matchup.matchup_id in excinfo.value.blockers
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "review"
    assert lifecycle.effective_result(matchup.matchup_id) is None

    # Resolving the ruling (and the resulting interchange decision) unblocks it.
    entry = matchup.home_season_entry_id
    v = review_repo.record_dnp_ruling(
        matchup.matchup_id, entry, "F1", True, expected_review_version=1, actor=SCORER, reason="confirmed"
    )
    review_repo.record_interchange_ruling(
        matchup.matchup_id, entry, "F1", expected_review_version=v, actor=SCORER, reason="cover with interchange"
    )
    result = attempt_signoff(
        lifecycle, review_repo, identities, round_.bbbffl_round_id, actor=SCORER, reason="now ready"
    )
    assert result.state == "final"


def test_stale_evidence_blocks_publication():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3304)
    review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id, evidence_fresh=False)
    assert review.ready_for_signoff is False
    assert all("evidence" in reason for m in review.matchups for reason in m.blockers)
    with pytest.raises(SignoffValidationError):
        attempt_signoff(
            lifecycle,
            review_repo,
            identities,
            round_.bbbffl_round_id,
            actor=SCORER,
            reason="try anyway",
            evidence_fresh=False,
        )
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "review"


def test_stale_round_version_at_signoff_fails_closed():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3305)
    review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    matchup_ids = [m.matchup_id for m in review.matchups]
    results = {mid: (1.0, 1.0) for mid in matchup_ids}
    with pytest.raises(StaleRoundVersionError):
        lifecycle.publish_results(
            round_.bbbffl_round_id,
            results,
            actor=SCORER,
            reason="stale",
            expected_round_version=review.round_version + 1,
        )
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "review"


def test_stale_matchup_review_version_at_signoff_fails_closed():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3306)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    # Another scorer records a ruling after this scorer read the review.
    review_repo.record_dnp_ruling(
        matchup.matchup_id,
        matchup.home_season_entry_id,
        "F1",
        True,
        expected_review_version=1,
        actor=SCORER,
        reason="concurrent",
    )
    review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    stale_versions = {m.matchup_id: 1 for m in review.matchups}  # every matchup's revision *before* the ruling above
    results = {m.matchup_id: (m.home.effective_score, m.away.effective_score) for m in review.matchups}
    with pytest.raises(StaleRoundVersionError):
        lifecycle.publish_results(
            round_.bbbffl_round_id,
            results,
            actor=SCORER,
            reason="stale review",
            expected_round_version=review.round_version,
            expected_review_versions=stale_versions,
        )
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "review"


def test_signoff_failure_partway_rolls_back_all_five_matchups():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3307)

    def fail_after_third(count):
        if count == 3:
            raise RuntimeError("simulated publication failure")

    review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    results = {m.matchup_id: (m.home.effective_score, m.away.effective_score) for m in review.matchups}
    review_versions = {m.matchup_id: m.review_version for m in review.matchups}
    with pytest.raises(RuntimeError, match="simulated"):
        lifecycle.publish_results(
            round_.bbbffl_round_id,
            results,
            actor=SCORER,
            reason="atomicity test",
            expected_round_version=review.round_version,
            expected_review_versions=review_versions,
            failure_hook=fail_after_third,
        )
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "review"
    assert all(
        lifecycle.result_history(m.matchup_id) == [] and m.effective_official_version is None
        for m in lifecycle.list_matchups(round_.bbbffl_round_id)
    )


# -- Correction -----------------------------------------------------------


def test_correction_creates_version_two_preserving_version_one():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3401)
    attempt_signoff(lifecycle, review_repo, identities, round_.bbbffl_round_id, actor=SCORER, reason="initial")
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    original = lifecycle.effective_result(matchup.matchup_id)
    assert original.version == 1

    review_repo.record_override(
        matchup.matchup_id,
        matchup.home_season_entry_id,
        "F1",
        250.0,
        None,
        "transcription error discovered after publication",
        expected_review_version=lifecycle.get_matchup(matchup.matchup_id).review_version,
        actor=ADMIN,
    )
    corrected = attempt_correction(
        lifecycle, review_repo, identities, matchup.matchup_id, actor=ADMIN, reason="fix F1 transcription"
    )
    assert corrected.version == 2
    assert corrected.home_score != original.home_score

    history = lifecycle.result_history(matchup.matchup_id)
    assert [h.version for h in history] == [1, 2]
    assert history[0].home_score == original.home_score
    assert history[0].input_snapshot == original.input_snapshot
    assert lifecycle.get_matchup(matchup.matchup_id).effective_official_version == 2

    events = AuditEventRepository(db).list_events(entity_type="competition.matchup", entity_id=matchup.matchup_id)
    assert [e.action for e in events] == ["competition.result.published", "competition.result.corrected"]
    assert events[-1].reason == "fix F1 transcription"
    # The round never had to leave 'final' to accept the correction.
    assert lifecycle.get_round(round_.bbbffl_round_id).state == "final"


def test_correction_requires_a_reason():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3402)
    attempt_signoff(lifecycle, review_repo, identities, round_.bbbffl_round_id, actor=SCORER, reason="initial")
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    with pytest.raises(ValueError, match="reason"):
        attempt_correction(lifecycle, review_repo, identities, matchup.matchup_id, actor=ADMIN, reason="")


def test_stale_correction_attempt_fails_safely():
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3403)
    attempt_signoff(lifecycle, review_repo, identities, round_.bbbffl_round_id, actor=SCORER, reason="initial")
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    with pytest.raises(StaleRoundVersionError):
        lifecycle.correct_matchup_result(
            matchup.matchup_id, 10, 10, reason="stale", actor=ADMIN, expected_review_version=999
        )
    assert lifecycle.effective_result(matchup.matchup_id).version == 1


def test_a_second_correction_at_the_same_review_version_is_rejected_as_stale():
    """Regression: correcting a matchup did not itself advance
    `review_version`, so two corrections built from the same
    `expected_review_version` (neither having touched a ruling/override in
    between) could both pass the CAS check -- the second one silently
    superseding the first's freshly-published version rather than being
    rejected as stale."""
    db, lifecycle, round_, entries, stats, canon, review_repo, identities = _setup(3404)
    attempt_signoff(lifecycle, review_repo, identities, round_.bbbffl_round_id, actor=SCORER, reason="initial")
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    review_version = lifecycle.get_matchup(matchup.matchup_id).review_version

    lifecycle.correct_matchup_result(
        matchup.matchup_id, 111, 111, reason="first correction", actor=ADMIN, expected_review_version=review_version
    )
    assert lifecycle.effective_result(matchup.matchup_id).version == 2
    assert lifecycle.get_matchup(matchup.matchup_id).review_version == review_version + 1

    with pytest.raises(StaleRoundVersionError):
        lifecycle.correct_matchup_result(
            matchup.matchup_id,
            222,
            222,
            reason="second correction, same stale revision",
            actor=ADMIN,
            expected_review_version=review_version,
        )
    # Only the first correction's version 2 exists; no duplicate version 3.
    history = lifecycle.result_history(matchup.matchup_id)
    assert [h.version for h in history] == [1, 2]
    assert lifecycle.effective_result(matchup.matchup_id).version == 2
    assert lifecycle.effective_result(matchup.matchup_id).home_score == 111
