"""Issue #195 acceptance coverage: premiership/wooden-spoon recording, the
audited re-recording path, and the atomic six-step `active -> completed`
season-completion command. Concurrency between a correction and completion
is covered separately in test_season_completion_postgresql.py; the shared
completed-season write fence's wiring into every result-changing path is
covered in test_completed_season_fence.py."""

from app.audit import ActorContext, AuditEventRepository
from app.finals import FinalsBracketRepository
from app.season import SeasonCompletedError, SeasonRepository
from app.season_awards import (
    PREMIERSHIP,
    WOODEN_SPOON,
    SeasonAwardRepository,
    UnresolvedWoodenSpoonTieError,
    reconcile_premiership,
    reconcile_wooden_spoon,
)
from app.season_completion import SeasonNotReadyError, complete_season, preview_complete_season
from tests.finals_helpers import correct_official_result
from tests.finals_seeding_helpers import all_draws, build_2026_replay_season
from tests.season_completion_helpers import build_completable_season

ACTOR = ActorContext.anonymous_operator("test")


# -- The completion command is the only path to "completed" -----------------


def test_transition_lifecycle_refuses_to_reach_completed_directly():
    """Codex review, PR #206: the generic, unconditional `transition_
    lifecycle` must never be usable to reach `completed` directly -- that
    would bypass `complete_season`'s readiness gate and award
    materialisation entirely, permanently locking the season (via the write
    fence) with missing or stale historical facts. Only `complete_season`
    may perform this transition."""
    built = build_completable_season(year=5102)
    database, season_id = built["database"], built["season"].season_id

    try:
        SeasonRepository(database).transition_lifecycle(season_id, "completed", actor=ACTOR, reason="bypass attempt")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "app.season_completion.complete_season" in str(exc)

    season = SeasonRepository(database).get_season(season_id)
    assert season.lifecycle_state == "active"
    assert SeasonAwardRepository(database).get_active(season_id, PREMIERSHIP) is None


# -- Premiership/wooden-spoon recording --------------------------------------


def test_completion_records_premiership_and_wooden_spoon_referencing_effective_versions():
    built = build_completable_season(year=5100)
    database, season_id = built["database"], built["season"].season_id

    result = complete_season(database, season_id, actor=ACTOR, reason="complete 2026 replay")

    premiership = result.premiership_award
    assert premiership.award_type == PREMIERSHIP
    assert premiership.status == "active"
    assert premiership.provenance["bracket_id"] == built["bracket"].bracket_id
    assert premiership.provenance["grand_final_matchup_id"] == built["grand_final_matchup_id"]
    assert premiership.provenance["official_version"] == 1
    assert premiership.runner_up_season_entry_id is not None
    assert premiership.runner_up_season_entry_id != premiership.season_entry_id

    wooden_spoon = result.wooden_spoon_award
    assert wooden_spoon.award_type == WOODEN_SPOON
    assert wooden_spoon.status == "active"
    assert wooden_spoon.provenance["ordinary_competition_id"] == built["ordinary_competition_id"]
    assert wooden_spoon.provenance["through_round"] == 20
    assert len(wooden_spoon.provenance["result_references"]) == 100  # 10 rounds x 10 matches (20 rounds / 2)
    assert wooden_spoon.runner_up_season_entry_id is None

    # Both are queryable via the ordinary read boundary independent of the
    # `CompletionResult` object returned above.
    repo = SeasonAwardRepository(database)
    assert repo.get_active(season_id, PREMIERSHIP).award_id == premiership.award_id
    assert repo.get_active(season_id, WOODEN_SPOON).award_id == wooden_spoon.award_id


def test_completion_records_a_single_season_completed_audit_event_alongside_lifecycle_changed():
    built = build_completable_season(year=5101)
    database, season_id = built["database"], built["season"].season_id

    result = complete_season(database, season_id, actor=ACTOR, reason="complete 2026 replay")

    events = AuditEventRepository(database).list_events(entity_type="season", entity_id=season_id)
    completed_events = [e for e in events if e.action == "season.completed"]
    lifecycle_events = [
        e
        for e in events
        if e.action == "season.lifecycle.changed" and e.after_state == {"lifecycle_state": "completed"}
    ]
    assert len(completed_events) == 1
    assert completed_events[0].event_id == result.completion_event_id
    assert len(lifecycle_events) == 1
    assert completed_events[0].payload["premiership_award_id"] == result.premiership_award.award_id
    assert completed_events[0].payload["wooden_spoon_award_id"] == result.wooden_spoon_award.award_id


