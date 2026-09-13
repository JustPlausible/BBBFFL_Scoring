"""Issue #192: SuperScore roster, eligibility and round lifecycle setup --
service-level coverage. HTTP-route coverage (coach lineup, adjudication,
correction) lives in tests/test_superscore_routes.py.

Covers: stream/round creation, atomic ten-row review-state setup, the
round-cannot-open-incomplete gate, all-ten-entry eligibility, the review-
state row's lock/advance behaviour across submit/submit_positions/
submit_correction, the entry-scoped ruling boundary (and its correction-
time invalidation), that no SuperScore path ever needs a `matchup_id`, and
that `app.participation.assess_participation` is reused unchanged.
"""

import pytest

from app.audit import ActorContext
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.lineup_correction import LineupCorrectionService
from app.lineups import LineupConflictError, WeeklyLineupRepository
from app.participation import ParticipationState, assess_participation
from app.superscore_participation import SuperScoreParticipantError, require_superscore_entry_eligible
from app.superscore_review import (
    StaleReviewVersionError,
    SuperScoreReviewRepository,
    UnknownReviewStateError,
    recommend_dnp_evidence,
)
from app.superscore_round import (
    EXPECTED_ENTRY_COUNT,
    IncompleteReviewStateError,
    eligible_entries,
    ensure_round,
    ensure_stream,
    get_review_state,
    open_round,
    review_state_complete,
    setup_round,
)
from tests.finals_helpers import KnownRound
from tests.finals_seeding_helpers import build_2026_replay_season
from tests.superscore_helpers import FINALS_AFL_ROUNDS, build_superscore_ready_season

ACTOR = ActorContext.anonymous_operator("test")


class _NoMatchesAflClient:
    """Duck-typed AFL client with no live network dependency -- only the
    review-state/ruling-invalidation wiring is under test here, never
    lockout/lock-state mechanics (covered exhaustively elsewhere)."""

    def get_matches(self, round_id):
        return []


def _built(year):
    return build_superscore_ready_season(year=year)


def _fresh_superscore_stream(year):
    """A season/superscore-stream pair with **no** SS rounds created yet --
    unlike `_built`/`build_superscore_ready_season`, which already fully
    sets up SS1-SS4 -- so a test can exercise `setup_round`/`open_round`
    against a genuinely fresh round of its own."""
    built = build_2026_replay_season(year=year)
    database, season = built["database"], built["season"]
    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (season.season_id,)
    ).fetchone()
    stream = ensure_stream(
        database, season.season_id, rules_row["rules_version_id"], built["competition"].competition_id
    )
    built["superscore_stream"] = stream
    return built


# -- Stream/round creation ----------------------------------------------


def test_ensure_stream_and_round_are_idempotent():
    built = _built(4001)
    database, season = built["database"], built["season"]
    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (season.season_id,)
    ).fetchone()
    again = ensure_stream(database, season.season_id, rules_row["rules_version_id"], built["ordinary_competition_id"])
    assert again.competition_id == built["superscore_stream"].competition_id

    round_id_again = ensure_round(database, built["superscore_stream"].competition_id, 1, 1)
    assert round_id_again == built["superscore_rounds"][1]


def test_ensure_stream_rejects_a_conflicting_ordinary_competition_id():
    built = _built(4002)
    database, season = built["database"], built["season"]
    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (season.season_id,)
    ).fetchone()
    other = database.execute(
        "SELECT competition_id FROM competition_stream WHERE competition_id != ? LIMIT 1",
        (built["ordinary_competition_id"],),
    ).fetchone()
    with pytest.raises(Exception, match="already exists"):
        ensure_stream(database, season.season_id, rules_row["rules_version_id"], other["competition_id"])


