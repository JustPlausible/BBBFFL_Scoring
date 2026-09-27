"""Issue #239 acceptance coverage: the explicit `setup -> active` season-
activation gate's domain/service behaviour -- a read-only readiness preview
distinct from the atomic mutation, successful activation, missing/
contradictory prerequisites, repeated activation attempts, and the
completed-season write fence. HTTP/browser-flow and authorization coverage
live in `tests/test_season_activation_api.py`."""

from app.audit import AuditEventRepository
from app.db import transaction
from app.season import SeasonCompletedError, SeasonNotFoundError, SeasonRepository
from app.season_activation import (
    SeasonActivationError,
    SeasonActivationStateError,
    SeasonNotReadyToActivateError,
    activate_season,
    preview_activate_season,
)
from tests.season_activation_helpers import ACTOR, build_activation_ready_season

REASON = "setup complete; activating for live operation"


def _audit_event_count(database, season_id: str) -> int:
    return len(AuditEventRepository(database).list_events(entity_type="season", entity_id=season_id))


# -- Readiness preview is read-only and names specific blockers -------------


def test_preview_reports_ready_once_every_prerequisite_is_satisfied():
    built = build_activation_ready_season(year=6001)
    database, season_id = built["database"], built["season"].season_id

    readiness = preview_activate_season(database, season_id)

    assert readiness.ready is True
    assert readiness.lifecycle_state == "setup"
    assert readiness.diagnostic is None
    assert {check.key for check in readiness.checks} == {
        "entries",
        "player_pool",
        "competition_state",
        "fixture_state",
        "preseason_draft",
    }
    assert all(check.ready for check in readiness.checks)


def test_preview_names_missing_entries_as_a_specific_blocker():
    built = build_activation_ready_season(
        year=6002, create_entries=False, populate_pool=False, configure_squad=False, freeze_fixture=False
    )
    database, season_id = built["database"], built["season"].season_id

    readiness = preview_activate_season(database, season_id)

    assert readiness.ready is False
    entries_check = next(check for check in readiness.checks if check.key == "entries")
    assert entries_check.ready is False
    assert "currently 0" in entries_check.detail
    assert "entries" in readiness.diagnostic


def test_preview_names_incomplete_squads_as_a_specific_blocker_distinct_from_missing_pool():
    """Issue #239's "player pool / completed squads" prerequisite is refused
    for either half independently: an empty pool, or a populated pool whose
    squads are not yet complete (draft in progress)."""
    built = build_activation_ready_season(year=6003, complete_picks=False, finalize_draft=False, freeze_fixture=False)
    database, season_id = built["database"], built["season"].season_id

    readiness = preview_activate_season(database, season_id)

    assert readiness.ready is False
    pool_check = next(check for check in readiness.checks if check.key == "player_pool")
    assert pool_check.ready is False
    assert "0 of 10" in pool_check.detail
    draft_check = next(check for check in readiness.checks if check.key == "preseason_draft")
    assert draft_check.ready is False
    assert "0 of 20" in draft_check.detail


def test_preview_names_unfinalized_draft_once_every_pick_is_complete():
    built = build_activation_ready_season(year=6004, finalize_draft=False, freeze_fixture=False)
    database, season_id = built["database"], built["season"].season_id

    readiness = preview_activate_season(database, season_id)

    assert readiness.ready is False
    draft_check = next(check for check in readiness.checks if check.key == "preseason_draft")
    assert draft_check.ready is False
    assert "finalized" in draft_check.detail
    # Every pick is complete, so the squad-completion check independently
    # already reports ready -- only finalisation itself is outstanding.
    pool_check = next(check for check in readiness.checks if check.key == "player_pool")
    assert pool_check.ready is True


def test_preview_names_unfrozen_fixture_draw_as_a_specific_blocker():
    built = build_activation_ready_season(year=6005, freeze_fixture=False)
    database, season_id = built["database"], built["season"].season_id

    readiness = preview_activate_season(database, season_id)

    assert readiness.ready is False
    fixture_check = next(check for check in readiness.checks if check.key == "fixture_state")
    assert fixture_check.ready is False
    assert "has not been created" in fixture_check.detail


def test_preview_names_uninitialized_competition_as_a_specific_blocker():
    built = build_activation_ready_season(year=6006, initialize_competition=False, freeze_fixture=False)
    database, season_id = built["database"], built["season"].season_id

    readiness = preview_activate_season(database, season_id)

    assert readiness.ready is False
    competition_check = next(check for check in readiness.checks if check.key == "competition_state")
    assert competition_check.ready is False
    assert "not been initialized" in competition_check.detail


