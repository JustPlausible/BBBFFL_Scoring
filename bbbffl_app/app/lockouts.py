"""BBBFFL-owned player-level AFL-match lockout decisions.

`afl-api` remains authoritative for AFL match identity, scheduled start and
status (see app/afl_client.py). This module owns the *lockout decision*: it
resolves each selected season player to the AFL match relevant to their
accepted BBBFFL round (app/round_mapping.py's accepted mapping -- never a
provider "current round" or other global state), and combines that match's
authoritative facts with an explicit evaluation instant to decide whether an
ordinary coach edit to a BBBFFL position is permitted.

Nothing here calls `datetime.now()` on its own initiative: every entry point
either receives `evaluation_at` explicitly or defaults it once, at the outer
edge, to `datetime.now(timezone.utc)` -- so tests and 2026 replay can always
supply a fixed instant and get a deterministic answer.

## Lock boundary

A position's selected player is:

- **editable** while their AFL match's authoritative status is UPCOMING (or
  a tolerated legacy alias) and `evaluation_at` is strictly before the
  match's scheduled `start_time_utc`;
- **locked** the instant `evaluation_at` reaches or passes that scheduled
  start (`>=`), and remains locked for every later evaluation regardless of
  status, once LIVE, POSTGAME or CONCLUDED is observed (status is
  authoritative over wall-clock time -- a match already LIVE is locked even
  if `start_time_utc` is missing or in the future), and forever after,
  because commencement is irreversible (see below);
- **indeterminate** when the match's status is not one of afl-api's
  documented v1 lifecycle values/aliases (`app.afl_client.
  is_recognized_match_status`), or when it is UPCOMING but has no scheduled
  start time to evaluate against. Indeterminate positions fail closed for
  ordinary coach edits (an unchanged selection is still accepted; a change
  is rejected) rather than guessing an unlock, per the season model's
  instruction to handle postponed/abandoned/unusual matches conservatively.

## Historical irreversibility

Once a position's LOCKED state has actually been observed and durably
recorded in `weekly_lineup_lock` (`_insert_lock`), that row -- not a fresh
recomputation against possibly-corrected afl-api facts -- is the answer for
that lineup/position from then on (`_evaluate_position` always prefers a
matching persisted row over live `matches` data). `weekly_lineup_lock` rows
are immutable (database triggers reject UPDATE/DELETE, mirroring
`weekly_lineup_submission`). Nothing here rewrites `app.lineups`' immutable
submitted lineup versions; lock evidence is a separate, parallel append-only
record.

Two invariants make this durability real rather than aspirational:

1. **Only the effective submission can be materialized.** `_evaluate_position`
   only *writes* a lock row when its caller passes `materializable=True`,
   and the only caller that does is `_materialize_lineup`, which always
   derives the positions it evaluates from `weekly_lineup`'s own
   `effective_submission_version` -- never from whatever a caller happens
   to pass as a "positions" argument (which may be an unsubmitted, private
   draft; see `LockoutRepository.lock_state`'s docstring). A draft
   selection can therefore never occupy the one immutable evidence slot a
   position has (`weekly_lineup_lock`'s primary key is `(lineup_id,
   position)`) ahead of the player actually, officially selected there.
2. **An observation survives a rejected mutation.** `_materialize_lineup`
   runs in its own standalone transaction that commits independently of
   whatever happens afterwards. `WeeklyLineupRepository.submit` calls it
   *before* opening its own transaction (see `LockGuard.materialize`
   below), so a lock discovered while evaluating a submission that is then
   rejected is not lost when that submission's transaction rolls back.
   Everything evaluated *inside* the guarded transaction itself
   (`guard_transition`) is read-only with respect to `weekly_lineup_lock`
   (`materializable=False`) for exactly this reason -- a write there could
   never survive the `LockedSelectionError` it is often about to raise.
   The residual: a match that reaches its lock boundary in the narrow
   window between `materialize()` returning and `guard_transition` running
   is still correctly rejected (live evaluation always governs the
   accept/reject decision), but its evidence is not durably written until
   the next observation (the next `lock_state` read or `submit` attempt)
   -- a purely eventual-consistency gap, never a correctness one.

Before a lock exists, a legitimate schedule correction *does* move the
boundary -- there is nothing to protect yet, so the corrected
`start_time_utc` simply governs the next evaluation.

## Concurrency

`_materialize_lineup` and the read-only pass in `lock_state`/
`guard_transition` each run in their own transaction rather than a nested
one inside another already-open transaction: SQLite allows only one writer
at a time, so nesting a second write transaction inside
`WeeklyLineupRepository.submit`'s already-open one would deadlock/fail
there. Sequencing them instead (materialize, *then* open the guarded
transaction) avoids that while still giving PostgreSQL real concurrent-
writer guarantees: `guard_transition` runs inside the same transaction/
connection that already holds a row lock on the `weekly_lineup` header (see
`submit`'s `FOR UPDATE` read), so two concurrent submissions for the same
lineup serialize on that row lock; the loser re-evaluates lock state (and
reads any lock evidence the winner's `materialize()` step already
committed) before its own compare-and-swap. Two independent connections
racing to *observe* (not mutate) a never-before-evaluated lock rely on
`INSERT ... ON CONFLICT (lineup_id, position) DO NOTHING` to converge to one
row rather than crashing or diverging. Nothing here uses client/browser-
supplied timestamps. See `tests/test_lockouts_concurrency.py`.

## Non-goals (see issue #34)

No DNP/Interchange replacement decision, no carry-forward/proxy submission,
no scorer override/correction workflow, no coach UI. `PositionLockState`/
`LineupLockView` exist so a later UI can explain lock state without
recomputing these rules itself.

This module also does not model a scorer-configured "early lockout match
set" or a separate "main lockout trigger" match (`docs/plans/
2027-season-model.md`'s fuller lockout description, and the roadmap's
package-23 row) -- every AFL match is its own independent trigger for the
players who belong to it. Issue #34's own acceptance criteria and
validation matrix describe and test exactly that per-match model and do not
reference early-set/main-trigger configuration. Introducing that
distinction would add a new scorer-configured entity (which matches count
as "early", which one is "main") and materially different semantics (a
main-trigger match starting would freeze *every* remaining position,
including ones in AFL matches that have not started) -- a deliberately
separate, larger follow-up rather than a silent expansion of this issue's
scope. See docs/lockouts.md.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from app.afl_client import Match, is_recognized_match_status, normalize_match_status
from app.db import transaction
from app.lineups import POSITIONS
from app.season import _now


class LockoutIntegrityError(ValueError):
    """Base class for this module's domain errors."""


