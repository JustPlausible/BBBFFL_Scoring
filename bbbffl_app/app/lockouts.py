"""BBBFFL-owned player-level AFL-match lockout decisions.

`afl-api` remains authoritative for AFL match identity, scheduled start and
status (see app/afl_client.py) -- nothing more. Which AFL matches actually
cause a BBBFFL lockout is a BBBFFL competition decision: this module owns a
persisted **round lockout plan** (`LockoutTriggerRepository`) of explicit,
commissioner/scorer-configured triggers, and the **lockout decision** that
resolves each selected season player to their accepted-round AFL match
(app/round_mapping.py's accepted mapping -- never a provider "current round"
or other global state) and asks whether a configured trigger covering that
match has activated.

Nothing here calls `datetime.now()` on its own initiative: every entry point
uses an explicit `evaluation_at` first, then an available replay match-facts
clock, and only otherwise defaults once at the outer edge to
`datetime.now(timezone.utc)` -- so tests and replay can always supply a fixed
instant and get a deterministic answer.

## The round lockout plan

A BBBFFL round's lockout plan is an ordered set of **triggers**
(`LockoutTrigger`, persisted by `LockoutTriggerRepository`), each one of:

- **selective** (an "early lockout" stage): associated with one or more
  specific AFL match IDs. The trigger activates the instant *any* of its
  associated matches reaches its own lock boundary (`evaluate_match_lock`
  below); once activated, every selected player whose own resolved match is
  in that trigger's configured match set is locked -- regardless of whether
  their own particular match, if different from the one that fired the
  trigger, has itself started. This generalises the season model's "once an
  early AFL match starts, any selected player from either participating AFL
  club is locked" to a configured *group* of matches sharing one lockout
  moment (e.g. a Thursday double-header).
- **main**: the round's final trigger. Once activated (by any of its own
  associated matches reaching lock boundary), *every* remaining editable
  position in *every* lineup for the round locks at once, regardless of
  whether that player's own AFL match has started. A round normally has at
  most one current main trigger (`LockoutTriggerRepository` enforces this).

A round may have zero or more selective triggers plus one main trigger --
the common case is "optional early stage(s), then main" -- but nothing here
hard-codes exactly two stages: an unusual round (e.g. an extra Tuesday
fixture) can configure additional selective triggers with no code change,
only additional `LockoutTriggerRepository.create` calls. **A player whose
AFL match is not covered by any activated trigger, selective or main,
remains editable even if that match has itself already started** -- an
uncommitted BBBFFL match commencing does not, by itself, freeze anything;
only a configured trigger does. This is the deliberate correction of an
earlier version of this module, which treated every AFL match as its own
independent trigger; see docs/lockouts.md.

Trigger configuration is never inferred from AFL scheduling -- not day of
week, not chronological match order, not "first match of the round", not
the AFL round number, not browser/UI state. It is always an explicit
commissioner/scorer decision, recorded with actor/reason like every other
privileged BBBFFL mutation (`app.audit`).

## Lock boundary (per AFL match, used to decide when a *trigger* fires)

`evaluate_match_lock(match, evaluation_at)` -- a pure function with no
persisted-evidence awareness -- decides whether one AFL match has, by
itself, reached its own boundary:

- **editable** while its authoritative status is UPCOMING (or a tolerated
  legacy alias) and `evaluation_at` is strictly before `start_time_utc`;
- **locked** the instant `evaluation_at` reaches or passes that scheduled
  start (`>=`), or once LIVE, POSTGAME or CONCLUDED is observed (status is
  authoritative over wall-clock time);
- **indeterminate** when the status is not one of afl-api's documented v1
  lifecycle values/aliases (`app.afl_client.is_recognized_match_status`), or
  it is UPCOMING with no scheduled start time. A trigger whose only
  associated match is indeterminate simply does not activate yet -- fail
  safe, never guessed.

This is now used only to decide **trigger activation** (see
`LockoutRepository._materialize_round_triggers`), not directly as a
per-player decision -- see "The round lockout plan" above.

## Historical irreversibility

Two independent, cooperating layers make "locked, permanently" actually
true rather than aspirational:

1. **Trigger-level.** Once a trigger has activated, that fact is durably
   recorded in `bbbffl_round_lockout_trigger_activation` (immutable, same
   trigger-based enforcement as `weekly_lineup_submission`/
   `weekly_lineup_lock`), and `LockoutTriggerRepository.replace` then
   permanently refuses to change that trigger's configuration
   (`TriggerAlreadyActivatedError`) -- so the set of AFL matches an
   activated trigger covers can never change afterwards. A later upstream
   schedule/status correction to one of those matches cannot un-fire the
   trigger either: activation evidence, once written, is never
   recomputed against fresh `matches` data.
2. **Player-level.** Once a *specific selected player's* position has been
   observed as locked (because it resolves to a match covered by an
   activated trigger, or because main has activated), that observation is
   durably recorded in `weekly_lineup_lock` (PK `(lineup_id, position)`),
   exactly as before this round-plan model was introduced.
   `_evaluate_position` always prefers a matching persisted row over a
   fresh recomputation. Two invariants make this durable rather than
   aspirational:

   - **Only the effective submission can be materialized.**
     `_evaluate_position` only *writes* a row when its caller passes
     `materializable=True`, and the only caller that does is
     `_materialize_lineup`, which always derives the positions it evaluates
     from `weekly_lineup`'s own `effective_submission_version` -- never
     from whatever a caller passes as a `positions` argument (which may be
     an unsubmitted, private draft; see `LockoutRepository.lock_state`'s
     docstring). A draft selection can therefore never occupy the one
     immutable evidence slot a position has ahead of the player actually,
     officially selected there.
   - **An observation survives a rejected mutation.** `_materialize_lineup`
     (and, ahead of it, `_materialize_round_triggers`) runs in its own
     standalone transaction that commits independently of whatever happens
     afterwards. `WeeklyLineupRepository.submit` calls both (via
     `LockGuard.materialize`) *before* opening its own transaction, so a
     lock discovered while evaluating a submission that is then rejected is
     not lost when that submission's transaction rolls back.
     `guard_transition` itself is read-only with respect to both
     `weekly_lineup_lock` and `bbbffl_round_lockout_trigger_activation`
     (`materializable=False`) for exactly this reason. The residual: a
     trigger that activates in the narrow window between `materialize()`
     returning and `guard_transition` running is still correctly *rejected*
     (live evaluation always governs the accept/reject decision), but its
     evidence is not durably written until the next observation -- an
     eventual-consistency gap, never a correctness one.

Before a trigger has activated, a legitimate reconfiguration (a different
associated match, a different type) *does* move the boundary -- there is
nothing to protect yet, so the new configuration simply governs the next
evaluation. `LockoutTriggerRepository.replace` requires a reason and is
itself an audited action (`app.audit.LOCKOUT_TRIGGER_CONFIGURED`), unlike
ordinary deterministic lock evaluation/materialization, which stays silent
-- see "Audit" in docs/lockouts.md.

## Concurrency

`_materialize_round_triggers`, `_materialize_lineup` and the read-only pass
in `lock_state`/`guard_transition` each run in their own transaction rather
than nested inside another already-open one: SQLite allows only one writer
at a time, so nesting a second write transaction inside
`WeeklyLineupRepository.submit`'s already-open one would deadlock/fail
there. Sequencing them instead (materialize the round's triggers, then the
lineup's evidence, *then* open the guarded transaction) avoids that while
still giving PostgreSQL real concurrent-writer guarantees:
`LockoutTriggerRepository.replace` and `_materialize_round_triggers` both
take a `FOR UPDATE` lock on the same trigger header row before checking/
recording activation, so a commissioner's reconfiguration and a concurrent
activation observation cannot interleave unsafely -- one fully completes
before the other proceeds. `submit`'s own row lock on `weekly_lineup`
similarly serializes concurrent submissions for one lineup; `guard_transition`
runs after that lock is acquired and always re-reads live trigger/lock
state rather than trusting anything the caller precomputed, so a submission
prepared against a stale lockout-plan revision cannot succeed once that
revision is no longer current -- it either commits against genuinely
current state or fails with `LockedSelectionError`. Two independent
connections racing to *observe* (not mutate) a never-before-evaluated lock
rely on `INSERT ... ON CONFLICT DO NOTHING` to converge to one row rather
than crashing or diverging. Nothing here uses client/browser-supplied
timestamps. See `tests/test_lockouts_concurrency.py`.

## Non-goals (see issue #34)

No DNP/Interchange replacement decision, no carry-forward/proxy submission,
no scorer override/correction workflow beyond `LockoutTriggerRepository`'s
pre-activation `replace`, no coach/commissioner UI (the repository/service
boundary here is what a later management UI would call).
`PositionLockState`/`LineupLockView` exist so a later UI can explain lock
state without recomputing these rules itself.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from sqlalchemy.exc import IntegrityError

from app.afl_client import Match, is_recognized_match_status, normalize_match_status
from app.audit import ENTITY_TYPE_LOCKOUT_TRIGGER, LOCKOUT_TRIGGER_CONFIGURED, ActorContext, append_event
from app.db import _for_update_suffix, transaction
from app.lineups import POSITIONS
from app.season import _id, _now

LOCKOUT_TRIGGER_TYPES = frozenset({"selective", "main"})


class LockoutIntegrityError(ValueError):
    """Base class for this module's domain errors."""