def test_preview_reports_contradictory_ordinary_competition_state():
    """More than one `ordinary`-typed competition stream is a contradiction,
    not merely an incomplete step -- `preview_activate_season` must name it
    rather than reporting a generic false."""
    built = build_activation_ready_season(year=6007, freeze_fixture=False)
    database, season_id = built["database"], built["season"].season_id
    ordinary = next(c for c in built["seasons"].list_competitions(season_id) if c.stream_type == "ordinary")
    rules = built["seasons"].list_rules_versions(season_id)[0]
    built["seasons"].create_competition(season_id, rules.rules_version_id, "ordinary-2", "Extra Ordinary", "ordinary")

    readiness = preview_activate_season(database, season_id)

    assert readiness.ready is False
    competition_check = next(check for check in readiness.checks if check.key == "competition_state")
    assert competition_check.ready is False
    assert "2 ordinary competition streams" in competition_check.detail
    assert ordinary is not None  # sanity: the original stream still exists


def test_preview_raises_for_an_unknown_season():
    database = build_activation_ready_season(year=6008)["database"]
    try:
        preview_activate_season(database, "not-a-real-season-id")
        raise AssertionError("expected SeasonNotFoundError")
    except SeasonNotFoundError:
        pass


def test_preview_never_writes_anything():
    built = build_activation_ready_season(year=6009)
    database, season_id = built["database"], built["season"].season_id
    before_state = SeasonRepository(database).get_season(season_id)
    before_events = _audit_event_count(database, season_id)

    preview_activate_season(database, season_id)

    after_state = SeasonRepository(database).get_season(season_id)
    assert after_state == before_state
    assert _audit_event_count(database, season_id) == before_events


# -- Successful activation ---------------------------------------------------


def test_activate_season_transitions_setup_to_active_with_actor_reason_and_audit_event():
    built = build_activation_ready_season(year=6010)
    database, season_id = built["database"], built["season"].season_id
    before_events = _audit_event_count(database, season_id)

    result = activate_season(database, season_id, actor=ACTOR, reason=REASON)

    assert result.season.lifecycle_state == "active"
    assert result.previous_lifecycle_state == "setup"
    stored = SeasonRepository(database).get_season(season_id)
    assert stored.lifecycle_state == "active"

    events = AuditEventRepository(database).list_events(entity_type="season", entity_id=season_id)
    assert len(events) == before_events + 1
    event = events[-1]
    assert event.action == "season.lifecycle.changed"
    assert event.reason == REASON
    assert event.actor_id == ACTOR.actor_id
    assert event.actor_role == ACTOR.actor_role
    assert event.before_state == {"lifecycle_state": "setup"}
    assert event.after_state == {"lifecycle_state": "active"}
    assert event.occurred_at is not None


def test_activate_season_requires_an_explicit_reason():
    built = build_activation_ready_season(year=6011)
    database, season_id = built["database"], built["season"].season_id
    before_events = _audit_event_count(database, season_id)

    for blank in (None, "", "   "):
        try:
            activate_season(database, season_id, actor=ACTOR, reason=blank)
            raise AssertionError("expected SeasonActivationError")
        except SeasonActivationError:
            pass

    assert SeasonRepository(database).get_season(season_id).lifecycle_state == "setup"
    assert _audit_event_count(database, season_id) == before_events


# -- Missing prerequisites: refused, unchanged, no mutation audit event -----


def test_activate_season_refuses_when_entries_are_missing_and_writes_nothing():
    built = build_activation_ready_season(
        year=6012, create_entries=False, populate_pool=False, configure_squad=False, freeze_fixture=False
    )
    database, season_id = built["database"], built["season"].season_id
    before_events = _audit_event_count(database, season_id)

    try:
        activate_season(database, season_id, actor=ACTOR, reason=REASON)
        raise AssertionError("expected SeasonNotReadyToActivateError")
    except SeasonNotReadyToActivateError as exc:
        assert "10 season entries are required" in str(exc)

    assert SeasonRepository(database).get_season(season_id).lifecycle_state == "setup"
    assert _audit_event_count(database, season_id) == before_events


def test_activate_season_refuses_when_draft_is_not_finalized_and_writes_nothing():
    built = build_activation_ready_season(year=6013, finalize_draft=False, freeze_fixture=False)
    database, season_id = built["database"], built["season"].season_id
    before_events = _audit_event_count(database, season_id)

    try:
        activate_season(database, season_id, actor=ACTOR, reason=REASON)
        raise AssertionError("expected SeasonNotReadyToActivateError")
    except SeasonNotReadyToActivateError as exc:
        assert "finalized" in str(exc)

    assert SeasonRepository(database).get_season(season_id).lifecycle_state == "setup"
    assert _audit_event_count(database, season_id) == before_events


