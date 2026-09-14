"""Issue #194: the read-only, post-hoc verification that gates step 7 of
"Checkpoint timing" (`docs/2026-finals-superscore-design.md`'s "Audit,
correction and recovery" section) -- creating the final database/checkpoint
archival evidence for a completed season.

Issue #195's `app.season_completion.complete_season` owns steps 1-6 of that
sequence (lock the season row; verify every required finals round and
SS1-SS4 are `final`; materialise/supersede the premiership and wooden-spoon
records; record the `season.completed` audit event; transition the season to
`completed`; commit) and, on success, returns a `CompletionResult` exposing
the resulting `completed_season_version`/`completion_event_id`. This module
is deliberately downstream of that transaction, never inside it: it takes no
lock, verifies nothing about finals/SuperScore readiness, and materialises no
award -- it only reads the already-committed outcome and asserts, before an
operator is allowed to treat the current database state as an archival
recovery point, that:

1. the season's lifecycle state actually is `completed` (not merely that a
   caller *believes* the completion transaction succeeded);
2. exactly one `season.completed` audit event exists for this season, so the
   archival step's provenance is bound to a real, append-only completion
   event -- never a lifecycle label alone;
3. if the caller already holds an expected completion-event identifier
   (e.g. printed by `complete_season` itself, or by a prior run of this
   verification), the currently observed event matches it exactly --
   catching the case where archival evidence would otherwise silently bind
   itself to a *different* completion than the one the operator reviewed.

This function takes no lock and starts no transaction: `bbbffl_season.
lifecycle_state = 'completed'` is a terminal state with no reopen pathway
(`app.season.SeasonRepository.guard_writable` refuses every further
result-changing write once observed, `complete_season` itself included), so
once this function observes `completed` there is no live writer left to race
-- the property this module needs to prove is "the completion transaction
has already committed", not "no one else can commit concurrently". A pre-
commit or concurrently-read version could only be observed by reading before
`complete_season`'s own transaction commits step 5/6, which is exactly what
requiring `lifecycle_state == 'completed'` (only ever set at that commit)
rules out.
"""

from dataclasses import dataclass

from app.audit import AuditEventRepository
from app.season import SeasonRepository, _now
from app.season_completion import ENTITY_TYPE_SEASON, SEASON_COMPLETED


class SeasonArchivalError(RuntimeError):
    """Base class for this module's domain errors."""


class UnknownSeasonError(SeasonArchivalError):
    """No season exists with the given `season_id`."""


class SeasonNotCompletedError(SeasonArchivalError):
    """The season's `lifecycle_state` is not `completed` yet -- the final
    archival checkpoint must not be taken. This is the fail-closed gate:
    #195's completion transaction (steps 1-6) has not been observed to have
    committed, so there is nothing yet for step 7 to bind evidence to."""


class MissingCompletionEventError(SeasonArchivalError):
    """The season reads `completed` but no `season.completed` audit event
    can be found for it. This should be unreachable through `complete_season`
    itself (which records the event and the transition in the same
    transaction), but the archival step must not trust the lifecycle label
    alone -- if this is ever raised, investigate before taking any archival
    evidence; do not treat the bare lifecycle state as sufficient proof."""


class AmbiguousCompletionEventError(SeasonArchivalError):
    """More than one `season.completed` audit event exists for this season.
    `complete_season` is not idempotently re-callable once a season is
    `completed` (it raises `SeasonCompletedError` instead), and no reopen
    pathway exists in this codebase, so this should be unreachable -- it is
    a fail-closed guard against ever silently picking "the latest one" if
    that invariant is ever violated."""


class CompletionEventMismatchError(SeasonArchivalError):
    """The currently observed `season.completed` audit event does not match
    the completion-event identifier the caller expected -- e.g. a different
    completion was recorded than the one an operator reviewed before running
    this verification. Never silently proceed against a different event than
    the one asserted; re-derive the expected identifier from the current
    state instead of forcing past this."""


@dataclass(frozen=True)
class ArchivalVerification:
    season_id: str
    completed_season_version: int
    completion_event_id: str
    completion_event_occurred_at: str
    verified_at: str


def verify_season_completed_for_archival(
    database,
    season_id: str,
    *,
    expected_completion_event_id: str | None = None,
) -> ArchivalVerification:
    """Read-only. Raises a typed `SeasonArchivalError` subclass and returns
    nothing if the season is not in a state the final archival checkpoint
    may legitimately be taken against. On success, returns the exact
    `completed_season_version`/`completion_event_id` the archival evidence
    (the paired `pg_dump` + checkpoint JSON + provenance-manifest record)
    must be labelled with -- never a version read before or concurrently
    with the completion transaction, and never the bare lifecycle label
    without its corresponding audit event."""
    season = SeasonRepository(database).get_season(season_id)
    if season is None:
        raise UnknownSeasonError(f"unknown season {season_id}")
    if season.lifecycle_state != "completed":
        raise SeasonNotCompletedError(
            f"season {season_id} is not completed yet (currently {season.lifecycle_state!r}); "
            "the final archival checkpoint must not be taken until #195's completion transaction has committed"
        )

    events = AuditEventRepository(database).list_events(
        entity_type=ENTITY_TYPE_SEASON, entity_id=season_id, action=SEASON_COMPLETED
    )
    if not events:
        raise MissingCompletionEventError(
            f"season {season_id} reads lifecycle_state='completed' but no {SEASON_COMPLETED!r} audit event was "
            "found for it; do not take archival evidence against a lifecycle label with no corresponding event"
        )
    if len(events) > 1:
        raise AmbiguousCompletionEventError(
            f"season {season_id} has {len(events)} {SEASON_COMPLETED!r} audit events; expected exactly one"
        )
    event = events[0]

    if expected_completion_event_id is not None and event.event_id != expected_completion_event_id:
        raise CompletionEventMismatchError(
            f"season {season_id}'s current completion event is {event.event_id}, not the expected "
            f"{expected_completion_event_id}; re-derive the expected identifier from the current state rather "
            "than proceeding against a different completion than the one reviewed"
        )

    return ArchivalVerification(
        season_id=season_id,
        completed_season_version=season.version,
        completion_event_id=event.event_id,
        completion_event_occurred_at=event.occurred_at,
        verified_at=_now(),
    )