class MatchResolutionError(LockoutIntegrityError):
    """A selected player could not be resolved to exactly one AFL match."""


class LockedSelectionError(LockoutIntegrityError):
    """An ordinary edit attempted to mutate a locked/indeterminate position."""


class TriggerAlreadyActivatedError(LockoutIntegrityError):
    """An attempt was made to revise a trigger whose configuration has
    already caused a durable lockout activation -- its match set is now
    permanently frozen (see this module's docstring, 'Historical
    irreversibility', layer 1)."""


class LockState(str, Enum):
    EDITABLE = "editable"
    LOCKED = "locked"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class PositionLockState:
    """Read-model row explaining one position's lock state, for a later
    coach/scorer UI -- never recomputed hidden rules client-side."""

    position: str
    season_player_id: str | None
    state: LockState
    reason: str
    afl_match_id: int | None
    effective_lock_at: str | None
    observed_status: str | None
    irreversible: bool


@dataclass(frozen=True)
class LineupLockView:
    lineup_id: str
    bbbffl_round_id: str
    season_entry_id: str
    evaluated_at: str
    positions: dict[str, PositionLockState]


@dataclass(frozen=True)
class LockoutTrigger:
    """One configured stage of a BBBFFL round's lockout plan. `revision`
    only ever advances before the trigger activates (see
    `LockoutTriggerRepository.replace`); afterwards it is frozen."""

    trigger_id: str
    bbbffl_round_id: str
    trigger_key: str
    revision: int
    trigger_type: str  # "selective" | "main"
    sequence: int
    afl_match_ids: tuple[int, ...]
    created_at: str
    created_by: str | None
    reason: str | None


