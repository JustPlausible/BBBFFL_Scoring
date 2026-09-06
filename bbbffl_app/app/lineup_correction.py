"""Audited Scorer/Admin correction of an authoritative weekly lineup after a
selective or main lockout has already activated (issue #137).

This is a narrowly authorised **competition correction** -- the league
confirming that a submitted lineup was recorded incorrectly and directing
the scorer to fix it -- never an ordinary coach/proxy lineup edit and never
a numeric score override. It is the one door into an already-locked
position, and it is deliberately narrower than `app.lineup_proxy`: generic
proxy-entry authority (`lineup.proxy`) is never sufficient here (see
`_ensure_correction_actor` below); a season-scoped `lineup.correct_locked`
capability is required (`app.authorization`).

## Relationship to `app.lineups.WeeklyLineupRepository.submit_correction`

`WeeklyLineupRepository.submit_correction` is the domain primitive: it
enforces the round-state/CAS/ownership/Opening-Round-deferred rules and
writes the new immutable submission version plus its correction-provenance
record, atomically, in one transaction. This module is the thin,
reason-checked, human-readable orchestration layer on top of it --
analogous to how `app.lineup_proxy.LineupProxyService` sits on top of
`WeeklyLineupRepository.submit`/`submit_positions`:

- `_ensure_correction_actor` rejects any actor that is not an
  `anonymous_operator` with `actor_role` scorer/admin/replay_operator --
  the same "actor, never the coach" convention `app.lineup_proxy` uses,
  never trusting a client-supplied role.
- `correct()` accepts a *partial* position-change map (e.g. just the two
  slots of a Tackler <-> Interchange swap) and merges it onto the lineup's
  current effective positions to build the complete, atomic corrected
  position map `submit_correction` requires -- so a caller only has to
  describe what actually changed, while the domain layer still validates
  (and persists) the single final proposed state.
- `describe()` builds the human-readable read model a Scorer/Admin
  correction UI needs: current lineup with position-level lock evidence
  (`app.lockouts.LockoutRepository.lock_state`), and the complete audited
  correction history for this lineup -- both resolved to player/club names,
  never bare internal IDs.
"""

from dataclasses import dataclass

from app.audit import ActorContext
from app.coach_lineup import CoachLineupService
from app.identity import IdentityRepository, team_display_label
from app.lineups import POSITIONS, LineupCorrection, LineupIntegrityError, NoEffectiveSubmissionError

CORRECTION_ACTOR_ROLES = frozenset({"scorer", "admin", "replay_operator"})


class LineupCorrectionServiceError(LineupIntegrityError):
    """Base class for this module's domain errors."""


class UnauthorizedCorrectionActorError(LineupCorrectionServiceError):
    """The supplied actor is not a recognised scorer/admin/replay-operator
    operator context -- see this module's docstring."""


def _ensure_correction_actor(actor: ActorContext) -> None:
    if actor.actor_type != "anonymous_operator" or actor.actor_role not in CORRECTION_ACTOR_ROLES:
        raise UnauthorizedCorrectionActorError(
            "locked-lineup corrections require an anonymous_operator actor with actor_role scorer, admin, "
            f"or replay_operator, got actor_type={actor.actor_type!r} actor_role={actor.actor_role!r}"
        )


@dataclass(frozen=True)
class CorrectionSlotView:
    position: str
    season_player_id: str | None
    player_display_name: str | None
    afl_club_name: str | None
    lock_state: str
    lock_reason: str | None
    afl_match_id: int | None
    effective_lock_at: str | None
    irreversible: bool


@dataclass(frozen=True)
class LineupCorrectionCandidate:
    """The current-state read model a Scorer/Admin correction UI needs:
    human-readable lineup + position-level lock evidence, plus the complete
    correction history for this lineup (issue #137's UI requirements)."""

    lineup_id: str
    season_id: str
    competition_id: str
    bbbffl_round_id: str
    season_entry_id: str
    expected_submission_version: int
    round_state: str
    slots: list[CorrectionSlotView]
    correction_history: list[dict]
    available_players: list[dict]


