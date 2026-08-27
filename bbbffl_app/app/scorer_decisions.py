"""Application service: scorer-decision orchestration for one competition
instance (Grand Final or a SuperScore round).

This is the "routes orchestrate services, services own business rules,
repositories own persistence mechanics" boundary described in
docs/architecture.md for the scoring domain. `app/routes/admin.py` and
`app/routes/superscore.py` both call these functions instead of duplicating
validation/orchestration against `app.db.DecisionsRepository` themselves --
before this module existed, both route files independently re-implemented
"is this team_key known", "is this slot/position valid", "is the competition
instance already locked" and the finalize-or-409 orchestration. Consolidating
that here means a future caller (an admin script, a replay harness, a test)
gets the same rules as the HTTP surface for free, and the two route files no
longer drift from each other.

Every function here is a thin, side-effect-scoped wrapper: it validates
inputs against competition-level facts the caller already has (the set of
known team_keys) and against the roster/position vocabulary in
`app.scoring`, then delegates the actual write to the `DecisionsRepository`
instance the caller passes in. The repository remains the sole owner of the
transaction and of translating a write into a persisted row plus its audit
event (see `app/db.py`); this module never opens a transaction or touches
SQL directly, and never decides *how* a decision is stored -- only *whether*
it is a legal one to make right now.

Errors are raised as plain domain exceptions (never `fastapi.HTTPException`)
so this module has no HTTP dependency and stays usable outside a request
context. `app/main.py` registers exception handlers that translate each one
to the same HTTP status the routes returned before this module existed.
"""

import dataclasses

from app.audit import ActorContext
from app.scoring import ROSTER_SLOTS, SCORABLE_POSITIONS

# The admin surface today is one shared token, not a per-person login (see
# app/routes/admin.py's require_admin and roadmap package 19/20). Every
# mutation is attributed to this well-defined, non-impersonating actor -- see
# app/audit.py's module docstring for why "anonymous_operator" rather than
# inventing a fake authenticated identity. actor_role still distinguishes
# ordinary scorer duties from the privileged finalisation action.
SCORER_ACTOR = ActorContext.anonymous_operator(role="scorer")
ADMIN_ACTOR = ActorContext.anonymous_operator(role="admin")


class ScorerDecisionError(Exception):
    """Base class for the domain errors this module raises."""


class UnknownTeamError(ScorerDecisionError):
    """`team_key` is not part of the competition instance being scored."""


class InvalidSlotError(ScorerDecisionError):
    """A DNP request named a slot outside `app.scoring.ROSTER_SLOTS`."""


class InvalidPositionError(ScorerDecisionError):
    """An interchange/override request named a position outside
    `app.scoring.SCORABLE_POSITIONS`."""


class CompetitionFinalizedError(ScorerDecisionError):
    """The competition instance is already finalised; decisions are locked."""


class ResultNotReadyError(ScorerDecisionError):
    """Finalisation was attempted before every relevant AFL match completed."""


def _ensure_known_team(team_keys, team_key: str) -> None:
    if team_key not in team_keys:
        raise UnknownTeamError(f"Unknown team_key: {team_key}")


def _ensure_editable(decisions) -> None:
    if decisions.get_matchup_state().finalized:
        raise CompetitionFinalizedError(
            "competition instance already finalised; decisions are locked"
        )


def set_dnp(decisions, team_keys, team_key: str, slot: str, dnp: bool, *, actor: ActorContext = SCORER_ACTOR) -> None:
    """Record (or clear) a scorer DNP decision for one roster slot."""
    _ensure_editable(decisions)
    _ensure_known_team(team_keys, team_key)
    if slot not in ROSTER_SLOTS:
        raise InvalidSlotError(f"Unknown slot: {slot}")
    decisions.set_dnp(team_key, slot, dnp, actor=actor)


def set_interchange(
    decisions, team_keys, team_key: str, target_position: str | None, *, actor: ActorContext = SCORER_ACTOR
) -> None:
    """Assign (or clear, via `target_position=None`) the Interchange's
    covering position for one team."""
    _ensure_editable(decisions)
    _ensure_known_team(team_keys, team_key)
    if target_position is not None and target_position not in SCORABLE_POSITIONS:
        raise InvalidPositionError(f"Invalid target_position: {target_position}")
    decisions.set_interchange_assignment(team_key, target_position, actor=actor)


def set_override(
    decisions,
    team_keys,
    team_key: str,
    position: str,
    override_score: float | None,
    reason: str | None,
    *,
    actor: ActorContext = SCORER_ACTOR,
) -> None:
    """Set (or clear, via `override_score=None`) a direct score override."""
    _ensure_editable(decisions)
    _ensure_known_team(team_keys, team_key)
    if position not in SCORABLE_POSITIONS:
        raise InvalidPositionError(f"Invalid position: {position}")
    decisions.set_override(team_key, position, override_score, reason, actor=actor)


def finalize(result, decisions, note: str | None, *, actor: ActorContext = ADMIN_ACTOR) -> None:
    """Freeze an already-computed `result` (a `service.MatchupResult` or
    `service.SuperScoreResult`) as the official outcome.

    `result` is computed exactly once by the caller (via
    `service.build_matchup_state`/`build_superscore_state`) and passed in
    rather than recomputed here -- a second afl-api round trip after
    `decisions.finalize()` commits would mean a transient afl-api failure
    could make the caller report failure for an already-irreversible
    finalisation. `decisions.finalize()` remains the sole owner of the
    write transaction (domain row + audit event, see `app/db.py`); this
    function only decides whether finalising is currently legal.
    """
    if result.status != "AWAITING_SCORER_SIGNOFF":
        raise ResultNotReadyError(
            "Cannot finalise until all relevant AFL matches are complete "
            "(status must be AWAITING_SCORER_SIGNOFF)."
        )
    snapshot = dataclasses.asdict(result)
    decisions.finalize(note, snapshot, actor=actor)
