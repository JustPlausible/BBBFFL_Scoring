"""Application orchestration for the coach's regular-season lineup page.

The HTTP route deliberately delegates here.  This view service composes the
existing identity, ownership, draft/submission, validation, staged-lockout and
Opening Round boundaries; it does not implement any of their rules.
"""

from dataclasses import dataclass

from app.afl_client import AflApiError
from app.audit import ActorContext
from app.lineup_validation import LineupValidationService, ValidatedLineupSubmissionService
from app.lineups import POSITIONS, LineupConflictError, LineupIntegrityError, WeeklyLineupRepository
from app.lockouts import (
    LockedSelectionError,
    LockoutRepository,
    LockState,
    MatchResolutionError,
    PositionLockState,
    RoundMatchFactsProvider,
)
from app.opening_round import DeferredSlotLockedError, OpeningRoundNominationRepository, OpeningRoundSelectionGuard
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.round_mapping import RoundMappingRepository

COACH_LINEUP_POSITIONS = POSITIONS
EXPECTED_COACH_LINEUP_ERRORS = (
    LineupConflictError,
    LockedSelectionError,
    DeferredSlotLockedError,
    LineupIntegrityError,
    ValueError,
)


@dataclass(frozen=True)
class CoachLineupContext:
    season: dict
    round: dict
    entry: dict
    opponent: str | None
    draft: object
    submission: object | None
    players: list
    selected_players: dict
    locks: dict
    deferred: dict
    validation: object | None