def test_ensure_stream_rejects_an_unknown_ordinary_competition_id_without_orphaning_anything():
    """Regression (PR #204 review, P2): `superscore_stream.ordinary_
    competition_id` carries a foreign key to `competition_stream`, but
    `create_competition` commits the new SuperScore `competition_stream`
    row in its own transaction (`app.db.transaction` never nests) before
    that FK is ever checked -- so a bogus id used to fail only *after* an
    orphaned `competition_stream` row was already committed, permanently
    blocking a retry on the `(season_id, stream_key)` uniqueness
    constraint. `ensure_stream` now validates the id up front, so nothing
    is created at all on bad input and a retry with the real id succeeds."""
    built = build_2026_replay_season(year=4009)
    database, season = built["database"], built["season"]
    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (season.season_id,)
    ).fetchone()

    with pytest.raises(Exception, match="ordinary_competition_id"):
        ensure_stream(database, season.season_id, rules_row["rules_version_id"], "not-a-real-competition-id")

    orphan = database.execute(
        "SELECT 1 FROM competition_stream WHERE season_id=? AND stream_key='superscore'", (season.season_id,)
    ).fetchone()
    assert orphan is None

    stream = ensure_stream(
        database, season.season_id, rules_row["rules_version_id"], built["competition"].competition_id
    )
    assert stream.ordinary_competition_id == built["competition"].competition_id


def test_ensure_stream_rejects_an_ordinary_competition_id_from_a_different_season():
    """`ordinary_competition_id` must be *this* season's own ordinary
    competition, exactly as `app.finals`'s identical `_resolve_seed` check
    requires for `finals_bracket.ordinary_competition_id` -- a real
    `competition_stream` row from another season passes the foreign key
    but must still be rejected."""
    built = build_2026_replay_season(year=4010)
    other_season = build_2026_replay_season(database=built["database"], year=4011)
    database, season = built["database"], built["season"]
    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (season.season_id,)
    ).fetchone()

    with pytest.raises(Exception, match="ordinary_competition_id"):
        ensure_stream(
            database, season.season_id, rules_row["rules_version_id"], other_season["competition"].competition_id
        )


# -- Atomic ten-row review-state setup ------------------------------------


def test_setup_round_creates_all_ten_review_state_rows_atomically():
    built = _built(4003)
    database = built["database"]
    for round_id in built["superscore_rounds"].values():
        assert review_state_complete(database, round_id)
        versions = {
            row["season_entry_id"]: row["review_version"]
            for row in database.execute(
                "SELECT season_entry_id, review_version FROM superscore_entry_review_state WHERE bbbffl_round_id=?",
                (round_id,),
            ).fetchall()
        }
        assert set(versions) == {e.season_entry_id for e in built["entries"]}
        assert set(versions.values()) == {0}


def test_setup_round_is_idempotent():
    built = _built(4004)
    database = built["database"]
    round_id = built["superscore_rounds"][1]
    setup_round(database, round_id, actor=ACTOR, reason="re-run setup")
    count = database.execute(
        "SELECT COUNT(*) AS n FROM superscore_entry_review_state WHERE bbbffl_round_id=?", (round_id,)
    ).fetchone()["n"]
    assert count == EXPECTED_ENTRY_COUNT


def test_setup_round_fails_atomically_when_entry_count_is_wrong(monkeypatch):
    built = _fresh_superscore_stream(4005)
    database, season = built["database"], built["season"]
    # Easier to prove the atomic-failure path by monkeypatching
    # `eligible_entries` to return an incomplete list for this one call
    # rather than constructing a genuinely nine-entry season.
    import app.superscore_round as superscore_round_module

    real_eligible_entries = superscore_round_module.eligible_entries
    monkeypatch.setattr(superscore_round_module, "eligible_entries", lambda db, sid: real_eligible_entries(db, sid)[:9])
    round_id = ensure_round(database, built["superscore_stream"].competition_id, 1, 1)
    from app.superscore_round import confirm_afl_mapping

    confirm_afl_mapping(
        database,
        KnownRound({(season.year, FINALS_AFL_ROUNDS[1])}),
        round_id,
        season.year,
        FINALS_AFL_ROUNDS[1],
        reason="mapping for the broken-entry-count round",
    )
    with pytest.raises(IncompleteReviewStateError):
        setup_round(database, round_id, actor=ACTOR, reason="broken setup")
    # Nothing committed -- the whole review-state transaction rolled back.
    count = database.execute(
        "SELECT COUNT(*) AS n FROM superscore_entry_review_state WHERE bbbffl_round_id=?", (round_id,)
    ).fetchone()["n"]
    assert count == 0
    # The round-lifecycle row itself, created by an earlier, independent
    # call inside `setup_round`, is unaffected by the later rollback --
    # only the review-state-row transaction failed.
    assert CompetitionLifecycleRepository(database).get_round(round_id) is not None


# -- Round cannot open with an incomplete review-state set ----------------