@dataclass(frozen=True)
class TriggerCoverage:
    """A round's currently-activated trigger state, as consulted by
    `_evaluate_position`. `configured` is false only when the round has no
    lockout plan at all (fails closed to indeterminate, never guessed)."""

    configured: bool
    locked_match_ids: frozenset[int]
    main_activated: bool


class MatchFactsProvider(Protocol):
    """Duck-typed source of AFL match facts for one BBBFFL round. Tests
    supply a fixed fake; production composes the accepted round mapping
    with the real `AflApiClient` (see `RoundMatchFactsProvider`)."""

    def matches_for(self, bbbffl_round_id: str) -> list[Match]: ...


class RoundMatchFactsProvider:
    """The only place this module touches afl-api, and only through the
    accepted BBBFFL -> AFL round mapping (app.round_mapping) -- never a
    provider "current round" or other global/implicit state."""

    def __init__(self, round_mappings, afl_client):
        self._round_mappings = round_mappings
        self._afl_client = afl_client

    def matches_for(self, bbbffl_round_id: str) -> list[Match]:
        mapping = self._round_mappings.resolve(bbbffl_round_id)
        if mapping is None:
            raise MatchResolutionError(f"BBBFFL round {bbbffl_round_id} has no accepted AFL round mapping")
        return self._afl_client.get_matches(mapping.afl_round_id)

    def evaluation_at(self) -> datetime | None:
        """Return a replay's explicit clock, if it has one.

        Live AFL clients have no ``clock`` and therefore retain the normal
        wall-clock evaluation. This is only clock plumbing; trigger coverage
        and activation continue to use the persisted BBBFFL plan below.
        """
        clock = getattr(self._afl_client, "clock", None)
        return clock.now() if clock is not None else None


def _evaluation_at(explicit: datetime | None, match_facts: MatchFactsProvider) -> datetime:
    if explicit is not None:
        return explicit
    provider_clock = getattr(match_facts, "evaluation_at", None)
    replay_at = provider_clock() if provider_clock is not None else None
    return replay_at or datetime.now(timezone.utc)


def _parse_instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def evaluate_match_lock(match: Match, evaluation_at: datetime) -> tuple[LockState, str]:
    """Pure, deterministic boundary check for one AFL match at one instant
    -- used to decide whether a *trigger* associated with this match has
    reached the point where it activates. No I/O, no persisted evidence."""
    if not is_recognized_match_status(match.status):
        return LockState.INDETERMINATE, f"unrecognized_status:{match.status}"
    normalized = normalize_match_status(match.status)
    if normalized in ("live", "postgame", "completed"):
        return LockState.LOCKED, f"match_status_{normalized}"
    # normalized == "yet_to_play" (UPCOMING or a tolerated alias): status
    # alone does not yet require a lock, so fall back to scheduled time.
    if match.start_time_utc is None:
        return LockState.INDETERMINATE, "missing_scheduled_start_time"
    start = _parse_instant(match.start_time_utc)
    if evaluation_at >= start:
        return LockState.LOCKED, "match_time_reached"
    return LockState.EDITABLE, "not_yet_started"


def resolve_match(afl_team_id: int | None, matches: list[Match]) -> Match:
    """Resolve a season player's cached `afl_team_id` to exactly one AFL
    match. Never a name-based join; fails explicitly (never guesses) if the
    match cannot be reliably resolved."""
    if afl_team_id is None:
        raise MatchResolutionError("selected player has no known AFL club; cannot resolve an AFL match")
    found = [match for match in matches if match.involves_team(afl_team_id)]
    if not found:
        raise MatchResolutionError(f"no AFL match found for team {afl_team_id} in the mapped round")
    if len(found) > 1:
        raise MatchResolutionError(f"ambiguous AFL match resolution for team {afl_team_id}: {len(found)} matches found")
    return found[0]


