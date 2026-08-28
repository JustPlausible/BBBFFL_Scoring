"""Issue #56 weekly lineup hard-validation/advisory boundary."""

import pytest

from app.afl_client import Round, Team
from app.lineup_validation import LineupValidationError, LineupValidationService, ValidatedLineupSubmissionService
from app.lineups import POSITIONS, WeeklyLineupRepository
from tests.test_lineups import context, save


class AvailabilityFixture:
    def __init__(self, round_):
        self.round = round_

    def get_rounds(self, season_id):
        return [self.round]


def full(players):
    return {slot: players[i].season_player_id for i, slot in enumerate(POSITIONS)}


def mapped_context(*, year=2026, stale=False, unresolved=False):
    db, lifecycle, round_record, entries, scope, players, foreign = context(year)
    mapping = db.execute(
        "SELECT v.afl_round_id FROM round_afl_mapping m JOIN round_afl_mapping_revision v "
        "ON v.mapping_id=m.mapping_id AND v.revision=m.current_revision WHERE m.bbbffl_round_id=?",
        (round_record.bbbffl_round_id,),
    ).fetchone()
    bye = Team(77, "Bye Club")
    db.execute(
        "UPDATE season_player_pool SET afl_team_id=?, afl_team_name=? WHERE season_player_id=?",
        (bye.team_id, bye.name, players[0].season_player_id),
    )
    afl_round = Round(mapping["afl_round_id"], 1, None if unresolved else (bye,), "stale" if stale else "current")
    return db, round_record, entries, scope, players, foreign, AvailabilityFixture(afl_round)


def test_valid_owned_nine_player_lineup_submits_with_bye_advice_and_no_mutation():
    db, round_, entries, scope, players, _, afl = mapped_context()
    draft = save(WeeklyLineupRepository(db), round_, entries, scope, full(players))
    before_dnp = db.execute("SELECT COUNT(*) n FROM slot_dnp").fetchone()["n"]
    result = ValidatedLineupSubmissionService(db, afl).submit(
        draft.lineup_id, expected_draft_revision=1, expected_submission_version=0
    )
    assert result.submission.version == 1
    assert result.validation.valid
    assert [m.code for m in result.validation.warnings] == ["afl_club_bye"]
    assert result.validation.warnings[0].details["dnp"] is False
    assert db.execute("SELECT COUNT(*) n FROM slot_dnp").fetchone()["n"] == before_dnp
    # Structured output is directly JSON-compatible at the API boundary.
    assert result.to_dict()["validation"]["messages"][0]["severity"] == "warning"


def test_incomplete_draft_saves_but_submission_validation_rejects_it():
    db, round_, entries, scope, players, _, afl = mapped_context()
    draft = save(WeeklyLineupRepository(db), round_, entries, scope, {"F1": players[0].season_player_id})
    assert draft.positions["F2"] is None
    with pytest.raises(LineupValidationError) as caught:
        ValidatedLineupSubmissionService(db, afl).submit(
            draft.lineup_id, expected_draft_revision=1, expected_submission_version=0
        )
    assert {m.code for m in caught.value.result.errors} == {"required_position_unfilled"}
    assert WeeklyLineupRepository(db).get_effective_submission(draft.lineup_id) is None


def test_duplicate_and_foreign_player_failures_are_structured():
    db, round_, entries, scope, players, foreign, afl = mapped_context()
    draft = save(WeeklyLineupRepository(db), round_, entries, scope, full(players))
    positions = full(players)
    positions["F2"] = positions["F1"]
    positions["F3"] = foreign.season_player_id
    result = LineupValidationService(db, afl).validate_submission(draft.lineup_id, positions)
    assert {m.code for m in result.errors} == {"player_selected_multiple_times", "player_not_owned"}


@pytest.mark.parametrize(
    "stale,unresolved,code",
    [
        (True, False, "availability_evidence_stale"),
        (False, True, "availability_evidence_indeterminate"),
    ],
)
def test_2026_replay_represents_stale_and_unknown_evidence_without_dnp(stale, unresolved, code):
    db, round_, entries, scope, players, _, afl = mapped_context(stale=stale, unresolved=unresolved)
    draft = save(WeeklyLineupRepository(db), round_, entries, scope, full(players))
    result = LineupValidationService(db, afl).validate_submission(draft.lineup_id, draft.positions)
    assert result.valid
    assert code in {message.code for message in result.messages}
    assert db.execute("SELECT COUNT(*) n FROM slot_dnp").fetchone()["n"] == 0