# -- Idempotent, audited re-recording path -----------------------------------


def test_reconcile_premiership_is_idempotent_against_unchanged_effective_version():
    built = build_completable_season(year=5110)
    database, season_id = built["database"], built["season"].season_id

    first, first_created = reconcile_premiership(database, season_id, actor=ACTOR, reason="initial recording")
    second, second_created = reconcile_premiership(database, season_id, actor=ACTOR, reason="repeat, unchanged")

    assert first_created is True
    assert second_created is False
    assert first.award_id == second.award_id
    assert SeasonAwardRepository(database).history(season_id, PREMIERSHIP) == [first]


def test_reconcile_wooden_spoon_is_idempotent_against_unchanged_effective_version():
    built = build_completable_season(year=5111)
    database, season_id = built["database"], built["season"].season_id

    first, first_created = reconcile_wooden_spoon(database, season_id, actor=ACTOR, reason="initial recording")
    second, second_created = reconcile_wooden_spoon(database, season_id, actor=ACTOR, reason="repeat, unchanged")

    assert first_created is True
    assert second_created is False
    assert first.award_id == second.award_id


def test_grand_final_correction_supersedes_the_frozen_premiership_record_not_overwrites_it():
    built = build_completable_season(year=5120)
    database, season_id = built["database"], built["season"].season_id
    gf_matchup_id = built["grand_final_matchup_id"]

    original, _ = reconcile_premiership(database, season_id, actor=ACTOR, reason="initial recording")

    # Reverse the Grand Final result (home originally won 100-50).
    correct_official_result(database, gf_matchup_id, 10, 200, reason="corrected GF evidence reverses the result")

    superseded, created = reconcile_premiership(
        database, season_id, actor=ACTOR, reason="re-recording after GF correction"
    )

    assert created is True
    assert superseded.award_id != original.award_id
    assert superseded.season_entry_id != original.season_entry_id
    assert superseded.provenance["official_version"] == 2

    repo = SeasonAwardRepository(database)
    assert repo.get_active(season_id, PREMIERSHIP).award_id == superseded.award_id
    history = repo.history(season_id, PREMIERSHIP)
    old = next(a for a in history if a.award_id == original.award_id)
    assert old.status == "superseded"
    assert old.superseded_by_award_id == superseded.award_id


def test_round20_correction_supersedes_the_frozen_wooden_spoon_record_not_overwrites_it():
    """`build_completable_season`'s ordinary competition uses `tests.
    midseason_draft_helpers.dominant_scores`: `entries[9]` (last-created)
    loses every match it plays, so it is always the mathematical wooden
    spoon before any correction. Reversing *every* head-to-head result
    between `entries[9]` and `entries[8]` (the only two matches where
    flipping the outcome can plausibly change who finishes last, since
    every other match's result is unaffected) is enough to swap their
    relative ladder order and hand the wooden spoon to `entries[8]`
    instead -- a single reversed result would not overcome the many other
    losses each of them otherwise has."""
    built = build_completable_season(year=5121)
    database, season_id = built["database"], built["season"].season_id
    last_place_entry = built["entries"][9].season_entry_id
    second_last_entry = built["entries"][8].season_entry_id

    original, _ = reconcile_wooden_spoon(database, season_id, actor=ACTOR, reason="initial recording")
    assert original.season_entry_id == last_place_entry

    head_to_head = database.execute(
        "SELECT m.matchup_id, m.home_season_entry_id, m.away_season_entry_id "
        "FROM bbbffl_matchup m JOIN bbbffl_round_lifecycle l ON l.bbbffl_round_id = m.bbbffl_round_id "
        "WHERE l.competition_id=? AND l.fixture_round_number<=20 "
        "AND ((m.home_season_entry_id=? AND m.away_season_entry_id=?) "
        "OR (m.home_season_entry_id=? AND m.away_season_entry_id=?))",
        (
            built["ordinary_competition_id"],
            last_place_entry,
            second_last_entry,
            second_last_entry,
            last_place_entry,
        ),
    ).fetchall()
    assert head_to_head  # the round-robin draw meets them at least once
    for match in head_to_head:
        home_is_last_place = match["home_season_entry_id"] == last_place_entry
        # Reverse the result: the previously-last entry now wins big.
        new_scores = (500, 1) if home_is_last_place else (1, 500)
        correct_official_result(database, match["matchup_id"], *new_scores, reason="corrected Round-robin evidence")

    superseded, created = reconcile_wooden_spoon(
        database, season_id, actor=ACTOR, reason="re-recording after correction"
    )

    assert created is True
    assert superseded.award_id != original.award_id
    assert superseded.season_entry_id != original.season_entry_id
    repo = SeasonAwardRepository(database)
    assert repo.get_active(season_id, WOODEN_SPOON).award_id == superseded.award_id
    old = next(a for a in repo.history(season_id, WOODEN_SPOON) if a.award_id == original.award_id)
    assert old.status == "superseded"
    assert old.superseded_by_award_id == superseded.award_id