class LineupCorrectionService:
    def __init__(self, database, afl_client):
        self.database = database
        self._coach_lineup = CoachLineupService(database, afl_client)
        self.lineups = self._coach_lineup.lineups
        self.lockouts = self._coach_lineup.lockouts
        self.match_facts = self._coach_lineup.match_facts
        self.pool = self._coach_lineup.pool
        self.ownership = self._coach_lineup.ownership
        self._identities = IdentityRepository(database)

    def describe(self, season_id: str, competition_id: str, bbbffl_round_id: str, season_entry_id: str):
        lineup_id, effective_version = self.lineups.get_or_create_header(
            season_id, competition_id, bbbffl_round_id, season_entry_id
        )
        submission = self.lineups.get_effective_submission(lineup_id)
        positions = submission.positions if submission is not None else {position: None for position in POSITIONS}
        lock_view = self.lockouts.lock_state(
            lineup_id, bbbffl_round_id, season_entry_id, positions, match_facts=self.match_facts
        )
        round_row = self.database.execute(
            "SELECT state FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?", (bbbffl_round_id,)
        ).fetchone()
        slots = [
            CorrectionSlotView(
                position=position,
                season_player_id=positions.get(position),
                player_display_name=self._player_name(positions.get(position)),
                afl_club_name=self._player_club(positions.get(position)),
                lock_state=lock_view.positions[position].state.value,
                lock_reason=lock_view.positions[position].reason,
                afl_match_id=lock_view.positions[position].afl_match_id,
                effective_lock_at=lock_view.positions[position].effective_lock_at,
                irreversible=lock_view.positions[position].irreversible,
            )
            for position in POSITIONS
        ]
        available_players = []
        for period in self.ownership.current_squad(season_entry_id):
            player = self.pool.get_by_id(period.season_player_id)
            if player is None:
                continue
            available_players.append(
                {
                    "season_player_id": player.season_player_id,
                    "display_name": player.display_name,
                    "afl_club_name": player.afl_team_name,
                }
            )
        return LineupCorrectionCandidate(
            lineup_id=lineup_id,
            season_id=season_id,
            competition_id=competition_id,
            bbbffl_round_id=bbbffl_round_id,
            season_entry_id=season_entry_id,
            expected_submission_version=effective_version,
            round_state=round_row["state"] if round_row else "unknown",
            slots=slots,
            correction_history=[self._describe_correction(c) for c in self.lineups.list_corrections(lineup_id)],
            available_players=available_players,
        )

    def correct(
        self,
        season_id: str,
        competition_id: str,
        bbbffl_round_id: str,
        season_entry_id: str,
        position_changes: dict,
        *,
        expected_submission_version: int,
        actor: ActorContext,
        reason: str,
    ) -> LineupCorrection:
        """Merge `position_changes` (a partial `{position: season_player_id
        | None}` map -- only the slots actually changing) onto the lineup's
        current effective positions and submit the resulting complete,
        atomic corrected position map via
        `WeeklyLineupRepository.submit_correction`.

        `position_changes` is validated as an atomic set of slot changes
        together with the untouched positions: the *final* proposed lineup
        (never an intermediate state) is what `submit_correction` validates
        for duplicate players/legal positions/ownership, so a direct swap
        (e.g. Tackler <-> Interchange, each named in `position_changes`)
        never requires an invalid intermediate duplicate-player state.

        Always passes an ordinary `app.lockouts.LockGuard` to
        `submit_correction` for its `.materialize()` step only (never for
        rejection -- see that method's docstring): this guarantees the
        current effective lineup's lock evidence is durably recorded before
        the correction's own provenance is captured, even if this is the
        very first lineup operation since a trigger activated.
        """
        _ensure_correction_actor(actor)
        unknown = set(position_changes) - set(POSITIONS)
        if unknown:
            raise LineupIntegrityError(f"unknown scoring positions: {sorted(unknown)}")
        lineup_id, _ = self.lineups.get_or_create_header(season_id, competition_id, bbbffl_round_id, season_entry_id)
        current = self.lineups.get_effective_submission(lineup_id)
        if current is None:
            # Issue #151: same team-name-first, id-diagnostic convention as
            # app.carry_forward's NoCarryForwardSourceError.
            raise NoEffectiveSubmissionError(
                f"{team_display_label(self._identities, season_entry_id)} has no effective submitted lineup for "
                f"this round; there is nothing to correct (season_entry_id={season_entry_id}, "
                f"bbbffl_round_id={bbbffl_round_id})"
            )
        corrected_positions = {**current.positions, **position_changes}
        return self.lineups.submit_correction(
            lineup_id,
            corrected_positions,
            expected_submission_version=expected_submission_version,
            actor=actor,
            reason=reason,
            lock_guard=self.lockouts.guard(match_facts=self.match_facts),
        )

    def _describe_correction(self, correction: LineupCorrection) -> dict:
        return {
            "correction_id": correction.correction_id,
            "from_version": correction.from_version,
            "to_version": correction.to_version,
            "actor_type": correction.actor_type,
            "actor_id": correction.actor_id,
            "actor_role": correction.actor_role,
            "reason": correction.reason,
            "created_at": correction.created_at,
            "slots": [
                {
                    "position": slot.position,
                    "previous_season_player_id": slot.previous_season_player_id,
                    "previous_player_display_name": self._player_name(slot.previous_season_player_id),
                    "corrected_season_player_id": slot.corrected_season_player_id,
                    "corrected_player_display_name": self._player_name(slot.corrected_season_player_id),
                    "was_locked": slot.was_locked,
                    "lock_reason": slot.lock_reason,
                    "afl_match_id": slot.afl_match_id,
                    "effective_lock_at": slot.effective_lock_at,
                    "observed_status": slot.observed_status,
                    "locked_at": slot.locked_at,
                }
                for slot in correction.slots
            ],
        }

    def _player_name(self, season_player_id: str | None) -> str | None:
        if season_player_id is None:
            return None
        player = self.pool.get_by_id(season_player_id)
        return player.display_name if player else None

    def _player_club(self, season_player_id: str | None) -> str | None:
        if season_player_id is None:
            return None
        player = self.pool.get_by_id(season_player_id)
        return player.afl_team_name if player else None