def test_round_cannot_open_with_incomplete_review_state():
    built = _fresh_superscore_stream(4006)
    database, season = built["database"], built["season"]
    round_id = ensure_round(database, built["superscore_stream"].competition_id, 2, 2)
    from app.superscore_round import confirm_afl_mapping

    confirm_afl_mapping(
        database,
        KnownRound({(season.year, FINALS_AFL_ROUNDS[2])}),
        round_id,
        season.year,
        FINALS_AFL_ROUNDS[2],
        reason="mapping only, no setup",
    )
    # Deliberately bypass `setup_round`: create the lifecycle row directly
    # (as #197's own primitive allows) without ever creating the durable
    # review-state row set.
    CompetitionLifecycleRepository(database).create_non_ordinary_round(round_id, actor=ACTOR, reason="no setup")
    assert not review_state_complete(database, round_id)
    with pytest.raises(IncompleteReviewStateError):
        open_round(database, round_id, actor=ACTOR, reason="must not open")
    assert CompetitionLifecycleRepository(database).get_round(round_id).state == "upcoming"

    # Running setup afterwards makes it openable.
    setup_round(database, round_id, actor=ACTOR, reason="late setup")
    opened = open_round(database, round_id, actor=ACTOR, reason="now openable")
    assert opened.state == "open"


# -- All ten entries are eligible and can submit --------------------------


def test_all_ten_entries_can_submit_lineups():
    built = _built(4007)
    database = built["database"]
    round_id = built["superscore_rounds"][1]
    entries = eligible_entries(database, built["season"].season_id)
    assert len(entries) == EXPECTED_ENTRY_COUNT
    open_round(database, round_id, actor=ACTOR, reason="open for submission")
    lineups = WeeklyLineupRepository(database)
    for entry in built["entries"]:
        squad = built["ownership"].current_squad(entry.season_entry_id)
        positions = {"F1": squad[0].season_player_id}
        draft = lineups.save_draft(
            built["season"].season_id,
            built["superscore_stream"].competition_id,
            round_id,
            entry.season_entry_id,
            positions,
            expected_revision=0,
        )
        submission = lineups.submit(
            draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0
        )
        assert submission.version == 1
        assert get_review_state(database, round_id, entry.season_entry_id) == 1


# -- Review-state row lock/advance behaviour -------------------------------


def _open_and_submit(built, round_id, entry, *, position="F1"):
    database = built["database"]
    squad = built["ownership"].current_squad(entry.season_entry_id)
    lineups = WeeklyLineupRepository(database)
    draft = lineups.save_draft(
        built["season"].season_id,
        built["superscore_stream"].competition_id,
        round_id,
        entry.season_entry_id,
        {position: squad[0].season_player_id},
        expected_revision=0,
    )
    return lineups.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0)


def test_unlocked_resubmission_via_submit_increments_review_state():
    built = _built(4008)
    database = built["database"]
    round_id = built["superscore_rounds"][1]
    open_round(database, round_id, actor=ACTOR, reason="open")
    entry = built["entries"][0]
    submitted = _open_and_submit(built, round_id, entry)
    assert get_review_state(database, round_id, entry.season_entry_id) == 1

    lineups = WeeklyLineupRepository(database)
    squad = built["ownership"].current_squad(entry.season_entry_id)
    draft = lineups.save_draft(
        built["season"].season_id,
        built["superscore_stream"].competition_id,
        round_id,
        entry.season_entry_id,
        {"F1": squad[0].season_player_id, "F2": squad[1].season_player_id},
        expected_revision=1,
    )
    resubmitted = lineups.submit(
        draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=submitted.version
    )
    assert resubmitted.version == 2
    assert get_review_state(database, round_id, entry.season_entry_id) == 2


def test_submit_positions_resubmission_increments_review_state():
    built = _built(4009)
    database = built["database"]
    round_id = built["superscore_rounds"][1]
    open_round(database, round_id, actor=ACTOR, reason="open")
    entry = built["entries"][0]
    submitted = _open_and_submit(built, round_id, entry)
    lineups = WeeklyLineupRepository(database)
    squad = built["ownership"].current_squad(entry.season_entry_id)
    lineup_id, _ = lineups.get_or_create_header(
        built["season"].season_id, built["superscore_stream"].competition_id, round_id, entry.season_entry_id
    )
    resubmitted = lineups.submit_positions(
        lineup_id,
        {"F1": squad[1].season_player_id},
        expected_submission_version=submitted.version,
        actor=ACTOR,
        source_type="scorer_proxy",
    )
    assert resubmitted.version == 2
    assert get_review_state(database, round_id, entry.season_entry_id) == 2