def test_activate_season_names_every_blocker_when_several_prerequisites_are_missing():
    built = build_activation_ready_season(
        year=6014,
        create_entries=False,
        populate_pool=False,
        configure_squad=False,
        initialize_competition=False,
        freeze_fixture=False,
    )
    database, season_id = built["database"], built["season"].season_id

    try:
        activate_season(database, season_id, actor=ACTOR, reason=REASON)
        raise AssertionError("expected SeasonNotReadyToActivateError")
    except SeasonNotReadyToActivateError as exc:
        message = str(exc)
        assert "season entries" in message
        assert "player pool" in message.lower() or "pool has not been populated" in message
        assert "not been initialized" in message
        assert "fixture-number draw" in message


# -- Repeated activation: refused with a clear current-state message --------


def test_repeated_activation_is_refused_with_a_clear_already_active_message_and_writes_nothing():
    built = build_activation_ready_season(year=6015)
    database, season_id = built["database"], built["season"].season_id
    activate_season(database, season_id, actor=ACTOR, reason=REASON)
    events_after_first = _audit_event_count(database, season_id)

    try:
        activate_season(database, season_id, actor=ACTOR, reason="try again")
        raise AssertionError("expected SeasonActivationStateError")
    except SeasonActivationStateError as exc:
        assert "already active" in str(exc)

    assert SeasonRepository(database).get_season(season_id).lifecycle_state == "active"
    assert _audit_event_count(database, season_id) == events_after_first


def test_preview_after_activation_reports_already_active_diagnostic():
    built = build_activation_ready_season(year=6016)
    database, season_id = built["database"], built["season"].season_id
    activate_season(database, season_id, actor=ACTOR, reason=REASON)

    readiness = preview_activate_season(database, season_id)

    assert readiness.ready is False
    assert readiness.lifecycle_state == "active"
    assert readiness.checks == []
    assert "already active" in readiness.diagnostic


# -- Invalid lifecycle state: completed is refused via the existing fence ---


def test_activation_of_a_completed_season_is_refused_via_the_write_fence_and_writes_nothing():
    built = build_activation_ready_season(year=6017)
    database, season_id = built["database"], built["season"].season_id
    # Directly mark the season completed for this boundary test, the same
    # technique `tests/test_season_setup.py` already uses to exercise the
    # completed-season fence without running the full completion ceremony.
    with transaction(database) as conn:
        conn.execute("UPDATE bbbffl_season SET lifecycle_state='completed' WHERE season_id=?", (season_id,))
    before_events = _audit_event_count(database, season_id)

    try:
        activate_season(database, season_id, actor=ACTOR, reason=REASON)
        raise AssertionError("expected SeasonActivationStateError")
    except SeasonActivationStateError as exc:
        assert "completed" in str(exc)

    assert SeasonRepository(database).get_season(season_id).lifecycle_state == "completed"
    assert _audit_event_count(database, season_id) == before_events

    # `guard_writable`'s own exception must never leak past this module's
    # translated one.
    readiness = preview_activate_season(database, season_id)
    assert readiness.ready is False
    assert readiness.lifecycle_state == "completed"


def test_transition_lifecycle_still_reaches_active_directly_for_the_lower_level_capability():
    """Issue #239 explicitly preserves the existing lower-level lifecycle
    transition capability (`SeasonRepository.transition_lifecycle`) -- this
    module adds the browser gate and readiness rules on top of it, it does
    not remove or restrict that domain primitive."""
    built = build_activation_ready_season(year=6018, create_entries=False, freeze_fixture=False)
    database, season_id = built["database"], built["season"].season_id

    season = SeasonRepository(database).transition_lifecycle(season_id, "active", actor=ACTOR, reason=REASON)

    assert season.lifecycle_state == "active"


def test_guard_writable_completed_error_is_never_raised_directly_by_activate_season():
    """`activate_season` must translate `SeasonCompletedError` into its own
    `SeasonActivationStateError` rather than letting it escape unmapped
    (there is no `app.main` handler for the bare domain exception)."""
    built = build_activation_ready_season(year=6019)
    database, season_id = built["database"], built["season"].season_id
    with transaction(database) as conn:
        conn.execute("UPDATE bbbffl_season SET lifecycle_state='completed' WHERE season_id=?", (season_id,))

    try:
        activate_season(database, season_id, actor=ACTOR, reason=REASON)
        raise AssertionError("expected SeasonActivationStateError")
    except SeasonCompletedError:
        raise AssertionError("SeasonCompletedError must be translated, not raised directly")
    except SeasonActivationStateError:
        pass