def test_reconcile_wooden_spoon_refuses_an_unresolved_ladder_tie():
    built = build_2026_replay_season(year=5130, score_fn=all_draws)
    database, season_id = built["database"], built["season"].season_id
    SeasonRepository(database).transition_lifecycle(season_id, "active", actor=ACTOR, reason="activate")

    try:
        reconcile_wooden_spoon(database, season_id, actor=ACTOR, reason="tie check")
        raise AssertionError("expected UnresolvedWoodenSpoonTieError")
    except UnresolvedWoodenSpoonTieError:
        pass

    assert SeasonAwardRepository(database).get_active(season_id, WOODEN_SPOON) is None


# -- Six-step atomic completion, and its fail-closed readiness gate ----------


def test_completion_exposes_a_stable_completed_season_version_and_completion_event():
    built = build_completable_season(year=5140)
    database, season_id = built["database"], built["season"].season_id
    before = SeasonRepository(database).get_season(season_id)

    result = complete_season(database, season_id, actor=ACTOR, reason="complete 2026 replay")

    assert result.season.lifecycle_state == "completed"
    assert result.completed_season_version == result.season.version
    assert result.completed_season_version > before.version
    assert AuditEventRepository(database).get_event(result.completion_event_id) is not None
    # Re-reading the season externally observes the identical stable version.
    assert SeasonRepository(database).get_season(season_id).version == result.completed_season_version


def test_completion_refuses_if_any_required_finals_week_is_not_final():
    built = build_completable_season(year=5150)
    database, season_id = built["database"], built["season"].season_id
    week3_round_id = (
        built["database"]
        .execute(
            "SELECT bbbffl_round_id FROM finals_bracket_week WHERE bracket_id=? AND week_number=3",
            (built["bracket"].bracket_id,),
        )
        .fetchone()["bbbffl_round_id"]
    )
    with database.engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(
            text("UPDATE bbbffl_round_lifecycle SET state='review' WHERE bbbffl_round_id=:rid"),
            {"rid": week3_round_id},
        )

    preview = preview_complete_season(database, season_id)
    assert preview["ready"] is False
    assert week3_round_id in preview["round_states"]
    assert preview["round_states"][week3_round_id] == "review"

    try:
        complete_season(database, season_id, actor=ACTOR, reason="attempt despite incomplete finals")
        raise AssertionError("expected SeasonNotReadyError")
    except SeasonNotReadyError:
        pass

    season = SeasonRepository(database).get_season(season_id)
    assert season.lifecycle_state == "active"
    assert SeasonAwardRepository(database).get_active(season_id, PREMIERSHIP) is None
    assert SeasonAwardRepository(database).get_active(season_id, WOODEN_SPOON) is None


def test_completion_refuses_if_any_of_ss1_ss4_is_missing_or_not_final():
    built = build_completable_season(year=5151)
    database, season_id = built["database"], built["season"].season_id
    ss2_round_id = built["superscore_rounds"][2]
    with database.engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(
            text("UPDATE bbbffl_round_lifecycle SET state='review' WHERE bbbffl_round_id=:rid"),
            {"rid": ss2_round_id},
        )

    try:
        complete_season(database, season_id, actor=ACTOR, reason="attempt despite incomplete SS2")
        raise AssertionError("expected SeasonNotReadyError")
    except SeasonNotReadyError:
        pass

    assert SeasonRepository(database).get_season(season_id).lifecycle_state == "active"

    # Checking only Grand Final + SS4 would wrongly call this ready; it must not.
    gf_state = database.execute(
        "SELECT state FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?",
        (
            database.execute(
                "SELECT bbbffl_round_id FROM finals_bracket_week WHERE bracket_id=? AND week_number=4",
                (built["bracket"].bracket_id,),
            ).fetchone()["bbbffl_round_id"],
        ),
    ).fetchone()["state"]
    ss4_state = database.execute(
        "SELECT state FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?", (built["superscore_rounds"][4],)
    ).fetchone()["state"]
    assert gf_state == "final"
    assert ss4_state == "final"