def test_correction_increments_review_state():
    built = _built(4010)
    database = built["database"]
    round_id = built["superscore_rounds"][1]
    open_round(database, round_id, actor=ACTOR, reason="open")
    entry = built["entries"][0]
    _open_and_submit(built, round_id, entry)
    assert get_review_state(database, round_id, entry.season_entry_id) == 1

    service = LineupCorrectionService(database, afl_client=_NoMatchesAflClient())
    service.correct(
        built["season"].season_id,
        built["superscore_stream"].competition_id,
        round_id,
        entry.season_entry_id,
        {"F2": built["ownership"].current_squad(entry.season_entry_id)[1].season_player_id},
        expected_submission_version=1,
        actor=ActorContext.anonymous_operator("scorer"),
        reason="correction increments review state",
    )
    assert get_review_state(database, round_id, entry.season_entry_id) == 2


def test_failed_submission_does_not_advance_review_state():
    built = _built(4011)
    database = built["database"]
    round_id = built["superscore_rounds"][1]
    open_round(database, round_id, actor=ACTOR, reason="open")
    entry = built["entries"][0]
    _open_and_submit(built, round_id, entry)
    assert get_review_state(database, round_id, entry.season_entry_id) == 1

    lineups = WeeklyLineupRepository(database)
    lineup_id, _ = lineups.get_or_create_header(
        built["season"].season_id, built["superscore_stream"].competition_id, round_id, entry.season_entry_id
    )
    squad = built["ownership"].current_squad(entry.season_entry_id)
    with pytest.raises(LineupConflictError):
        lineups.submit_positions(
            lineup_id,
            {"F1": squad[2].season_player_id},
            expected_submission_version=0,  # stale: already at 1
            actor=ACTOR,
            source_type="scorer_proxy",
        )
    assert get_review_state(database, round_id, entry.season_entry_id) == 1


def test_ruling_increments_review_state_and_failed_ruling_does_not():
    built = _built(4012)
    database = built["database"]
    round_id = built["superscore_rounds"][1]
    open_round(database, round_id, actor=ACTOR, reason="open")
    entry = built["entries"][0]
    _open_and_submit(built, round_id, entry)
    assert get_review_state(database, round_id, entry.season_entry_id) == 1

    reviews = SuperScoreReviewRepository(database)
    new_version = reviews.record_dnp_ruling(
        round_id,
        entry.season_entry_id,
        "F1",
        True,
        expected_review_version=1,
        actor=ActorContext.anonymous_operator("scorer"),
        reason="confirmed DNP",
    )
    assert new_version == 2
    assert get_review_state(database, round_id, entry.season_entry_id) == 2

    with pytest.raises(StaleReviewVersionError):
        reviews.record_dnp_ruling(
            round_id,
            entry.season_entry_id,
            "F1",
            False,
            expected_review_version=1,  # stale: already at 2
            actor=ActorContext.anonymous_operator("scorer"),
            reason="stale retry",
        )
    assert get_review_state(database, round_id, entry.season_entry_id) == 2

    reviews.record_interchange_ruling(
        round_id,
        entry.season_entry_id,
        "F1",
        expected_review_version=2,
        actor=ActorContext.anonymous_operator("scorer"),
        reason="interchange target",
    )
    assert get_review_state(database, round_id, entry.season_entry_id) == 3

    reviews.record_override(
        round_id,
        entry.season_entry_id,
        "F2",
        12.5,
        4.0,
        "manual override",
        expected_review_version=3,
        actor=ActorContext.anonymous_operator("scorer"),
    )
    assert get_review_state(database, round_id, entry.season_entry_id) == 4


def test_ruling_requires_an_existing_review_state_row():
    built = _built(4013)
    database = built["database"]
    reviews = SuperScoreReviewRepository(database)
    with pytest.raises(UnknownReviewStateError):
        reviews.record_dnp_ruling(
            "no-such-round",
            "no-such-entry",
            "F1",
            True,
            expected_review_version=0,
            actor=ActorContext.anonymous_operator("scorer"),
            reason="no row",
        )