class LockoutTriggerRepository:
    """Persisted BBBFFL round lockout-trigger configuration: which AFL
    matches constitute the round's selective (early) and main lockout
    stages. A BBBFFL competition decision -- never inferred from day of
    week, match order, "first match of the round", AFL round number, or
    browser/UI state (see this module's docstring).

    This is the persistence/repository/service boundary a later
    commissioner/scorer management UI would call; no UI is built here.
    """

    def __init__(self, database):
        self.database = database

    def create(
        self,
        bbbffl_round_id: str,
        trigger_key: str,
        trigger_type: str,
        sequence: int,
        afl_match_ids,
        *,
        actor: ActorContext = ActorContext.anonymous_operator("admin"),
        reason: str | None = None,
    ) -> LockoutTrigger:
        """Create a brand-new trigger slot for this round. `trigger_key` is
        a stable, caller-chosen identity (e.g. "early-1", "main") unique
        within the round, addressed again by `replace`/`get`."""
        match_ids = self._validated_matches(trigger_type, afl_match_ids)
        with transaction(self.database) as conn:
            if trigger_type == "main":
                self._reject_duplicate_main(conn, bbbffl_round_id, exclude_trigger_key=None)
            trigger_id, revision, now = _id(), 1, _now()
            try:
                conn.execute(
                    "INSERT INTO bbbffl_round_lockout_trigger VALUES (?, ?, ?, ?, ?)",
                    (trigger_id, bbbffl_round_id, trigger_key, revision, now),
                )
            except IntegrityError as exc:
                raise LockoutIntegrityError(
                    f"trigger key {trigger_key!r} already exists for round {bbbffl_round_id}"
                ) from exc
            self._insert_revision(conn, trigger_id, revision, trigger_type, sequence, match_ids, actor, reason, now)
            append_event(
                conn,
                actor=actor,
                action=LOCKOUT_TRIGGER_CONFIGURED,
                entity_type=ENTITY_TYPE_LOCKOUT_TRIGGER,
                entity_id=trigger_id,
                entity_version=str(revision),
                reason=reason,
                after_state={"trigger_type": trigger_type, "sequence": sequence, "afl_match_ids": list(match_ids)},
            )
        return self.get(bbbffl_round_id, trigger_key)

    def replace(
        self,
        bbbffl_round_id: str,
        trigger_key: str,
        *,
        trigger_type: str,
        sequence: int,
        afl_match_ids,
        actor: ActorContext = ActorContext.anonymous_operator("admin"),
        reason: str,
    ) -> LockoutTrigger:
        """Revise an existing trigger's configuration -- only while it has
        not yet activated (see `TriggerAlreadyActivatedError`). Always
        requires a reason, matching this codebase's other pre-activation
        revision points (e.g. `app.round_mapping`)."""
        if not reason:
            raise ValueError("replacing a trigger's configuration requires a reason")
        match_ids = self._validated_matches(trigger_type, afl_match_ids)
        with transaction(self.database) as conn:
            head = conn.execute(
                "SELECT * FROM bbbffl_round_lockout_trigger WHERE bbbffl_round_id=? AND trigger_key=?"
                + _for_update_suffix(self.database),
                (bbbffl_round_id, trigger_key),
            ).fetchone()
            if not head:
                raise KeyError((bbbffl_round_id, trigger_key))
            if conn.execute(
                "SELECT 1 FROM bbbffl_round_lockout_trigger_activation WHERE trigger_id=?",
                (head["trigger_id"],),
            ).fetchone():
                raise TriggerAlreadyActivatedError(
                    f"trigger {trigger_key!r} has already activated; its configuration is now permanently frozen"
                )
            if trigger_type == "main":
                self._reject_duplicate_main(conn, bbbffl_round_id, exclude_trigger_key=trigger_key)
            revision, now = head["current_revision"] + 1, _now()
            conn.execute(
                "UPDATE bbbffl_round_lockout_trigger SET current_revision=? WHERE trigger_id=?",
                (revision, head["trigger_id"]),
            )
            self._insert_revision(
                conn, head["trigger_id"], revision, trigger_type, sequence, match_ids, actor, reason, now
            )
            append_event(
                conn,
                actor=actor,
                action=LOCKOUT_TRIGGER_CONFIGURED,
                entity_type=ENTITY_TYPE_LOCKOUT_TRIGGER,
                entity_id=head["trigger_id"],
                entity_version=str(revision),
                reason=reason,
                after_state={"trigger_type": trigger_type, "sequence": sequence, "afl_match_ids": list(match_ids)},
            )
        return self.get(bbbffl_round_id, trigger_key)

    def get(self, bbbffl_round_id: str, trigger_key: str) -> LockoutTrigger | None:
        row = self.database.execute(
            "SELECT t.trigger_id, t.bbbffl_round_id, t.trigger_key, r.revision, r.trigger_type, r.sequence, r.created_at, r.created_by, r.reason "
            "FROM bbbffl_round_lockout_trigger t "
            "JOIN bbbffl_round_lockout_trigger_revision r ON r.trigger_id=t.trigger_id AND r.revision=t.current_revision "
            "WHERE t.bbbffl_round_id=? AND t.trigger_key=?",
            (bbbffl_round_id, trigger_key),
        ).fetchone()
        if not row:
            return None
        return self._to_trigger(row)

    def list_triggers(self, bbbffl_round_id: str) -> list[LockoutTrigger]:
        rows = self.database.execute(
            "SELECT t.trigger_id, t.bbbffl_round_id, t.trigger_key, r.revision, r.trigger_type, r.sequence, r.created_at, r.created_by, r.reason "
            "FROM bbbffl_round_lockout_trigger t "
            "JOIN bbbffl_round_lockout_trigger_revision r ON r.trigger_id=t.trigger_id AND r.revision=t.current_revision "
            "WHERE t.bbbffl_round_id=? ORDER BY r.sequence, t.trigger_key",
            (bbbffl_round_id,),
        ).fetchall()
        return [self._to_trigger(row) for row in rows]

    def _to_trigger(self, row) -> LockoutTrigger:
        match_ids = tuple(
            r["afl_match_id"]
            for r in self.database.execute(
                "SELECT afl_match_id FROM bbbffl_round_lockout_trigger_match WHERE trigger_id=? AND revision=? ORDER BY afl_match_id",
                (row["trigger_id"], row["revision"]),
            ).fetchall()
        )
        return LockoutTrigger(
            row["trigger_id"],
            row["bbbffl_round_id"],
            row["trigger_key"],
            row["revision"],
            row["trigger_type"],
            row["sequence"],
            match_ids,
            row["created_at"],
            row["created_by"],
            row["reason"],
        )

    def _reject_duplicate_main(self, conn, bbbffl_round_id, *, exclude_trigger_key):
        rows = conn.execute(
            "SELECT t.trigger_key FROM bbbffl_round_lockout_trigger t "
            "JOIN bbbffl_round_lockout_trigger_revision r ON r.trigger_id=t.trigger_id AND r.revision=t.current_revision "
            "WHERE t.bbbffl_round_id=? AND r.trigger_type='main'",
            (bbbffl_round_id,),
        ).fetchall()
        others = [r["trigger_key"] for r in rows if r["trigger_key"] != exclude_trigger_key]
        if others:
            raise LockoutIntegrityError(
                f"round {bbbffl_round_id} already has a main trigger ({others[0]!r}); a round has at most one"
            )

    @staticmethod
    def _validated_matches(trigger_type, afl_match_ids) -> tuple[int, ...]:
        if trigger_type not in LOCKOUT_TRIGGER_TYPES:
            raise ValueError(f"trigger_type must be one of {sorted(LOCKOUT_TRIGGER_TYPES)}")
        match_ids = tuple(afl_match_ids)
        if not match_ids:
            raise ValueError("a trigger requires at least one associated AFL match")
        if len(match_ids) != len(set(match_ids)):
            raise ValueError("a trigger's AFL match IDs must be unique")
        return match_ids

    @staticmethod
    def _insert_revision(conn, trigger_id, revision, trigger_type, sequence, match_ids, actor, reason, now):
        conn.execute(
            "INSERT INTO bbbffl_round_lockout_trigger_revision VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trigger_id, revision, trigger_type, sequence, now, actor.actor_id, reason),
        )
        for afl_match_id in match_ids:
            conn.execute(
                "INSERT INTO bbbffl_round_lockout_trigger_match VALUES (?, ?, ?)",
                (trigger_id, revision, afl_match_id),
            )


