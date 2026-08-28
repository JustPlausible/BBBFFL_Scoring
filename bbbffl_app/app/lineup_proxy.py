"""Explicit scorer/admin proxy operations over the same weekly-lineup
aggregate an ordinary coach submission uses (roadmap package 22, issue
#55).

Builds on the shared package-24 submission validator and
`app.lineups.WeeklyLineupRepository` -- the same private
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
piece of *submission* provenance issue #55 asks for -- `actor_type`/
`actor_id`/`actor_role` (operator identity/role), `reason` (the required
note), `source_type="scorer_proxy"` (never `"coach"`), and the lineup's
own `season_entry_id` (the affected entry, via the `weekly_lineup`
header). The one addition this module needs is `weekly_lineup.
draft_source` (migrations/versions/0018_proxy_draft_source.py) -- a
single, whole-draft, non-history column, not a second audit log or a
per-position/per-edit trail -- so that a proxy intervention in a *draft*
can never silently disappear once submitted; see "Draft-handoff
provenance" below.

## What is (and is not) separately audited

Only the *submission* is a material, authoritative-state change -- exactly
as for a coach's own lineup, where `save_draft` (private, working state)
is never audited and `submit` (the immutable, official transition) always
is (see app/audit.py's module docstring: "audit events explain the
sequence of changes that produced current state"). `create_or_amend` here
is the same `save_draft` a coach's own editing uses, so proxy draft edits
carry the same (lack of) *audit-event* trail a coach's own draft edits do
today -- a deliberate scope-consistent choice, not an oversight. `submit`
is the material, attributable, transactionally-audited action.

## Draft-handoff provenance

`weekly_lineup_draft_slot` has no per-position or per-edit authorship of
its own (never has, even for a coach's own multiple edits across
sessions) -- `create_or_amend` does not change that, and this module adds
no general per-edit draft history. What it does add is coarser but
sufficient: `create_or_amend` marks the resulting draft revision
`draft_source="scorer_proxy"`. `WeeklyLineupRepository.submit` (the
ordinary coach path) then refuses to submit a draft whose current
`draft_source` is `"scorer_proxy"` unless `source_type="scorer_proxy"`
too -- see `submit`'s docstring. A proxy-authored draft can therefore
never quietly become a `source_type="coach"` submission with no trace of
the intervention: either the operator submits it themselves (correctly
attributed), or the coach must first save their own draft edit (which
resets `draft_source` back to `"coach"`, since the coach has then
reviewed/rewritten it). This is action-scoped, not content-scoped: it
tracks who most recently wrote the *whole* draft, not which positions
came from whom -- deliberately the narrowest fix for the reported
misattribution risk, not a parallel lineup model or a second audit log.
"""

from app.audit import ActorContext
from app.lineup_validation import ValidatedLineupSubmissionService
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

    def __init__(self, database, afl_client=None):
        self.database = database
        self._lineups = WeeklyLineupRepository(database)
        self._submissions = ValidatedLineupSubmissionService(database, afl_client)

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
        edit fails safely rather than silently clobbering it.

        Marks the resulting draft revision `draft_source="scorer_proxy"`
        (see migrations/versions/0018_proxy_draft_source.py), so
        `WeeklyLineupRepository.submit`'s ordinary coach path refuses to
        submit it until either this module's own `submit` does (correctly
        attributed `source_type="scorer_proxy"`) or the coach saves their
        own draft edit on top of it -- the proxy intervention can no
        longer silently disappear into a `source_type="coach"` submission.
        """
        _ensure_operator(actor)
        return self._lineups.save_draft(
            season_id,
            competition_id,
            bbbffl_round_id,
            season_entry_id,
            positions,
            expected_revision=expected_revision,
            draft_source="scorer_proxy",
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
        return self._submissions.submit(
            lineup_id,
            expected_draft_revision=expected_draft_revision,
            expected_submission_version=expected_submission_version,
            actor=actor,
            source_type=SCORER_PROXY_SOURCE_TYPE,
            reason=reason,
            lock_guard=lock_guard,
        ).submission
