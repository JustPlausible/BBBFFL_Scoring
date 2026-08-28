"""Explicit scorer/admin proxy operations over the same weekly-lineup
aggregate an ordinary coach submission uses (roadmap package 22, issue
#55).

Builds directly on `app.lineups.WeeklyLineupRepository` -- the same private
draft / immutable submission boundary from #33, the same `lock_guard`
integration with app/lockouts.py, and the same append-only audit event
from #17 that `submit()` already writes on every submission (`app.audit`,
action `LINEUP_SUBMITTED`) -- rather than a parallel proxy-only selection
store or a second proxy audit log.

## Actor, never the coach

The receiving coach/season entry is never the actor. Every write here is
attributed to the operator: an `ActorContext` with `actor_type=
"anonymous_operator"` and `actor_role` one of `PROXY_ACTOR_ROLES`
("scorer"/"admin") -- see app/audit.py's module docstring for why an
anonymous-but-non-impersonating operator identity, rather than an
authenticated coach identity, is the correct pre-authentication actor
(roadmap package 19/20). `_ensure_operator` rejects any other actor before
any write is attempted.

## Provenance

`WeeklyLineupRepository.submit`'s existing columns already carry every
piece of provenance issue #55 asks for -- `actor_type`/`actor_id`/
`actor_role` (operator identity/role), `reason` (the required note),
`source_type="scorer_proxy"` (never `"coach"`), and the lineup's own
`season_entry_id` (the affected entry, via the `weekly_lineup` header) --
so this module adds no new columns or tables; it only enforces that a
proxy submission is always attributed and always tagged as proxy state,
never miscategorised as `"coach"`.

## What is (and is not) separately audited

Only the *submission* is a material, authoritative-state change -- exactly
as for a coach's own lineup, where `save_draft` (private, working state)
is never audited and `submit` (the immutable, official transition) always
is (see app/audit.py's module docstring: "audit events explain the
sequence of changes that produced current state"). `create_or_amend` here
is the same `save_draft` a coach's own editing uses, so proxy draft edits
carry the same (lack of) audit trail a coach's own draft edits do today --
a deliberate scope-consistent choice, not an oversight; see this package's
PR description for the follow-up this leaves open. `submit` is the
material, attributable, transactionally-audited action.

A consequence, accepted rather than overlooked: `weekly_lineup_draft_slot`
has no per-position or per-edit authorship of its own (never has, even for
a coach's own multiple edits across sessions), so if an operator edits a
draft via `create_or_amend` and a *different* actor -- the coach's own
ordinary `submit()`, never this module -- later submits that same draft
as-is, the resulting submission is correctly attributed to whoever
performed *that* submit action (`source_type="coach"`), not to the
operator who last touched the draft's content. This module's provenance
guarantee is action-scoped ("who submitted"), matching issue #55's actual
requirement ("Proxy actions must capture... actual operator actor
identity"), not content-scoped ("who typed this position"); the latter
would need new persisted draft-level authorship state (a schema change)
and is an explicit candidate follow-up, not something to bolt on here.
"""

from app.audit import ActorContext
from app.lineups import LineupIntegrityError, WeeklyLineupRepository

SCORER_PROXY_SOURCE_TYPE = "scorer_proxy"
PROXY_ACTOR_ROLES = frozenset({"scorer", "admin"})


class LineupProxyError(LineupIntegrityError):
    """Base class for this module's domain errors."""


class UnauthorizedProxyActorError(LineupProxyError):
    """The supplied actor is not a recognised scorer/admin operator
    context -- see this module's docstring, "Actor, never the coach"."""


def _ensure_operator(actor: ActorContext) -> None:
    if actor.actor_type != "anonymous_operator" or actor.actor_role not in PROXY_ACTOR_ROLES:
        raise UnauthorizedProxyActorError(
            "proxy lineup actions require an anonymous_operator actor with actor_role scorer or admin, "
            f"got actor_type={actor.actor_type!r} actor_role={actor.actor_role!r}"
        )


class LineupProxyService:
    """Scorer/admin proxy entry point over `WeeklyLineupRepository`, for an
    authorised operator acting on behalf of a season entry."""

    def __init__(self, database):
        self.database = database
        self._lineups = WeeklyLineupRepository(database)

    def create_or_amend(
        self,
        season_id: str,
        competition_id: str,
        bbbffl_round_id: str,
        season_entry_id: str,
        positions: dict,
        *,
        expected_revision: int,
        actor: ActorContext,
    ):
        """Create (if `expected_revision=0` and no draft yet exists) or
        amend the entry's private draft for this round, on the operator's
        behalf. Shares `save_draft`'s ordinary optimistic-concurrency
        contract, so a proxy edit racing the coach's own concurrent draft
        edit fails safely rather than silently clobbering it."""
        _ensure_operator(actor)
        return self._lineups.save_draft(
            season_id, competition_id, bbbffl_round_id, season_entry_id, positions, expected_revision=expected_revision
        )

    def submit(
        self,
        lineup_id: str,
        *,
        expected_draft_revision: int,
        expected_submission_version: int,
        actor: ActorContext,
        reason: str,
        lock_guard=None,
    ):
        """Submit (or resubmit) the lineup's current draft content as a new
        immutable version, attributed to the operator with
        `source_type="scorer_proxy"`.

        Goes through `WeeklyLineupRepository.submit` -- the exact same
        `lock_guard`/lockout enforcement, `expected_submission_version`
        compare-and-swap, and immutable-version history a coach's own
        submission uses (see app/lineups.py, app/lockouts.py). A scorer
        cannot use this to bypass an already-locked position: `lock_guard`
        rejects the attempt identically, whichever `source_type` requested
        it. Resubmission preserves every prior immutable submitted version
        exactly as an ordinary resubmission does.
        """
        _ensure_operator(actor)
        if not reason:
            raise LineupProxyError("a scorer/admin proxy submission requires a reason")
        return self._lineups.submit(
            lineup_id,
            expected_draft_revision=expected_draft_revision,
            expected_submission_version=expected_submission_version,
            actor=actor,
            source_type=SCORER_PROXY_SOURCE_TYPE,
            reason=reason,
            lock_guard=lock_guard,
        )