def test_correction_atomically_invalidates_prior_entry_scoped_ruling():
    built = _built(4014)
    database = built["database"]
    round_id = built["superscore_rounds"][1]
    open_round(database, round_id, actor=ACTOR, reason="open")
    entry = built["entries"][0]
    _open_and_submit(built, round_id, entry)

    reviews = SuperScoreReviewRepository(database)
    reviews.record_dnp_ruling(
        round_id,
        entry.season_entry_id,
        "F1",
        True,
        expected_review_version=1,
        actor=ActorContext.anonymous_operator("scorer"),
        reason="DNP before correction",
    )
    assert "F1" in reviews.get_slot_rulings(round_id, entry.season_entry_id)
    assert get_review_state(database, round_id, entry.season_entry_id) == 2

    service = LineupCorrectionService(database, afl_client=_NoMatchesAflClient())
    service.correct(
        built["season"].season_id,
        built["superscore_stream"].competition_id,
        round_id,
        entry.season_entry_id,
        {"F1": built["ownership"].current_squad(entry.season_entry_id)[1].season_player_id},
        expected_submission_version=1,
        actor=ActorContext.anonymous_operator("scorer"),
        reason="replace the ruled-DNP player",
    )
    # The stale ruling against the pre-correction occupant must not
    # silently keep applying to the replacement player.
    assert "F1" not in reviews.get_slot_rulings(round_id, entry.season_entry_id)
    assert get_review_state(database, round_id, entry.season_entry_id) == 3


def test_correction_invalidation_leaves_other_positions_untouched():
    built = _built(4015)
    database = built["database"]
    round_id = built["superscore_rounds"][1]
    open_round(database, round_id, actor=ACTOR, reason="open")
    entry = built["entries"][0]
    lineups = WeeklyLineupRepository(database)
    squad = built["ownership"].current_squad(entry.season_entry_id)
    draft = lineups.save_draft(
        built["season"].season_id,
        built["superscore_stream"].competition_id,
        round_id,
        entry.season_entry_id,
        {"F1": squad[0].season_player_id, "F2": squad[1].season_player_id},
        expected_revision=0,
    )
    lineups.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0)

    reviews = SuperScoreReviewRepository(database)
    reviews.record_dnp_ruling(
        round_id,
        entry.season_entry_id,
        "F1",
        True,
        expected_review_version=1,
        actor=ActorContext.anonymous_operator("scorer"),
        reason="F1 DNP",
    )
    reviews.record_dnp_ruling(
        round_id,
        entry.season_entry_id,
        "F2",
        True,
        expected_review_version=2,
        actor=ActorContext.anonymous_operator("scorer"),
        reason="F2 DNP",
    )

    service = LineupCorrectionService(database, afl_client=_NoMatchesAflClient())
    service.correct(
        built["season"].season_id,
        built["superscore_stream"].competition_id,
        round_id,
        entry.season_entry_id,
        {"F1": squad[2].season_player_id},
        expected_submission_version=1,
        actor=ActorContext.anonymous_operator("scorer"),
        reason="only replace F1",
    )
    rulings = reviews.get_slot_rulings(round_id, entry.season_entry_id)
    assert "F1" not in rulings
    assert "F2" in rulings  # untouched by the correction


# -- No SuperScore path ever needs a matchup_id ---------------------------


def test_no_matchup_rows_are_ever_created_for_superscore():
    built = _built(4016)
    database = built["database"]
    round_id = built["superscore_rounds"][1]
    open_round(database, round_id, actor=ACTOR, reason="open")
    entry = built["entries"][0]
    _open_and_submit(built, round_id, entry)
    reviews = SuperScoreReviewRepository(database)
    reviews.record_dnp_ruling(
        round_id,
        entry.season_entry_id,
        "F1",
        True,
        expected_review_version=1,
        actor=ActorContext.anonymous_operator("scorer"),
        reason="no matchup needed",
    )
    count = database.execute(
        "SELECT COUNT(*) AS n FROM bbbffl_matchup WHERE bbbffl_round_id=?", (round_id,)
    ).fetchone()["n"]
    assert count == 0


# -- app.participation reuse -----------------------------------------------


