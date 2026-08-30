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

# Display-only grouping of the domain's flat POSITIONS tuple, for the
# desktop lineup layout (issue #90). Purely presentational: it groups
# existing position names, it does not add, remove, reorder or reinterpret
# any of them, so POSITIONS itself and every other consumer of it are
# untouched.
COACH_LINEUP_POSITION_GROUPS = (
    ("Forwards", ("F1", "F2", "F3")),
    ("Midfield", ("M1", "M2", "M3")),
    ("Specialists", ("Ruck", "Tackler")),
    ("Interchange", ("Interchange",)),
)
assert {position for _, group_positions in COACH_LINEUP_POSITION_GROUPS for position in group_positions} == set(
    POSITIONS
), "COACH_LINEUP_POSITION_GROUPS must cover exactly POSITIONS"

# Account-page lineup states (issue #90). These mirror -- and must not
# diverge from -- coach_lineup.html's existing "Lineup state" wording,
# derived from the same draft/submission facts: whether an authoritative
# submission exists and whether the draft revision it was based on is still
# current. `DRAFT_SAVED` additionally uses `LineupDraft.revision` > 1 as the
# authoritative signal that the coach explicitly saved a draft at least once
# -- `ensure_draft` only ever creates revision 1 automatically, and every
# explicit Save/Submit goes through `save()`, which always advances the
# revision -- so this never has to guess or persist a new field.
ACCOUNT_STATE_NOT_SUBMITTED = "not_submitted"
ACCOUNT_STATE_DRAFT_SAVED = "draft_saved"
ACCOUNT_STATE_SUBMITTED = "submitted"
ACCOUNT_STATE_SUBMITTED_WITH_CHANGES = "submitted_with_changes"


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
        """Account-page rounds (issue #90), each carrying a cheap,
        authoritative draft/submission status summary alongside the
        season/round navigation data. Reads only the existing lineup
        repository's persisted draft and submission rows -- no AFL API
        calls, live lockout evaluation, Opening Round preload or
        submission validation, unlike `view()`, which a heavyweight
        lineup-editor page needs and an account summary does not."""
        rows = self.database.execute(
            "SELECT e.season_id, e.season_entry_id, c.competition_id, "
            "r.bbbffl_round_id round_id, r.label round_label, r.sequence "
            "FROM season_entry e JOIN season_entry_coach_history a ON a.season_entry_id=e.season_entry_id "
            "AND a.ended_at IS NULL JOIN competition_stream c ON c.season_id=e.season_id "
            "JOIN bbbffl_round r ON r.competition_id=c.competition_id "
            "WHERE a.coach_id=? AND c.stream_type='ordinary' ORDER BY e.season_id DESC, r.sequence",
            (coach_id,),
        ).fetchall()
        return [self._round_summary(dict(row)) for row in rows]

    def _round_summary(self, row):
        draft = self.lineups.get_draft(row["season_id"], row["competition_id"], row["round_id"], row["season_entry_id"])
        submission = self.lineups.get_effective_submission(draft.lineup_id) if draft else None
        if submission is not None:
            if draft.revision > submission.based_on_draft_revision:
                state = ACCOUNT_STATE_SUBMITTED_WITH_CHANGES
            else:
                state = ACCOUNT_STATE_SUBMITTED
        elif draft is not None and draft.revision > 1:
            state = ACCOUNT_STATE_DRAFT_SAVED
        else:
            state = ACCOUNT_STATE_NOT_SUBMITTED
        return {
            "season_id": row["season_id"],
            "round_id": row["round_id"],
            "round_label": row["round_label"],
            "draft_revision": draft.revision if draft else None,
            "submission_version": submission.version if submission else None,
            "submitted_at": submission.submitted_at if submission else None,
            "submission_based_on_revision": submission.based_on_draft_revision if submission else None,
            "state": state,
        }

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
