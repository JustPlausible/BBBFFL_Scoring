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


def test_incomplete_draft_saves_and_partial_submission_is_accepted_as_vacant():
    """Issue #98: a formal submission need not name a player in every
    position. A deliberately vacant position is legitimate, authoritative
    state -- persisted explicitly as `None`, reported only as an advisory
    warning, never fabricated and never blocked."""
    db, round_, entries, scope, players, _, afl = mapped_context()
    draft = save(WeeklyLineupRepository(db), round_, entries, scope, {"F1": players[0].season_player_id})
    assert draft.positions["F2"] is None
    result = ValidatedLineupSubmissionService(db, afl).submit(
        draft.lineup_id, expected_draft_revision=1, expected_submission_version=0
    )
    assert result.validation.valid
    assert result.submission.version == 1
    assert result.submission.positions["F1"] == players[0].season_player_id
    vacant_codes = {(m.position, m.code) for m in result.validation.warnings if m.code == "position_vacant"}
    assert vacant_codes == {(position, "position_vacant") for position in POSITIONS if position != "F1"}
    # Nothing was fabricated: every other position is explicitly None, never
    # a placeholder player, DNP ruling or zero-score record.
    assert all(result.submission.positions[position] is None for position in POSITIONS if position != "F1")
    effective = WeeklyLineupRepository(db).get_effective_submission(draft.lineup_id)
    assert effective.version == 1
    assert effective.positions == result.submission.positions


def test_position_missing_from_input_remains_a_hard_error_distinct_from_vacant():
    """A position whose key is entirely absent from the submitted mapping is
    unknown/corrupt input shape (`required_position_missing`), never the
    same thing as a deliberately vacant position (`position_vacant`,
    key present with a `None` value) -- see issue #98's three-way
    vacant/DNP/missing-or-corrupt distinction."""
    db, round_, entries, scope, players, _, afl = mapped_context()
    draft = save(WeeklyLineupRepository(db), round_, entries, scope, {"F1": players[0].season_player_id})
    malformed = dict(draft.positions)
    del malformed["F3"]
    result = LineupValidationService(db, afl).validate_submission(draft.lineup_id, malformed)
    assert not result.valid
    assert {m.code for m in result.errors} == {"required_position_missing"}
    assert next(m for m in result.errors if m.code == "required_position_missing").position == "F3"
    # F2 (present, explicitly None) is still only an advisory vacancy --
    # F3 (key absent entirely) must never also be labelled a vacancy.
    warning_positions = {(m.position, m.code) for m in result.warnings}
    assert ("F2", "position_vacant") in warning_positions
    assert ("F3", "position_vacant") not in warning_positions


def test_scorer_proxy_cannot_bypass_hard_validation():
    db, round_, entries, scope, players, foreign, _ = mapped_context()
    proxy = LineupProxyService(db)
    draft = proxy.create_or_amend(
        scope["season_id"],
        scope["competition_id"],
        round_.bbbffl_round_id,
        entries[0].season_entry_id,
        {"F1": players[0].season_player_id, "F2": foreign.season_player_id},
        expected_revision=0,
        actor=ActorContext.anonymous_operator("scorer"),
    )
    with pytest.raises(LineupValidationError) as caught:
        proxy.submit(
            draft.lineup_id,
            expected_draft_revision=draft.revision,
            expected_submission_version=0,
            actor=ActorContext.anonymous_operator("scorer"),
            reason="proxy",
        )
    assert {m.code for m in caught.value.result.errors} == {"player_not_owned"}


def test_carry_forward_propagates_a_vacant_position_without_fabricating_a_player():
    """Issue #98: carry-forward copies a previous partial submission exactly
    -- including its deliberate vacancy -- rather than requiring (or
    inventing) a full lineup to satisfy validation."""
    db, _, rounds, entries, scope, pool, ownership = carry_context(rounds=2)
    player = pool.refresh_player(scope["season_id"], 999001, "Only Player")
    ownership.acquire(player.season_player_id, entries[0].season_entry_id)
    legacy = WeeklyLineupRepository(db).save_draft(
        scope["season_id"],
        scope["competition_id"],
        rounds[0],
        entries[0].season_entry_id,
        {"F1": player.season_player_id},
        expected_revision=0,
    )
    WeeklyLineupRepository(db).submit(
        legacy.lineup_id, expected_draft_revision=legacy.revision, expected_submission_version=0
    )
    submitted, source = CarryForwardService(db).carry_forward(
        scope["season_id"],
        scope["competition_id"],
        rounds[1],
        entries[0].season_entry_id,
        expected_submission_version=0,
        actor=ActorContext.anonymous_operator("scorer"),
    )
    assert submitted.positions["F1"] == player.season_player_id
    assert all(submitted.positions[position] is None for position in POSITIONS if position != "F1")
    assert source.positions == submitted.positions


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
