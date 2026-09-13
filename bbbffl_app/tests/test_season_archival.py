"""app/season_archival.py (issue #194): the read-only guard proving the
final archival checkpoint (step 7 of "Checkpoint timing",
`docs/2026-finals-superscore-design.md`) can only be taken after issue
#195's completion transaction (steps 1-6) has actually committed, and that
it binds to the exact completed-season version/completion-event identifier
that transaction established -- never a pre-commit or concurrently-read
version, and never a bare lifecycle-state label alone."""

import pytest
from sqlalchemy import text

from app.audit import ActorContext
from app.season_archival import (
    CompletionEventMismatchError,
    MissingCompletionEventError,
    SeasonNotCompletedError,
    UnknownSeasonError,
    verify_season_completed_for_archival,
)
from app.season_completion import complete_season
from tests.season_completion_helpers import build_completable_season

ACTOR = ActorContext.anonymous_operator("test")


def test_unknown_season_is_rejected():
    built = build_completable_season(year=6301)
    with pytest.raises(UnknownSeasonError):
        verify_season_completed_for_archival(built["database"], "no-such-season")


def test_active_season_is_rejected_before_completion():
    """The core safety property: a season that has not yet observed
    `completed` must never be treated as archival-ready, even though every
    finals/SuperScore round is already `final` at this point in the fixture
    -- exactly the state a caller racing #195's own transaction would
    observe if it read before commit."""
    built = build_completable_season(year=6302)
    with pytest.raises(SeasonNotCompletedError):
        verify_season_completed_for_archival(built["database"], built["season"].season_id)


def test_completed_season_reports_the_exact_completion_identifiers():
    built = build_completable_season(year=6303)
    database, season_id = built["database"], built["season"].season_id
    result = complete_season(database, season_id, actor=ACTOR, reason="issue #194 archival guard test")

    verification = verify_season_completed_for_archival(database, season_id)
    assert verification.season_id == season_id
    assert verification.completed_season_version == result.completed_season_version
    assert verification.completion_event_id == result.completion_event_id


def test_expected_completion_event_id_must_match():
    built = build_completable_season(year=6304)
    database, season_id = built["database"], built["season"].season_id
    result = complete_season(database, season_id, actor=ACTOR, reason="issue #194 archival guard test")

    # Matches -> succeeds.
    verify_season_completed_for_archival(database, season_id, expected_completion_event_id=result.completion_event_id)

    # Does not match -> fails closed rather than silently proceeding
    # against a different completion than the one the operator reviewed.
    with pytest.raises(CompletionEventMismatchError):
        verify_season_completed_for_archival(database, season_id, expected_completion_event_id="not-the-real-event-id")


def test_lifecycle_state_alone_is_not_trusted_without_its_audit_event():
    """Defence in depth: `app.season.SeasonRepository.transition_lifecycle`
    already refuses to reach `completed` outside `complete_season`
    (Codex review, issue #195) -- proving this guard does not merely rely
    on that refusal requires going around the application layer entirely
    (a raw SQL row edit, standing in for e.g. errant manual DB surgery) to
    reach `lifecycle_state='completed'` with no corresponding
    `season.completed` audit event. Even then, this guard must refuse
    rather than treat the bare label as sufficient archival provenance."""
    built = build_completable_season(year=6305)
    database, season_id = built["database"], built["season"].season_id
    with database.engine.begin() as conn:
        conn.execute(
            text("UPDATE bbbffl_season SET lifecycle_state='completed' WHERE season_id=:sid"), {"sid": season_id}
        )

    with pytest.raises(MissingCompletionEventError):
        verify_season_completed_for_archival(database, season_id)