class LockGuard:
    """The object returned by `LockoutRepository.guard`; usable directly as
    `app.lineups.WeeklyLineupRepository.submit`'s `lock_guard` argument.

    Two methods, called at two different points relative to `submit`'s own
    transaction -- see this module's docstring ("Historical
    irreversibility") for why that split exists:

    - `materialize(lineup_id)`: called by `submit` *before* it opens its own
      transaction. Durably records the round's trigger activations and the
      lineup's own lock evidence, independent of whatever `submit` goes on
      to do.
    - `__call__(conn, lineup_row, previous_positions, proposed_positions)`:
      called by `submit` *inside* its own transaction (on `conn`, the same
      connection already holding a `FOR UPDATE` lock on `weekly_lineup`).
      Accepts or rejects the proposed change; never writes anything itself.
    """

    def __init__(
        self, repository: "LockoutRepository", match_facts: MatchFactsProvider, evaluation_at: datetime | None
    ):
        self._repository = repository
        self._match_facts = match_facts
        self._evaluation_at = evaluation_at

    def _at(self) -> datetime:
        return _evaluation_at(self._evaluation_at, self._match_facts)

    def materialize(self, lineup_id: str) -> None:
        self._repository._materialize(lineup_id, match_facts=self._match_facts, evaluation_at=self._at())

    def __call__(self, conn, lineup_row, previous_positions, proposed_positions) -> None:
        at = self._at()
        matches = self._match_facts.matches_for(lineup_row["bbbffl_round_id"])
        coverage = self._repository._trigger_coverage(conn, lineup_row["bbbffl_round_id"])
        self._repository.guard_transition(
            conn,
            lineup_row["lineup_id"],
            previous_positions,
            proposed_positions,
            evaluation_at=at,
            matches=matches,
            coverage=coverage,
        )