def test_superscore_review_reuses_participation_evidence_unchanged():
    direct = assess_participation(afl_team_id=1, bye_team_ids=frozenset({1}), match=None, stat_line=None)
    via_wrapper = recommend_dnp_evidence(afl_team_id=1, bye_team_ids=frozenset({1}), match=None, stat_line=None)
    assert direct == via_wrapper
    assert via_wrapper.state == ParticipationState.CLUB_BYE


# -- Calculation persistence must never be a review-version advancer ------


def test_locking_and_reading_the_review_state_row_never_advances_it():
    """No calculation service exists yet in this issue's scope (#193 owns
    it) -- this proves the row itself supports the read-only lock/compare
    contract #193 needs without being mutated by it, i.e. nothing about
    `superscore_entry_review_state`'s own shape or this issue's code
    advances `review_version` on a mere locked read."""
    built = _built(4017)
    database = built["database"]
    round_id = built["superscore_rounds"][1]
    open_round(database, round_id, actor=ACTOR, reason="open")
    entry = built["entries"][0]
    _open_and_submit(built, round_id, entry)
    before = get_review_state(database, round_id, entry.season_entry_id)

    from app.db import _for_update_suffix, transaction

    with transaction(database) as conn:
        row = conn.execute(
            "SELECT review_version FROM superscore_entry_review_state WHERE bbbffl_round_id=? AND season_entry_id=?"
            + _for_update_suffix(database),
            (round_id, entry.season_entry_id),
        ).fetchone()
        assert row["review_version"] == before
        # A real calculation service would record `computed_as_of_review_version`
        # on its own snapshot row here -- it must never write back to this
        # row itself.

    assert get_review_state(database, round_id, entry.season_entry_id) == before


# -- Explicit all-ten-entry eligibility enforcement ------------------------


def test_require_superscore_entry_eligible_accepts_every_season_entry():
    built = _built(4018)
    database = built["database"]
    for entry in built["entries"]:
        require_superscore_entry_eligible(database, built["superscore_stream"].competition_id, entry.season_entry_id)


def test_require_superscore_entry_eligible_rejects_a_foreign_season_entry():
    built = _built(4019)
    other = build_2026_replay_season(database=built["database"], year=4020)
    with pytest.raises(SuperScoreParticipantError):
        require_superscore_entry_eligible(
            built["database"], built["superscore_stream"].competition_id, other["entries"][0].season_entry_id
        )


def test_require_superscore_entry_eligible_is_a_no_op_for_ordinary_streams():
    built = _built(4021)
    other = build_2026_replay_season(database=built["database"], year=4022)
    # A no-op for a non-superscore competition_id, regardless of entry --
    # ordinary/finals eligibility is enforced by their own adapters.
    require_superscore_entry_eligible(
        built["database"], built["ordinary_competition_id"], other["entries"][0].season_entry_id
    )


# -- Completed-season write fence (issue #194, Codex review P1) -----------


def test_review_rulings_refuse_once_the_season_is_completed():
    """A DNP/interchange/override ruling must never remain writable after
    `app.season_completion.complete_season` -- otherwise the archival guard
    (`app.season_archival`) would be verifying a completion identifier that
    review state could still change underneath, after the fact."""
    from app.audit import ActorContext
    from app.season import SeasonCompletedError
    from tests.season_completion_helpers import build_completable_season

    built = build_completable_season(year=4023)
    database = built["database"]
    from app.season_completion import complete_season

    complete_season(database, built["season"].season_id, actor=ACTOR, reason="issue #194 write-fence regression test")

    round_id = built["superscore_rounds"][1]
    entry_id = built["entries"][0].season_entry_id
    reviews = SuperScoreReviewRepository(database)
    scorer = ActorContext.anonymous_operator("scorer")

    with pytest.raises(SeasonCompletedError):
        reviews.record_dnp_ruling(
            round_id, entry_id, "F1", True, expected_review_version=0, actor=scorer, reason="must refuse"
        )
    with pytest.raises(SeasonCompletedError):
        reviews.record_interchange_ruling(
            round_id, entry_id, "F1", expected_review_version=0, actor=scorer, reason="must refuse"
        )
    with pytest.raises(SeasonCompletedError):
        reviews.record_override(
            round_id, entry_id, "F2", 12.5, 4.0, "must refuse", expected_review_version=0, actor=scorer
        )