class MatchResolutionError(LockoutIntegrityError):
    """A selected player could not be resolved to exactly one AFL match."""


class LockedSelectionError(LockoutIntegrityError):
    """An ordinary edit attempted to mutate a locked/indeterminate position."""


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
            raise MatchResolutionError(
                f"BBBFFL round {bbbffl_round_id} has no accepted AFL round mapping"
            )
        return self._afl_client.get_matches(mapping.afl_round_id)


def _parse_instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def evaluate_match_lock(match: Match, evaluation_at: datetime) -> tuple[LockState, str]:
    """Pure, deterministic lock decision for one AFL match at one instant.
    No I/O, no persisted evidence -- callers that need irreversibility
    layer that on top (see `LockoutRepository`)."""
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
        raise MatchResolutionError(
            "selected player has no known AFL club; cannot resolve an AFL match"
        )
    found = [match for match in matches if match.involves_team(afl_team_id)]
    if not found:
        raise MatchResolutionError(
            f"no AFL match found for team {afl_team_id} in the mapped round"
        )
    if len(found) > 1:
        raise MatchResolutionError(
            f"ambiguous AFL match resolution for team {afl_team_id}: {len(found)} matches found"
        )
    return found[0]


class LockGuard:
    """The object returned by `LockoutRepository.guard`; usable directly as
    `app.lineups.WeeklyLineupRepository.submit`'s `lock_guard` argument.

    Two methods, called at two different points relative to `submit`'s own
    transaction -- see this module's docstring ("Historical
    irreversibility") for why that split exists:

    - `materialize(lineup_id)`: called by `submit` *before* it opens its own
      transaction. Durably records lock evidence for the lineup's current
      *effective* submission, independent of whatever `submit` goes on to
      do.
    - `__call__(conn, lineup_row, previous_positions, proposed_positions)`:
      called by `submit` *inside* its own transaction (on `conn`, the same
      connection already holding a `FOR UPDATE` lock on `weekly_lineup`).
      Accepts or rejects the proposed change; never writes to
      `weekly_lineup_lock` itself.
    """

    def __init__(self, repository: "LockoutRepository", match_facts: MatchFactsProvider, evaluation_at: datetime | None):
        self._repository = repository
        self._match_facts = match_facts
        self._evaluation_at = evaluation_at

    def _at(self) -> datetime:
        return self._evaluation_at or datetime.now(timezone.utc)

    def materialize(self, lineup_id: str) -> None:
        self._repository._materialize_lineup(lineup_id, match_facts=self._match_facts, evaluation_at=self._at())

    def __call__(self, conn, lineup_row, previous_positions, proposed_positions) -> None:
        at = self._at()
        matches = self._match_facts.matches_for(lineup_row["bbbffl_round_id"])
        self._repository.guard_transition(
            conn, lineup_row["lineup_id"], previous_positions, proposed_positions, evaluation_at=at, matches=matches
        )