class LockoutRepository:
    """Durable lockout evidence and the enforcement/read-model boundary.

    Ordinary deterministic evaluation never calls `app.audit.append_event`
    -- it is not a privileged action, merely an observation of already-
    authoritative AFL/trigger facts. Only `LockoutTriggerRepository`'s
    configuration writes are audited.
    """

    def __init__(self, database):
        self.database = database

    # -- Read model ------------------------------------------------------
    def lock_state(
        self,
        lineup_id: str,
        bbbffl_round_id: str,
        season_entry_id: str,
        positions: dict,
        *,
        match_facts: MatchFactsProvider,
        evaluation_at: datetime | None = None,
    ) -> LineupLockView:
        """Evaluate `positions` (which may be a private draft, the effective
        submission, or any other `{position: season_player_id}` mapping the
        caller has) for one lineup, for display/explanation purposes.

        This always first durably materializes the round's trigger
        activations and evidence for the lineup's *actual effective
        submission* (never for `positions` itself, which might not match it
        -- see this module's docstring's player-level durability
        invariants), then separately evaluates `positions` purely
        informationally: a position matching the effective selection comes
        back backed by that now-guaranteed-persisted evidence; a position
        that differs (an unsubmitted draft choice) is evaluated live and
        reported, but never written to `weekly_lineup_lock`.
        """
        unknown = set(positions) - set(POSITIONS)
        if unknown:
            raise LockoutIntegrityError(f"unknown scoring positions: {sorted(unknown)}")
        at = _evaluation_at(evaluation_at, match_facts)
        self._materialize_round_triggers(bbbffl_round_id, match_facts=match_facts, evaluation_at=at)
        self._materialize_lineup(lineup_id, match_facts=match_facts, evaluation_at=at)
        matches = match_facts.matches_for(bbbffl_round_id)
        with transaction(self.database) as conn:
            existing = self._existing_locks(conn, lineup_id)
            coverage = self._trigger_coverage(conn, bbbffl_round_id)
            view = {
                position: self._evaluate_position(
                    conn, existing, lineup_id, position, season_player_id, at, matches, coverage, materializable=False
                )
                for position, season_player_id in positions.items()
            }
        return LineupLockView(lineup_id, bbbffl_round_id, season_entry_id, at.isoformat(), view)

    # -- Enforcement -------------------------------------------------------
    def guard(self, *, match_facts: MatchFactsProvider, evaluation_at: datetime | None = None) -> LockGuard:
        """Build the `lock_guard` accepted by
        `app.lineups.WeeklyLineupRepository.submit`. See `LockGuard`."""
        return LockGuard(self, match_facts, evaluation_at)

    def guard_transition(
        self,
        conn,
        lineup_id: str,
        previous_positions: dict,
        proposed_positions: dict,
        *,
        evaluation_at: datetime,
        matches: list[Match],
        coverage: TriggerCoverage,
    ) -> None:
        """Reject a proposed submission that mutates a locked or
        indeterminate position, or that introduces a brand-new player whose
        own AFL match is already covered by an activated trigger (or main
        has already activated) anywhere in the lineup -- which is what
        stops Interchange (or any other still-open position) being used to
        bypass an activated trigger's lock, per issue #34's "no additional
        player from those AFL clubs may subsequently be added" rule.

        Read-only with respect to persisted evidence (`materializable=
        False` throughout): this runs inside the caller's own transaction,
        which may go on to roll back if this raises, so nothing durable can
        be written here -- see `LockGuard`/this module's docstring."""
        existing = self._existing_locks(conn, lineup_id)
        for position, previous_player in previous_positions.items():
            proposed_player = proposed_positions.get(position)
            evaluated = self._evaluate_position(
                conn,
                existing,
                lineup_id,
                position,
                previous_player,
                evaluation_at,
                matches,
                coverage,
                materializable=False,
            )
            if evaluated.state in (LockState.LOCKED, LockState.INDETERMINATE) and proposed_player != previous_player:
                raise LockedSelectionError(
                    f"position {position} cannot be changed ({evaluated.state.value}: {evaluated.reason})"
                )
        for position, proposed_player in proposed_positions.items():
            if proposed_player is None or proposed_player == previous_positions.get(position):
                continue
            evaluated = self._evaluate_position(
                conn,
                existing,
                lineup_id,
                position,
                proposed_player,
                evaluation_at,
                matches,
                coverage,
                materializable=False,
            )
            if evaluated.state != LockState.EDITABLE:
                raise LockedSelectionError(
                    f"cannot select a player for {position} whose AFL match is not editable "
                    f"({evaluated.state.value}: {evaluated.reason})"
                )

    # -- Internals: orchestration -------------------------------------------
    def _materialize(self, lineup_id: str, *, match_facts: MatchFactsProvider, evaluation_at: datetime) -> None:
        """Full materialization sequence for one lineup: the round's trigger
        activations first (own transaction), then the lineup's own evidence
        (own transaction) -- sequential, never nested (see this module's
        docstring, 'Concurrency')."""
        row = self.database.execute(
            "SELECT bbbffl_round_id FROM weekly_lineup WHERE lineup_id=?", (lineup_id,)
        ).fetchone()
        if row is not None:
            self._materialize_round_triggers(
                row["bbbffl_round_id"], match_facts=match_facts, evaluation_at=evaluation_at
            )
        self._materialize_lineup(lineup_id, match_facts=match_facts, evaluation_at=evaluation_at)

    def _materialize_round_triggers(
        self, bbbffl_round_id: str, *, match_facts: MatchFactsProvider, evaluation_at: datetime
    ) -> None:
        """Durably record activation for every configured trigger in this
        round whose associated matches have reached lock boundary, in a
        standalone transaction. Idempotent (`ON CONFLICT DO NOTHING` via
        `_insert_trigger_activation`); a round with no configured triggers
        is a safe no-op."""
        with transaction(self.database) as conn:
            trigger_ids = [
                r["trigger_id"]
                for r in conn.execute(
                    "SELECT trigger_id FROM bbbffl_round_lockout_trigger WHERE bbbffl_round_id=?", (bbbffl_round_id,)
                ).fetchall()
            ]
            if not trigger_ids:
                return
            matches_by_id = {match.match_id: match for match in match_facts.matches_for(bbbffl_round_id)}
            for trigger_id in trigger_ids:
                # Lock this trigger's header row so a concurrent
                # LockoutTriggerRepository.replace (which takes the same
                # lock) cannot swap its configuration out from under this
                # activation check.
                head = conn.execute(
                    "SELECT current_revision FROM bbbffl_round_lockout_trigger WHERE trigger_id=?"
                    + _for_update_suffix(self.database),
                    (trigger_id,),
                ).fetchone()
                if conn.execute(
                    "SELECT 1 FROM bbbffl_round_lockout_trigger_activation WHERE trigger_id=?", (trigger_id,)
                ).fetchone():
                    continue
                revision = head["current_revision"]
                match_ids = [
                    r["afl_match_id"]
                    for r in conn.execute(
                        "SELECT afl_match_id FROM bbbffl_round_lockout_trigger_match WHERE trigger_id=? AND revision=?",
                        (trigger_id, revision),
                    ).fetchall()
                ]
                fired = None
                for afl_match_id in match_ids:
                    match = matches_by_id.get(afl_match_id)
                    if match is None:
                        continue
                    state, reason = evaluate_match_lock(match, evaluation_at)
                    if state == LockState.LOCKED:
                        fired = (match, reason)
                        break
                if fired is not None:
                    match, reason = fired
                    self._insert_trigger_activation(conn, trigger_id, revision, match, reason, evaluation_at)

    def _materialize_lineup(self, lineup_id: str, *, match_facts: MatchFactsProvider, evaluation_at: datetime) -> None:
        """Durably record lock evidence for `lineup_id`'s current effective
        submitted selections, in a standalone transaction that commits (or
        no-ops) independently of anything the caller does afterwards.

        Always derives the positions to evaluate from `weekly_lineup`/
        `weekly_lineup_submission_slot` itself -- never from a caller-
        supplied positions mapping -- so an unsubmitted draft can never
        occupy `weekly_lineup_lock`'s one evidence slot per position ahead
        of the player actually, officially selected there. A lineup with no
        submission yet is a safe no-op (nothing effective to protect).
        """
        with transaction(self.database) as conn:
            header = conn.execute(
                "SELECT bbbffl_round_id, effective_submission_version FROM weekly_lineup WHERE lineup_id=?",
                (lineup_id,),
            ).fetchone()
            if not header or not header["effective_submission_version"]:
                return
            slots = conn.execute(
                "SELECT position, season_player_id FROM weekly_lineup_submission_slot WHERE lineup_id=? AND version=?",
                (lineup_id, header["effective_submission_version"]),
            ).fetchall()
            effective = {row["position"]: row["season_player_id"] for row in slots}
            matches = match_facts.matches_for(header["bbbffl_round_id"])
            coverage = self._trigger_coverage(conn, header["bbbffl_round_id"])
            existing = self._existing_locks(conn, lineup_id)
            for position, season_player_id in effective.items():
                self._evaluate_position(
                    conn,
                    existing,
                    lineup_id,
                    position,
                    season_player_id,
                    evaluation_at,
                    matches,
                    coverage,
                    materializable=True,
                )

    # -- Internals: evaluation -----------------------------------------------
    def _trigger_coverage(self, conn, bbbffl_round_id: str) -> TriggerCoverage:
        configured = conn.execute(
            "SELECT 1 FROM bbbffl_round_lockout_trigger WHERE bbbffl_round_id=?", (bbbffl_round_id,)
        ).fetchone()
        if not configured:
            return TriggerCoverage(configured=False, locked_match_ids=frozenset(), main_activated=False)
        rows = conn.execute(
            "SELECT r.trigger_type, m.afl_match_id "
            "FROM bbbffl_round_lockout_trigger_activation a "
            "JOIN bbbffl_round_lockout_trigger t ON t.trigger_id=a.trigger_id "
            "JOIN bbbffl_round_lockout_trigger_revision r ON r.trigger_id=a.trigger_id AND r.revision=a.revision "
            "JOIN bbbffl_round_lockout_trigger_match m ON m.trigger_id=a.trigger_id AND m.revision=a.revision "
            "WHERE t.bbbffl_round_id=?",
            (bbbffl_round_id,),
        ).fetchall()
        locked_match_ids = frozenset(row["afl_match_id"] for row in rows if row["trigger_type"] == "selective")
        main_activated = any(row["trigger_type"] == "main" for row in rows)
        return TriggerCoverage(configured=True, locked_match_ids=locked_match_ids, main_activated=main_activated)

    def _evaluate_position(
        self,
        conn,
        existing: dict,
        lineup_id: str,
        position: str,
        season_player_id,
        evaluation_at,
        matches,
        coverage: TriggerCoverage,
        *,
        materializable: bool,
    ) -> PositionLockState:
        if season_player_id is None:
            # A deliberate vacancy has no AFL club/match to resolve, so
            # there is nothing for any trigger -- selective or main -- to
            # lock, and no lock boundary is ever invented for it (issue #98,
            # docs/lockouts.md "Deliberately vacant positions"). It stays
            # editable for as long as the round itself remains open;
            # `guard_transition`'s new-player rule still governs whichever
            # player, if any, is later introduced here.
            return PositionLockState(position, None, LockState.EDITABLE, "empty", None, None, None, False)
        row = existing.get(position)
        if row is not None and row["season_player_id"] == season_player_id:
            # Durable, irreversible: never recomputed against (possibly
            # since-corrected) live match/trigger facts.
            return PositionLockState(
                position,
                season_player_id,
                LockState.LOCKED,
                row["lock_reason"],
                row["afl_match_id"],
                row["effective_lock_at"],
                row["observed_status"],
                True,
            )
        season_player = self._season_player(conn, season_player_id)
        afl_team_id = season_player["afl_team_id"] if season_player else None
        try:
            match = resolve_match(afl_team_id, matches)
        except MatchResolutionError as exc:
            return PositionLockState(
                position, season_player_id, LockState.INDETERMINATE, str(exc), None, None, None, False
            )
        if not coverage.configured:
            state, reason = LockState.INDETERMINATE, "lockout_plan_not_configured"
        elif match.match_id in coverage.locked_match_ids:
            state, reason = LockState.LOCKED, "selective_trigger_activated"
        elif coverage.main_activated:
            state, reason = LockState.LOCKED, "main_lockout_triggered"
        else:
            state, reason = LockState.EDITABLE, "not_yet_triggered"
        if state == LockState.LOCKED and materializable:
            self._insert_lock(conn, lineup_id, position, season_player_id, match, reason, evaluation_at)
        return PositionLockState(
            position,
            season_player_id,
            state,
            reason,
            match.match_id,
            match.start_time_utc,
            match.status,
            state == LockState.LOCKED and materializable,
        )

    def _season_player(self, conn, season_player_id):
        return conn.execute(
            "SELECT afl_team_id FROM season_player_pool WHERE season_player_id=?",
            (season_player_id,),
        ).fetchone()

    def _existing_locks(self, conn, lineup_id: str) -> dict:
        rows = conn.execute(
            "SELECT position, season_player_id, afl_match_id, observed_status, effective_lock_at, lock_reason, locked_at "
            "FROM weekly_lineup_lock WHERE lineup_id=?",
            (lineup_id,),
        ).fetchall()
        return {row["position"]: row for row in rows}

    def _insert_lock(self, conn, lineup_id, position, season_player_id, match, reason, evaluation_at):
        conn.execute(
            "INSERT INTO weekly_lineup_lock "
            "(lineup_id, position, season_player_id, afl_match_id, observed_status, effective_lock_at, lock_reason, locked_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (lineup_id, position) DO NOTHING",
            (
                lineup_id,
                position,
                season_player_id,
                match.match_id,
                match.status,
                match.start_time_utc,
                reason,
                evaluation_at.isoformat(),
                _now(),
            ),
        )

    def _insert_trigger_activation(self, conn, trigger_id, revision, match, reason, evaluation_at):
        conn.execute(
            "INSERT INTO bbbffl_round_lockout_trigger_activation "
            "(trigger_id, revision, afl_match_id, observed_status, effective_lock_at, activation_reason, evaluated_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (trigger_id) DO NOTHING",
            (
                trigger_id,
                revision,
                match.match_id,
                match.status,
                match.start_time_utc,
                reason,
                evaluation_at.isoformat(),
                _now(),
            ),
        )
