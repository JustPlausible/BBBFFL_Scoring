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

# The eight starting-lineup positions, excluding Interchange -- issue #98's
# vacancy-confirmation UX safeguard only prompts about these. Interchange
# itself being unnamed is ordinary/common and not the "did the coach forget
# a starter" risk this exists to catch. This is a display/UX-only grouping
# of the existing POSITIONS tuple (mirrors app.round_review's
# OVERRIDE_POSITIONS), never a change to the domain vacancy rules
# themselves: a vacant Interchange is still recorded and treated exactly
# like any other deliberate vacancy by app.lineup_validation/app.lineups.
ORDINARY_POSITIONS = tuple(position for position in POSITIONS if position != "Interchange")


def resolve_position_locks(lockouts, lineup_id, bbbffl_round_id, season_entry_id, positions, match_facts):
    """Authoritative position-level lock-state read model (issue #138): the
    one boundary both the coach page (`CoachLineupService.view`) and the
    delegated Replay Operator page (`app.routes.delegated_operations.
    _lineup_view`) call, so the two surfaces can never disagree about
    which ordinary positions are editable/locked/indeterminate. Neither
    surface recomputes lock rules itself -- both simply render whatever
    `LockoutRepository.lock_state` (which durably materialises applicable
    trigger/position evidence) reports here.

    Fails closed: if the live evidence read itself errors (afl-api down,
    an unresolved round mapping), every position comes back INDETERMINATE
    rather than confidently editable -- a failed read is never presented
    as safe to edit.
    """
    try:
        return lockouts.lock_state(
            lineup_id, bbbffl_round_id, season_entry_id, positions, match_facts=match_facts
        ).positions
    except (AflApiError, MatchResolutionError) as exc:
        return {
            position: PositionLockState(
                position,
                season_player_id,
                LockState.INDETERMINATE,
                f"lock evidence unavailable: {exc}",
                None,
                None,
                None,
                False,
            )
            for position, season_player_id in positions.items()
        }


_LOCK_REASON_DISPLAY = {
    "empty": "Deliberately vacant",
    "not_yet_triggered": "Not yet triggered -- editable",
    "selective_trigger_activated": "Selective lockout trigger activated",
    "main_lockout_triggered": "Main lockout activated",
    "lockout_plan_not_configured": "No lockout plan configured for this round",
    "missing_scheduled_start_time": "AFL match has no scheduled start time on record",
}


def humanize_lock_reason(reason_code: str) -> str:
    """Operator-readable text for a `PositionLockState.reason` code -- the
    one place both lineup surfaces translate a reason code to prose, so
    the wording never drifts between them (issue #138)."""
    if reason_code in _LOCK_REASON_DISPLAY:
        return _LOCK_REASON_DISPLAY[reason_code]
    if reason_code.startswith("unrecognized_status:"):
        return f"Unrecognised AFL match status ({reason_code.split(':', 1)[1]})"
    if reason_code.startswith("lock evidence unavailable"):
        return reason_code
    return reason_code.replace("_", " ").capitalize()


def describe_ordinary_position(
    position, lock: PositionLockState, deferred_context, player, draft_season_player_id, draft_player
) -> dict:
    """JSON-ready presentation of one ordinary position's authoritative
    lock state (issue #138), for the delegated lineup surface. Player
    names/clubs are resolved here, never left to the browser to guess
    from a season_player_id; `lock_type` keeps an Opening Round deferred
    nomination a visually/semantically distinct category from an
    ordinary selective/main lockout rather than merging them.

    `lock` is the *authoritative* evaluation for this position -- evaluated
    against the effective submission where one exists, so a private draft
    can never make an already-locked position look editable or show the
    wrong (unsubmitted) player as though it were the locked selection
    (issue #138, Codex review on PR #143). `draft_season_player_id` is
    this position's own current private-draft value; for a still-editable
    position the caller passes the same live-evaluated value as `lock`
    itself (there is nothing to diverge from), so `draft_diverges` is only
    ever true for a non-editable position whose private draft holds a
    different, unsubmitted value -- surfaced separately here rather than
    silently overriding or hiding the authoritative lock.
    """
    season_player_id = lock.season_player_id
    if deferred_context:
        lock_type, state, editable = "opening_round_deferred", "locked", False
        reason_code, reason_display = "opening_round_deferred", "Opening Round deferred nomination"
    elif lock.state == LockState.EDITABLE:
        state, editable = "editable", True
        lock_type = "vacant" if season_player_id is None else "editable"
        reason_code, reason_display = lock.reason, humanize_lock_reason(lock.reason)
    elif lock.state == LockState.LOCKED:
        state, editable = "locked", False
        lock_type = {
            "selective_trigger_activated": "selective_trigger",
            "main_lockout_triggered": "main_trigger",
        }.get(lock.reason, "locked")
        reason_code, reason_display = lock.reason, humanize_lock_reason(lock.reason)
    else:
        state, editable, lock_type = "indeterminate", False, "indeterminate"
        reason_code, reason_display = lock.reason, humanize_lock_reason(lock.reason)
    draft_diverges = not editable and draft_season_player_id != season_player_id
    return {
        "position": position,
        "season_player_id": season_player_id,
        "player_display_name": player.display_name if player else None,
        "afl_club_id": player.afl_team_id if player else None,
        "afl_club_name": player.afl_team_name if player else None,
        "state": state,
        "lock_type": lock_type,
        "editable": editable,
        "reason_code": reason_code,
        "reason_display": reason_display,
        "afl_match_id": lock.afl_match_id,
        "effective_lock_at": lock.effective_lock_at,
        "observed_status": lock.observed_status,
        "irreversible": lock.irreversible,
        "deferred_context": deferred_context,
        # The position's own current private-draft value, always -- so the
        # UI can serialise a Save/Submit payload correctly for every
        # position (issue #138, Codex review on PR #143): for an editable
        # position this equals `season_player_id` above; for a locked/
        # indeterminate/deferred one it may legitimately differ from the
        # authoritative value, in which case `draft_diverges` is true and
        # that divergent, unsubmitted value must never be presented or
        # sent as though it were accepted.
        "draft_season_player_id": draft_season_player_id,
        "draft_diverges": draft_diverges,
        "draft_player_display_name": draft_player.display_name if draft_diverges and draft_player else None,
        "draft_afl_club_name": draft_player.afl_team_name if draft_diverges and draft_player else None,
    }