class CoachLineupService:
    def __init__(self, database, afl_client):
        self.database = database
        self.afl_client = afl_client
        self.lineups = WeeklyLineupRepository(database)
        self.pool = PlayerPoolRepository(database)
        self.ownership = OwnershipRepository(database)
        self.nominations = OpeningRoundNominationRepository(database)
        self.lockouts = LockoutRepository(database)
        self.match_facts = RoundMatchFactsProvider(RoundMappingRepository(database), afl_client)

    def list_rounds(self, coach_id):
        rows = self.database.execute(
            "SELECT e.season_id, r.bbbffl_round_id round_id, r.label round_label, r.sequence "
            "FROM season_entry e JOIN season_entry_coach_history a ON a.season_entry_id=e.season_entry_id "
            "AND a.ended_at IS NULL JOIN competition_stream c ON c.season_id=e.season_id "
            "JOIN bbbffl_round r ON r.competition_id=c.competition_id "
            "WHERE a.coach_id=? AND c.stream_type='ordinary' ORDER BY e.season_id DESC, r.sequence",
            (coach_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def resolve(self, coach_id, season_id, round_id):
        row = self.database.execute(
            "SELECT e.season_entry_id, n.team_name, s.label season_label, "
            "r.sequence round_number, r.label round_label, r.competition_id "
            "FROM season_entry e JOIN season_entry_coach_history a ON a.season_entry_id=e.season_entry_id "
            "AND a.ended_at IS NULL JOIN season_entry_team_name_history n ON n.season_entry_id=e.season_entry_id "
            "AND n.ended_at IS NULL JOIN bbbffl_season s ON s.season_id=e.season_id "
            "JOIN bbbffl_round r ON r.competition_id IN "
            "(SELECT competition_id FROM competition_stream WHERE season_id=e.season_id) "
            "WHERE a.coach_id=? AND e.season_id=? AND r.bbbffl_round_id=? "
            "AND r.competition_id IN (SELECT competition_id FROM competition_stream WHERE stream_type='ordinary')",
            (coach_id, season_id, round_id),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def ensure_draft(self, season_id, round_id, entry):
        draft = self.lineups.get_draft(season_id, entry["competition_id"], round_id, entry["season_entry_id"])
        if draft is None:
            draft = self.lineups.save_draft(
                season_id, entry["competition_id"], round_id, entry["season_entry_id"], {}, expected_revision=0
            )
        # #69's service owns preload semantics and is idempotent.
        self.nominations.preload_target_lineup(
            self.lineups, season_id, entry["competition_id"], round_id, entry["season_entry_id"]
        )
        return self.lineups.get_draft(season_id, entry["competition_id"], round_id, entry["season_entry_id"])

    def save(self, season_id, round_id, entry, positions, revision):
        deferred = self.nominations.active_positions(round_id, entry["season_entry_id"])
        positions.update(deferred)  # crafted ordinary edits cannot displace #69 slots
        return self.lineups.save_draft(
            season_id,
            entry["competition_id"],
            round_id,
            entry["season_entry_id"],
            positions,
            expected_revision=revision,
        )

    def submit(self, draft, submission_version, coach_id):
        guard = OpeningRoundSelectionGuard(self.nominations, self.lockouts.guard(match_facts=self.match_facts))
        return ValidatedLineupSubmissionService(self.database, self.afl_client).submit(
            draft.lineup_id,
            expected_draft_revision=draft.revision,
            expected_submission_version=submission_version,
            actor=ActorContext.coach(coach_id),
            source_type="coach",
            lock_guard=guard,
        )

    def view(self, coach_id, season_id, round_id, *, positions=None, validation=None):
        entry = self.resolve(coach_id, season_id, round_id)
        if entry is None:
            return None
        draft = self.ensure_draft(season_id, round_id, entry)
        if positions is not None:
            draft = dataclass_replace_positions(draft, positions)
        submission = self.lineups.get_effective_submission(draft.lineup_id)
        # The selector is a view of current ownership, not ownership when the
        # private draft was last saved. Draft/submission content remains
        # untouched and may therefore truthfully show a now-released player
        # until the coach edits it; submission validation remains authoritative.
        squad = self.ownership.current_squad(entry["season_entry_id"])
        players = [self.pool.get_by_id(period.season_player_id) for period in squad]
        selected_players = {
            position: self.pool.get_by_id(player_id) if player_id else None
            for position, player_id in draft.positions.items()
        }
        deferred = {
            position: self.nominations.deferred_context(round_id, entry["season_entry_id"], position)
            for position in POSITIONS
        }
        deferred = {key: value for key, value in deferred.items() if value}
        try:
            lock_view = self.lockouts.lock_state(
                draft.lineup_id, round_id, entry["season_entry_id"], draft.positions, match_facts=self.match_facts
            )
            locks = lock_view.positions
        except (AflApiError, MatchResolutionError) as exc:
            # A failed evidence read is never presented as confidently editable.
            locks = {
                position: PositionLockState(
                    position,
                    draft.positions[position],
                    LockState.INDETERMINATE,
                    f"lock evidence unavailable: {exc}",
                    None,
                    None,
                    None,
                    False,
                )
                for position in POSITIONS
            }
        if validation is None and submission is not None:
            validation = LineupValidationService(self.database, self.afl_client).validate_submission(
                draft.lineup_id, draft.positions
            )
        opponent = self._opponent(season_id, entry["round_number"], entry["season_entry_id"])
        return CoachLineupContext(
            {"id": season_id, "label": entry["season_label"]},
            {"id": round_id, "number": entry["round_number"], "label": entry["round_label"]},
            entry,
            opponent,
            draft,
            submission,
            players,
            selected_players,
            locks,
            deferred,
            validation,
        )

    def _opponent(self, season_id, number, entry_id):
        row = self.database.execute(
            "SELECT CASE WHEN m.home_season_entry_id=? THEN m.away_season_entry_id ELSE m.home_season_entry_id END opponent "
            "FROM season_fixture_matchup m JOIN season_fixture_draw d ON d.fixture_draw_id=m.fixture_draw_id "
            "WHERE d.season_id=? AND m.bbbffl_round_number=? AND (? IN (m.home_season_entry_id,m.away_season_entry_id))",
            (entry_id, season_id, number, entry_id),
        ).fetchone()
        if not row:
            return None
        team = self.database.execute(
            "SELECT team_name FROM season_entry_team_name_history WHERE season_entry_id=? AND ended_at IS NULL",
            (row["opponent"],),
        ).fetchone()
        return team["team_name"] if team else None


def dataclass_replace_positions(draft, positions):
    from dataclasses import replace

    normalized = {position: positions.get(position) for position in POSITIONS}
    return replace(draft, positions=normalized)