def test_completion_is_atomic_no_partial_writes_on_readiness_failure():
    """A readiness failure rolls back the entire transaction -- no award
    row, no completion audit event, no lifecycle transition."""
    built = build_completable_season(year=5152)
    database, season_id = built["database"], built["season"].season_id
    ss1_round_id = built["superscore_rounds"][1]
    with database.engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(
            text("UPDATE bbbffl_round_lifecycle SET state='live' WHERE bbbffl_round_id=:rid"),
            {"rid": ss1_round_id},
        )

    before_events = len(AuditEventRepository(database).list_events(entity_type="season", entity_id=season_id))
    try:
        complete_season(database, season_id, actor=ACTOR, reason="attempt despite incomplete SS1")
        raise AssertionError("expected SeasonNotReadyError")
    except SeasonNotReadyError:
        pass

    after_events = len(AuditEventRepository(database).list_events(entity_type="season", entity_id=season_id))
    assert after_events == before_events
    assert SeasonAwardRepository(database).get_active(season_id, PREMIERSHIP) is None
    assert SeasonAwardRepository(database).get_active(season_id, WOODEN_SPOON) is None
    assert SeasonRepository(database).get_season(season_id).lifecycle_state == "active"


def test_completing_an_already_completed_season_is_refused_not_a_silent_second_success():
    built = build_completable_season(year=5153)
    database, season_id = built["database"], built["season"].season_id
    complete_season(database, season_id, actor=ACTOR, reason="first completion")

    try:
        complete_season(database, season_id, actor=ACTOR, reason="second attempt")
        raise AssertionError("expected SeasonCompletedError")
    except SeasonCompletedError:
        pass


# -- Reads remain available, and no implicit "current season" -------------------


def test_reads_remain_available_against_a_completed_season():
    built = build_completable_season(year=5160)
    database, season_id = built["database"], built["season"].season_id
    complete_season(database, season_id, actor=ACTOR, reason="complete 2026 replay")

    season = SeasonRepository(database).get_season(season_id)
    assert season.lifecycle_state == "completed"
    bracket = FinalsBracketRepository(database).get_bracket_by_id(built["bracket"].bracket_id)
    assert bracket is not None
    assert FinalsBracketRepository(database).list_pairings(built["bracket"].bracket_id, week_number=4)
    assert SeasonAwardRepository(database).get_active(season_id, PREMIERSHIP) is not None
    assert AuditEventRepository(database).list_events(entity_type="season", entity_id=season_id)


def test_completed_2026_and_active_2027_coexist_without_implicit_current_season():
    completed_built = build_completable_season(year=5170)
    database = completed_built["database"]
    complete_season(database, completed_built["season"].season_id, actor=ACTOR, reason="complete 2026 replay")

    active_built = build_2026_replay_season(database=database, year=5171)
    SeasonRepository(database).transition_lifecycle(
        active_built["season"].season_id, "active", actor=ACTOR, reason="activate 2027-shaped season"
    )

    # The completed season's fence never leaks onto the unrelated, active one.
    correct_official_result(
        database,
        database.execute(
            "SELECT m.matchup_id FROM bbbffl_matchup m JOIN bbbffl_round_lifecycle l "
            "ON l.bbbffl_round_id=m.bbbffl_round_id WHERE l.competition_id=? ORDER BY m.matchup_id LIMIT 1",
            (active_built["competition"].competition_id,),
        ).fetchone()["matchup_id"],
        77,
        3,
        reason="ordinary correction against the still-active season",
    )

    completed = SeasonRepository(database).get_season(completed_built["season"].season_id)
    active = SeasonRepository(database).get_season(active_built["season"].season_id)
    assert completed.lifecycle_state == "completed"
    assert active.lifecycle_state == "active"

    # And the completed season's own write fence is unaffected by the newer
    # season's activity: it still refuses.
    try:
        reconcile_premiership(database, completed_built["season"].season_id, actor=ACTOR, reason="late attempt")
        raise AssertionError("expected SeasonCompletedError")
    except SeasonCompletedError:
        pass
