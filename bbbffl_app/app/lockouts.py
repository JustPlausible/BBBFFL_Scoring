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
recorded in `weekly_lineup_lock` (`_insert_lock`, called from
`_evaluate_position` and `LockoutRepository.materialize`/`.lock_state`/
`.guard_transition`), that row -- not a fresh recomputation against
possibly-corrected afl-api facts -- is the answer for that lineup/position
from then on. A later upstream schedule/status correction therefore cannot
silently unlock history: `weekly_lineup_lock` rows are immutable (database
triggers reject UPDATE/DELETE, mirroring `weekly_lineup_submission`), and
`_evaluate_position` always prefers a matching persisted row over live
`matches` data. Nothing here rewrites `app.lineups`' immutable submitted
lineup versions; lock evidence is a separate, parallel append-only record.

## Concurrency

`guard_transition` is designed to run inside the *same* transaction/
connection that already holds a row lock on the `weekly_lineup` header (see
`app.lineups.WeeklyLineupRepository.submit`'s `FOR UPDATE` read). Two
concurrent submissions for the same lineup therefore serialize on that row
lock; the loser re-evaluates lock state (and reads any lock evidence the
winner just committed) before its own compare-and-swap, so it can never
commit a mutation against a player who was already locked at the
authoritative decision point -- it either succeeds against genuinely
pre-lock state or fails with `LockedSelectionError`. Nothing here uses
client/browser-supplied timestamps.

## Non-goals (see issue #34)

No DNP/Interchange replacement decision, no carry-forward/proxy submission,
no scorer override/correction workflow, no coach UI. `PositionLockState`/
`LineupLockView` exist so a later UI can explain lock state without
recomputing these rules itself.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Protocol

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
        """Evaluate (and durably materialize any newly-locked) positions for
        one lineup. Safe to call from a read-only endpoint at any time --
        this is the primary way lock evidence becomes durable in ordinary
        operation, ahead of any particular submission attempt. Takes plain
        identifiers (not a row/dataclass) so any caller -- a route, a test,
        a future replay harness -- can supply them however it already has
        them (a `LineupDraft`/`SubmittedLineup` from app.lineups, a raw DB
        row, ...)."""
        unknown = set(positions) - set(POSITIONS)
        if unknown:
            raise LockoutIntegrityError(f"unknown scoring positions: {sorted(unknown)}")
        at = evaluation_at or datetime.now(timezone.utc)
        matches = match_facts.matches_for(bbbffl_round_id)
        with transaction(self.database) as conn:
            existing = self._existing_locks(conn, lineup_id)
            view = {
                position: self._evaluate_position(
                    conn, existing, lineup_id, position, season_player_id, at, matches
                )
                for position, season_player_id in positions.items()
            }
        return LineupLockView(lineup_id, bbbffl_round_id, season_entry_id, at.isoformat(), view)

    # -- Enforcement -------------------------------------------------------
    def guard(
        self, *, match_facts: MatchFactsProvider, evaluation_at: datetime | None = None
    ) -> Callable:
        """Build the `lock_guard` callable accepted by
        `app.lineups.WeeklyLineupRepository.submit`. Kept as a plain
        callable (not a shared type) so `app/lineups.py` never has to
        import this module -- see this module's docstring on concurrency
        for why the callable must run inside the caller's own transaction.
        """

        def _guard(conn, lineup_row, previous_positions, proposed_positions):
            at = evaluation_at or datetime.now(timezone.utc)
            matches = match_facts.matches_for(lineup_row["bbbffl_round_id"])
            self.guard_transition(
                conn,
                lineup_row["lineup_id"],
                previous_positions,
                proposed_positions,
                evaluation_at=at,
                matches=matches,
            )

        return _guard

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
        subsequently be added" rule."""
        existing = self._existing_locks(conn, lineup_id)
        for position, previous_player in previous_positions.items():
            proposed_player = proposed_positions.get(position)
            evaluated = self._evaluate_position(
                conn, existing, lineup_id, position, previous_player, evaluation_at, matches
            )
            if evaluated.state in (LockState.LOCKED, LockState.INDETERMINATE) and proposed_player != previous_player:
                raise LockedSelectionError(
                    f"position {position} cannot be changed ({evaluated.state.value}: {evaluated.reason})"
                )
        for position, proposed_player in proposed_positions.items():
            if proposed_player is None or proposed_player == previous_positions.get(position):
                continue
            evaluated = self._evaluate_position(
                conn, existing, lineup_id, position, proposed_player, evaluation_at, matches
            )
            if evaluated.state != LockState.EDITABLE:
                raise LockedSelectionError(
                    f"cannot select a player for {position} whose AFL match is not editable "
                    f"({evaluated.state.value}: {evaluated.reason})"
                )

    # -- Internals ---------------------------------------------------------
    def _evaluate_position(
        self, conn, existing: dict, lineup_id: str, position: str, season_player_id, evaluation_at, matches
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
        if state == LockState.LOCKED:
            self._insert_lock(conn, lineup_id, position, season_player_id, match, reason, evaluation_at)
        return PositionLockState(
            position,
            season_player_id,
            state,
            reason,
            match.match_id,
            match.start_time_utc,
            match.status,
            state == LockState.LOCKED,
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

