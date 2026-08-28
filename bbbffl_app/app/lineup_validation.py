"""Side-effect-free weekly-lineup validation and AFL availability advice."""

from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Literal

from app.afl_client import AflApiError
from app.lineups import POSITIONS, LineupIntegrityError, WeeklyLineupRepository

Severity = Literal["error", "warning", "unknown"]


@dataclass(frozen=True)
class ValidationMessage:
    severity: Severity
    category: str
    code: str
    position: str | None = None
    season_player_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        return dict(
            severity=self.severity,
            category=self.category,
            code=self.code,
            position=self.position,
            season_player_id=self.season_player_id,
            details=self.details,
        )


@dataclass(frozen=True)
class LineupValidationResult:
    valid: bool
    messages: tuple[ValidationMessage, ...]

    @property
    def errors(self):
        return tuple(m for m in self.messages if m.severity == "error")

    @property
    def warnings(self):
        return tuple(m for m in self.messages if m.severity == "warning")

    def to_dict(self):
        return {"valid": self.valid, "messages": [m.to_dict() for m in self.messages]}


class LineupValidationError(LineupIntegrityError):
    def __init__(self, result):
        super().__init__("lineup failed submission validation")
        self.result = result


class LineupValidationService:
    """Reusable validator; performs no writes and never infers DNP."""

    def __init__(self, database, afl_client=None):
        self.database, self.afl_client = database, afl_client

    def validate_submission(self, lineup_id, positions):
        out = []
        lineup = self.database.execute("SELECT * FROM weekly_lineup WHERE lineup_id=?", (lineup_id,)).fetchone()
        if lineup is None:
            return LineupValidationResult(False, (ValidationMessage("error", "context", "lineup_not_found"),))
        supplied = set(positions)
        for slot in sorted(set(POSITIONS) - supplied):
            out.append(ValidationMessage("error", "positions", "required_position_missing", slot))
        for slot in sorted(supplied - set(POSITIONS)):
            out.append(ValidationMessage("error", "positions", "position_unknown", slot))
        selected = [(slot, positions.get(slot)) for slot in POSITIONS]
        seen = {}
        players = {}
        for slot, player_id in selected:
            if player_id is None:
                out.append(ValidationMessage("error", "positions", "required_position_unfilled", slot))
                continue
            if player_id in seen:
                out.append(
                    ValidationMessage(
                        "error",
                        "identity",
                        "player_selected_multiple_times",
                        slot,
                        player_id,
                        {"first_position": seen[player_id]},
                    )
                )
            seen[player_id] = slot
            if player_id in players:
                continue
            player = self.database.execute(
                "SELECT season_id, afl_team_id, afl_team_name FROM season_player_pool WHERE season_player_id=?",
                (player_id,),
            ).fetchone()
            players[player_id] = player
            if player is None or player["season_id"] != lineup["season_id"]:
                out.append(ValidationMessage("error", "identity", "season_player_invalid", slot, player_id))
                continue
            # Ownership at the submission attempt, from #21's periods. Never
            # infer it from a previous lineup.
            owner = self.database.execute(
                "SELECT season_entry_id FROM player_ownership_period WHERE season_player_id=? AND released_at IS NULL",
                (player_id,),
            ).fetchone()
            if owner is None or owner["season_entry_id"] != lineup["season_entry_id"]:
                out.append(ValidationMessage("error", "ownership", "player_not_owned", slot, player_id))
        scope = self.database.execute(
            "SELECT c.season_id cs, e.season_id es FROM competition_stream c "
            "JOIN bbbffl_round r ON r.competition_id=c.competition_id "
            "JOIN season_entry e ON e.season_entry_id=? WHERE c.competition_id=? AND r.bbbffl_round_id=?",
            (lineup["season_entry_id"], lineup["competition_id"], lineup["bbbffl_round_id"]),
        ).fetchone()
        if scope is None or scope["cs"] != lineup["season_id"] or scope["es"] != lineup["season_id"]:
            out.append(ValidationMessage("error", "context", "season_round_stream_mismatch"))
        self._add_availability(lineup, selected, players, out)
        return LineupValidationResult(not any(m.severity == "error" for m in out), tuple(out))

    def _add_availability(self, lineup, selected, players, out):
        if self.afl_client is None:
            out.append(ValidationMessage("unknown", "availability", "availability_evidence_unavailable"))
            return
        mapping = self.database.execute(
            "SELECT v.afl_season_id, v.afl_round_id FROM round_afl_mapping m "
            "JOIN round_afl_mapping_revision v ON v.mapping_id=m.mapping_id AND v.revision=m.current_revision "
            "WHERE m.bbbffl_round_id=? AND v.state='accepted'",
            (lineup["bbbffl_round_id"],),
        ).fetchone()
        if mapping is None:
            out.append(ValidationMessage("unknown", "availability", "availability_context_unmapped"))
            return
        # ResilientAflClient deliberately keeps freshness out of the cached
        # value. Its evidence batch is therefore the authority for whether
        # this particular read was live or a stale fallback. Plain contract
        # clients/fakes have no batch and a successful response is current.
        batch_context = (
            self.afl_client.evidence_batch() if hasattr(self.afl_client, "evidence_batch") else nullcontext(None)
        )
        try:
            with batch_context as batch:
                round_ = next(
                    (
                        r
                        for r in self.afl_client.get_rounds(mapping["afl_season_id"])
                        if r.round_id == mapping["afl_round_id"]
                    ),
                    None,
                )
        except (AflApiError, LookupError):
            round_ = None
        if round_ is None or round_.byes is None:
            out.append(ValidationMessage("unknown", "availability", "availability_evidence_indeterminate"))
            return
        if batch is not None and not batch.is_evidence_fresh():
            out.append(
                ValidationMessage(
                    "unknown", "availability", "availability_evidence_stale", details={"source": "afl-api-v1"}
                )
            )
            return
        bye_ids = {team.team_id for team in round_.byes}
        for slot, player_id in selected:
            player = players.get(player_id)
            if player is not None and player["afl_team_id"] in bye_ids:
                out.append(
                    ValidationMessage(
                        "warning",
                        "availability",
                        "afl_club_bye",
                        slot,
                        player_id,
                        {"afl_team_id": player["afl_team_id"], "afl_team_name": player["afl_team_name"], "dnp": False},
                    )
                )


