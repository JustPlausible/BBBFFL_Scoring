"""Issue #56 weekly lineup hard-validation/advisory boundary."""

from contextlib import contextmanager

import pytest

from app.afl_client import AflApiConnectionError, Round, Team
from app.afl_resilience import ResilientAflClient, RetryPolicy
from app.audit import ActorContext
from app.carry_forward import CarryForwardService
from app.lineup_proxy import LineupProxyService
from app.lineup_validation import LineupValidationError, LineupValidationService, ValidatedLineupSubmissionService
from app.lineups import POSITIONS, WeeklyLineupRepository
from app.player_pool import PlayerPoolRepository
from tests.test_carry_forward import context as carry_context
from tests.test_carry_forward import submit_round
from tests.test_lineups import context, save


class AvailabilityFixture:
    def __init__(self, round_, *, stale=False):
        self.round = round_
        self.stale = stale

    def get_rounds(self, season_id):
        return [self.round]

    @contextmanager
    def evidence_batch(self):
        class Batch:
            def is_evidence_fresh(inner_self):
                return not self.stale

        yield Batch()


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
    players[0] = PlayerPoolRepository(db).refresh_player(
        scope["season_id"],
        players[0].canonical_player_id,
        players[0].display_name,
        afl_team_id=bye.team_id,
        afl_team_name=bye.name,
    )
    afl_round = Round(mapping["afl_round_id"], 1, None if unresolved else (bye,))
    return db, round_record, entries, scope, players, foreign, AvailabilityFixture(afl_round, stale=stale)


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


def test_scorer_proxy_cannot_bypass_hard_validation():
    db, round_, entries, scope, players, _, _ = mapped_context()
    proxy = LineupProxyService(db)
    draft = proxy.create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entries[0].season_entry_id,
        {"F1": players[0].season_player_id},
        expected_revision=0,
        actor=ActorContext.anonymous_operator("scorer"),
    )
    with pytest.raises(LineupValidationError):
        proxy.submit(
            draft.lineup_id,
            expected_draft_revision=draft.revision,
            expected_submission_version=0,
            actor=ActorContext.anonymous_operator("scorer"),
            reason="proxy",
        )


def test_carry_forward_cannot_bypass_hard_validation():
    db, _, rounds, entries, scope, pool, ownership = carry_context(rounds=2)
    player = pool.refresh_player(scope["season_id"], 999001, "Only Player")
    ownership.acquire(player.season_player_id, entries[0].season_entry_id)
    submit_round(WeeklyLineupRepository(db), scope, rounds[0], entries[0], {"F1": player.season_player_id})
    with pytest.raises(LineupValidationError):
        CarryForwardService(db).carry_forward(
            scope["season_id"],
            scope["competition_id"],
            rounds[1],
            entries[0].season_entry_id,
            expected_submission_version=0,
            actor=ActorContext.anonymous_operator("scorer"),
        )


def test_validated_submission_still_delegates_to_authoritative_lock_guard():
    db, round_, entries, scope, players, _, afl = mapped_context()
    draft = save(WeeklyLineupRepository(db), round_, entries, scope, full(players))

    def locked(*args):
        raise RuntimeError("locked by staged authority")

    with pytest.raises(RuntimeError, match="staged authority"):
        ValidatedLineupSubmissionService(db, afl).submit(
            draft.lineup_id, expected_draft_revision=1, expected_submission_version=0, lock_guard=locked
        )


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


def test_resilient_cached_round_fallback_is_reported_stale_not_as_current_bye():
    db, round_, entries, scope, players, _, fixture = mapped_context()

    class Transport:
        failed = False

        def get_rounds(self, season_id):
            if self.failed:
                raise AflApiConnectionError("/rounds")
            return [fixture.round]

    transport = Transport()
    resilient = ResilientAflClient(transport, retry_policy=RetryPolicy(max_attempts=1))
    draft = save(WeeklyLineupRepository(db), round_, entries, scope, full(players))
    current = LineupValidationService(db, resilient).validate_submission(draft.lineup_id, draft.positions)
    assert "afl_club_bye" in {message.code for message in current.messages}

    transport.failed = True
    stale = LineupValidationService(db, resilient).validate_submission(draft.lineup_id, draft.positions)
    assert "availability_evidence_stale" in {message.code for message in stale.messages}
    assert "afl_club_bye" not in {message.code for message in stale.messages}
    assert db.execute("SELECT COUNT(*) n FROM slot_dnp").fetchone()["n"] == 0