class LockoutRepository:
    """Durable lockout evidence and the enforcement/read-model boundary.

    Ordinary deterministic evaluation never calls `app.audit.append_event`
    -- it is not a privileged action, merely an observation of already-
    authoritative AFL facts (see this module's docstring and issue #34's
    explicit "ordinary ... reads should not generate noisy audit records").
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

        This always first durably materializes evidence for the lineup's
        *actual effective submission* (never for `positions` itself, which
        might not match it -- see this module's docstring's first
        durability invariant), then separately evaluates `positions` purely
        informationally: a position matching the effective selection comes
        back backed by that now-guaranteed-persisted evidence; a position
        that differs (an unsubmitted draft choice) is evaluated live and
        reported, but never written to `weekly_lineup_lock`.
        """
        unknown = set(positions) - set(POSITIONS)
        if unknown:
            raise LockoutIntegrityError(f"unknown scoring positions: {sorted(unknown)}")
        at = evaluation_at or datetime.now(timezone.utc)
        self._materialize_lineup(lineup_id, match_facts=match_facts, evaluation_at=at)
        matches = match_facts.matches_for(bbbffl_round_id)
        with transaction(self.database) as conn:
            existing = self._existing_locks(conn, lineup_id)
            view = {
                position: self._evaluate_position(
                    conn, existing, lineup_id, position, season_player_id, at, matches, materializable=False
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
    ) -> None:
        """Reject a proposed submission that mutates a locked or
        indeterminate position, or that introduces a brand-new player whose
        own AFL match has already reached a non-editable state anywhere in
        the lineup -- which is what stops Interchange (or any other
        still-open position) being used to bypass a started club's lock,
        per issue #34's "no additional player from those AFL clubs may
        subsequently be added" rule.

        Read-only with respect to `weekly_lineup_lock` (`materializable=
        False` throughout): this runs inside the caller's own transaction,
        which may go on to roll back if this raises, so nothing durable can
        be written here -- see `LockGuard`/this module's docstring."""
        existing = self._existing_locks(conn, lineup_id)
        for position, previous_player in previous_positions.items():
            proposed_player = proposed_positions.get(position)
            evaluated = self._evaluate_position(
                conn, existing, lineup_id, position, previous_player, evaluation_at, matches, materializable=False
            )
            if evaluated.state in (LockState.LOCKED, LockState.INDETERMINATE) and proposed_player != previous_player:
                raise LockedSelectionError(
                    f"position {position} cannot be changed ({evaluated.state.value}: {evaluated.reason})"
                )
        for position, proposed_player in proposed_positions.items():
            if proposed_player is None or proposed_player == previous_positions.get(position):
                continue
            evaluated = self._evaluate_position(
                conn, existing, lineup_id, position, proposed_player, evaluation_at, matches, materializable=False
            )
            if evaluated.state != LockState.EDITABLE:
                raise LockedSelectionError(
                    f"cannot select a player for {position} whose AFL match is not editable "
                    f"({evaluated.state.value}: {evaluated.reason})"
                )

    # -- Internals ---------------------------------------------------------
    def _materialize_lineup(self, lineup_id: str, *, match_facts: MatchFactsProvider, evaluation_at: datetime) -> None:
        """Durably record lock evidence for `lineup_id`'s current effective
        submitted selections, in a standalone transaction that commits
        (or no-ops) independently of anything the caller does afterwards.

        Always derives the positions to evaluate from `weekly_lineup`/
        `weekly_lineup_submission_slot` itself -- never from a caller-
        supplied positions mapping -- so an unsubmitted draft can never
        occupy `weekly_lineup_lock`'s one evidence slot per position ahead
        of the player actually, officially selected there. A lineup with no
        submission yet is a safe no-op (nothing effective to protect).

        Deliberately a separate transaction rather than nested inside a
        caller's already-open one: SQLite allows only one writer, so
        nesting here would conflict with `WeeklyLineupRepository.submit`'s
        own open transaction on that dialect.
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
            existing = self._existing_locks(conn, lineup_id)
            for position, season_player_id in effective.items():
                self._evaluate_position(
                    conn, existing, lineup_id, position, season_player_id, evaluation_at, matches, materializable=True
                )

    def _evaluate_position(
        self, conn, existing: dict, lineup_id: str, position: str, season_player_id, evaluation_at, matches, *, materializable: bool
    ) -> PositionLockState:
        if season_player_id is None:
            return PositionLockState(position, None, LockState.EDITABLE, "empty", None, None, None, False)
        row = existing.get(position)
        if row is not None and row["season_player_id"] == season_player_id:
            # Durable, irreversible: never recomputed against (possibly
            # since-corrected) live match facts.
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
            return PositionLockState(position, season_player_id, LockState.INDETERMINATE, str(exc), None, None, None, False)
        state, reason = evaluate_match_lock(match, evaluation_at)
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