def vacant_ordinary_positions(positions):
    """The ordinary positions in `positions` (a `{position: season_player_id
    or None}` mapping, e.g. a draft's) that are currently vacant -- used
    only to decide whether the coach-facing Submit flow should ask for an
    explicit confirmation before creating an authoritative submitted
    version (issue #98). Never a validation rule: an unconfirmed vacancy is
    not invalid, just unconfirmed."""
    return [position for position in ORDINARY_POSITIONS if positions.get(position) is None]


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
# current. `DRAFT_SAVED` additionally needs a signal that the coach chose at
# least one position themselves -- `LineupDraft.revision` alone is not safe
# for this: `ensure_draft`'s `preload_target_lineup` call (#69) can advance
# the revision on a mere page view, with no coach action at all, whenever
# this round/entry has an active Opening Round nomination. So this compares
# the persisted draft positions against the same Opening Round preload
# baseline `save()`/`preload_target_lineup` already compute, rather than
# guessing from the revision number or persisting a new field.
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
        season/round navigation data. Reads only the existing lineup and
        Opening Round nomination repositories' persisted rows -- no AFL API
        calls, live lockout evaluation, draft/nomination writes (unlike
        `ensure_draft`'s preload) or submission validation, unlike `view()`,
        which a heavyweight lineup-editor page needs and an account summary
        does not."""
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
        elif draft is not None and self._has_coach_chosen_content(row, draft):
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

    def _has_coach_chosen_content(self, row, draft):
        """True only once the persisted draft holds a position the coach
        picked, as opposed to a freshly auto-created empty draft or one an
        Opening Round preload alone has populated. `revision > 1` is not a
        safe proxy for this on its own -- see this module's "Account-page
        lineup states" note above -- so this instead compares the draft's
        positions against exactly what an untouched preload would have set
        (`{}` merged with any active nomination), the same computation
        `save()` and `preload_target_lineup` already perform."""
        if draft.revision <= 1:
            return False
        deferred = self.nominations.active_positions(row["round_id"], row["season_entry_id"])
        baseline = {position: deferred.get(position) for position in POSITIONS}
        return draft.positions != baseline

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

    def save(self, season_id, round_id, entry, positions, revision, coach_id=None):
        deferred = self.nominations.active_positions(round_id, entry["season_entry_id"])
        positions.update(deferred)  # crafted ordinary edits cannot displace #69 slots
        return self.lineups.save_draft(
            season_id,
            entry["competition_id"],
            round_id,
            entry["season_entry_id"],
            positions,
            expected_revision=revision,
            actor=ActorContext.coach(coach_id) if coach_id is not None else None,
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
        deferred = {
            position: self.nominations.deferred_context(round_id, entry["season_entry_id"], position)
            for position in POSITIONS
        }
        deferred = {key: value for key, value in deferred.items() if value}
        # Issue #155 (Codex review, PR #157): immutability must be evaluated
        # against the lineup's *effective submission*, never a private draft
        # that may hold a rejected, never-submitted attempt -- e.g. the
        # coach's own save-then-submit into a position Main has already
        # locked. Evaluating straight off `draft.positions` would otherwise
        # echo that rejected pick back as though it were the authoritative
        # locked value, exactly the class of bug issue #138 already fixed
        # for the delegated surface (`app.routes.delegated_operations.
        # _lineup_view`). A still-editable position keeps reflecting the
        # coach's own current draft pick live -- there is nothing
        # authoritative to defer to yet.
        submitted_positions = submission.positions if submission is not None else None
        authoritative_positions = submitted_positions if submitted_positions is not None else draft.positions
        locks = resolve_position_locks(
            self.lockouts,
            draft.lineup_id,
            round_id,
            entry["season_entry_id"],
            authoritative_positions,
            self.match_facts,
        )
        if submitted_positions is not None and draft.positions != submitted_positions:
            draft_locks = resolve_position_locks(
                self.lockouts, draft.lineup_id, round_id, entry["season_entry_id"], draft.positions, self.match_facts
            )
            locks = {
                **locks,
                **{
                    position: draft_locks[position]
                    for position, lock in locks.items()
                    if lock.state == LockState.EDITABLE and position in draft_locks
                },
            }
        # Built from `locks`, not `draft.positions`, for the same reason:
        # a locked position's displayed occupant must be the authoritative
        # selection, never a divergent, unsubmitted draft value. An
        # editable position's `PositionLockState.season_player_id` already
        # equals the draft's own current pick (see above), so this is a
        # no-op there. Falls back to the draft's own value for a position
        # `locks` has no entry for at all (never true in production --
        # `resolve_position_locks` always covers every position it is
        # given -- but some unit tests stub `lockouts.lock_state` down to
        # an empty read model for concerns unrelated to lockout evaluation).
        selected_players = {}
        for position in POSITIONS:
            lock = locks.get(position)
            player_id = lock.season_player_id if lock is not None else draft.positions.get(position)
            selected_players[position] = self.pool.get_by_id(player_id) if player_id else None
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