@dataclass(frozen=True)
class ValidatedSubmission:
    submission: Any
    validation: LineupValidationResult

    def to_dict(self):
        return {
            "submission": {
                "lineup_id": self.submission.lineup_id,
                "version": self.submission.version,
                "positions": self.submission.positions,
            },
            "validation": self.validation.to_dict(),
        }


class ValidatedLineupSubmissionService:
    """Validates, then delegates persistence and staged locks to #33/#34."""

    def __init__(self, database, afl_client=None):
        self.lineups = WeeklyLineupRepository(database)
        self.validation = LineupValidationService(database, afl_client)

    def submit(self, lineup_id, *, expected_draft_revision, expected_submission_version, lock_guard=None, **kwargs):
        row = self.lineups.database.execute("SELECT * FROM weekly_lineup WHERE lineup_id=?", (lineup_id,)).fetchone()
        if row is None:
            raise LineupValidationError(self.validation.validate_submission(lineup_id, {}))
        draft = self.lineups.get_draft(
            row["season_id"], row["competition_id"], row["bbbffl_round_id"], row["season_entry_id"]
        )
        result = self.validation.validate_submission(lineup_id, draft.positions)
        if not result.valid:
            raise LineupValidationError(result)
        submitted = self.lineups.submit(
            lineup_id,
            expected_draft_revision=expected_draft_revision,
            expected_submission_version=expected_submission_version,
            lock_guard=lock_guard,
            **kwargs,
        )
        return ValidatedSubmission(submitted, result)

    def submit_positions(self, lineup_id, positions, *, expected_submission_version, lock_guard=None, **kwargs):
        """The same boundary for carry-forward/system-derived content."""
        result = self.validation.validate_submission(lineup_id, positions)
        if not result.valid:
            raise LineupValidationError(result)
        submitted = self.lineups.submit_positions(
            lineup_id,
            positions,
            expected_submission_version=expected_submission_version,
            lock_guard=lock_guard,
            **kwargs,
        )
        return ValidatedSubmission(submitted, result)
