"""Issue #211 workflow improvement B: `app.finals_superscore_open.
open_finals_and_superscore_week` -- the single paired web "Open week"
action -- and its HTTP surface
(`POST /api/admin/finals/{bracket_id}/weeks/{week_number}/open-paired`).

Coverage proves the paired action:

- validates the pairing (an unmapped/blocked finals week, or a week with no
  configured SuperScore round) fails closed and mutates nothing;
- opens both the finals week and its concurrent SuperScore round, each
  through its own existing lifecycle transition, each recording its own
  separate audit event;
- synchronises SS's lockout plan from the now-opened finals week as part of
  the same action (issue #211 workflow A), so no separate manual step is
  required;
- is idempotent against a pairing that is already (partially) open.
"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.db import transaction
from app.finals import FinalsBracketRepository
from app.finals_preflight import open_finals_week
from app.finals_superscore_open import (
    FrozenMappingDivergedError,
    LockoutPlanDivergedError,
    PairedOpenWeekError,
    open_finals_and_superscore_week,
    synchronise_lockout_plan_from_finals,
)
from app.lockouts import LockoutTriggerRepository, TriggerAlreadyActivatedError
from app.round_mapping import RoundMappingRepository
from app.superscore_round import confirm_afl_mapping, ensure_round, ensure_stream, setup_round
from tests.finals_helpers import KnownRound, accept_week_mapping
from tests.finals_seeding_helpers import build_2026_replay_season

ACTOR = ActorContext.anonymous_operator("test")


class _StubRound:
    def __init__(self, round_id):
        self.round_id = round_id


class _StubMatch:
    def __init__(self, match_id):
        self.match_id = match_id


# Issue #211 P1 (Codex review, round 4): `open_finals_and_superscore_week`
# now validates each finals lockout trigger's configured AFL match IDs
# against this round's real match list before opening -- covers every
# literal match ID this test file's triggers configure by default.
_DEFAULT_STUB_MATCH_IDS = (1111, 1112, 2222, 3333, 7777, 9999)


class _StubAflClient:
    """`round_exists` (used by `AflApiReferenceValidator`, which
    `synchronise_lockout_plan_from_finals` builds internally) must resolve
    the exact `afl_round_id` `_seed` maps the finals week onto -- an empty
    `get_rounds()` would otherwise make every SS mapping confirmation fail
    with "AFL season/round reference does not exist", even though the
    finals week's own mapping was already accepted through a real
    validator (`tests.finals_helpers.accept_week_mapping`).

    `matches_by_round`, when given, overrides the default "every test
    match ID is covered" behaviour with an explicit `{afl_round_id:
    [match_id, ...]}` map -- needed to reproduce a trigger whose match IDs
    are genuinely *not* part of a particular (e.g. newly-corrected) AFL
    round's match list."""

    def __init__(self, afl_round_id=8801, matches_by_round=None):
        self._afl_round_id = afl_round_id
        self._matches_by_round = matches_by_round

    def get_matches(self, afl_round_id):
        if self._matches_by_round is not None:
            return [_StubMatch(match_id) for match_id in self._matches_by_round.get(afl_round_id, [])]
        return [_StubMatch(match_id) for match_id in _DEFAULT_STUB_MATCH_IDS]

    def get_rounds(self, afl_season_id):
        return [_StubRound(self._afl_round_id)]


class _StaleEvidenceBatch:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def is_evidence_fresh(self):
        return False


class _StaleAflClient(_StubAflClient):
    """Issue #211 P1 (Codex review, round 5): a resilient production
    `afl_client` exposes `evidence_batch()`, whose returned context
    manager's `is_evidence_fresh()` reports whether a read taken inside it
    came from a live call or a stale fallback cache -- this stub always
    reports stale, regardless of what `get_matches` itself returns."""

    def evidence_batch(self):
        return _StaleEvidenceBatch()


def _seed(database, year, *, with_ss1=True, with_finals_mapping=True, with_lockout_triggers=True):
    built = build_2026_replay_season(database=database, year=year)
    from app.season import SeasonRepository

    seasons = SeasonRepository(database)
    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (built["season"].season_id,)
    ).fetchone()
    finals_competition = seasons.create_competition(
        built["season"].season_id, rules_row["rules_version_id"], "finals", "Finals", "finals"
    )
    repo = FinalsBracketRepository(database)
    bracket = repo.create_bracket(
        built["season"].season_id,
        finals_competition.competition_id,
        built["competition"].competition_id,
        actor=ACTOR,
        reason="issue #211 paired-open test bracket",
    )["bracket"]
    week1_round_id = repo.get_week_round_id(bracket.bracket_id, 1)
    afl_round_id = 8801
    if with_finals_mapping:
        accept_week_mapping(database, week1_round_id, year=year, afl_round_id=afl_round_id)
        if with_lockout_triggers:
            LockoutTriggerRepository(database).configure(
                week1_round_id, "main", "main", 1, [9999], actor=ACTOR, reason="finals main lockout"
            )

    built["bracket"] = bracket
    built["week1_round_id"] = week1_round_id
    built["afl_round_id"] = afl_round_id

    if with_ss1:
        stream = ensure_stream(
            database, built["season"].season_id, rules_row["rules_version_id"], built["competition"].competition_id
        )
        built["ss1_round_id"] = ensure_round(database, stream.competition_id, 1, 1)
    return built


def test_paired_open_fails_closed_when_no_superscore_round_is_configured():
    built = _seed(_database_for_test(9500), 9500, with_ss1=False)
    with pytest.raises(PairedOpenWeekError, match="no SuperScore round"):
        open_finals_and_superscore_week(
            built["database"], _StubAflClient(), built["bracket"].bracket_id, 1, actor=ACTOR
        )
    lifecycle = CompetitionLifecycleRepository(built["database"])
    assert lifecycle.get_round(built["week1_round_id"]) is None


def test_paired_open_fails_closed_when_finals_preflight_is_blocked():
    built = _seed(_database_for_test(9501), 9501, with_finals_mapping=False)
    with pytest.raises(PairedOpenWeekError, match="not ready to open|failed preflight"):
        open_finals_and_superscore_week(
            built["database"], _StubAflClient(), built["bracket"].bracket_id, 1, actor=ACTOR
        )
    lifecycle = CompetitionLifecycleRepository(built["database"])
    assert lifecycle.get_round(built["week1_round_id"]) is None
    assert lifecycle.get_round(built["ss1_round_id"]) is None


def test_paired_open_opens_both_streams_and_synchronises_the_ss_lockout_plan():
    built = _seed(_database_for_test(9502), 9502)
    result = open_finals_and_superscore_week(
        built["database"], _StubAflClient(), built["bracket"].bracket_id, 1, actor=ACTOR
    )
    assert result["finals_state"] == "open"
    assert result["superscore_state"] == "open"
    assert result["finals_already_open"] is False
    assert result["superscore_already_open"] is False
    assert result["lockout_sync"]["synced_trigger_keys"] == ["main"]

    ss_triggers = LockoutTriggerRepository(built["database"]).list_triggers(built["ss1_round_id"])
    assert [t.trigger_key for t in ss_triggers] == ["main"]

    lifecycle = CompetitionLifecycleRepository(built["database"])
    finals_audit = (
        built["database"]
        .execute("SELECT COUNT(*) AS n FROM audit_event WHERE entity_id=?", (built["week1_round_id"],))
        .fetchone()["n"]
    )
    ss_audit = (
        built["database"]
        .execute("SELECT COUNT(*) AS n FROM audit_event WHERE entity_id=?", (built["ss1_round_id"],))
        .fetchone()["n"]
    )
    assert finals_audit > 0
    assert ss_audit > 0
    assert lifecycle.get_round(built["week1_round_id"]).state == "open"
    assert lifecycle.get_round(built["ss1_round_id"]).state == "open"


def test_paired_open_is_idempotent_once_both_streams_are_already_open():
    built = _seed(_database_for_test(9503), 9503)
    open_finals_and_superscore_week(built["database"], _StubAflClient(), built["bracket"].bracket_id, 1, actor=ACTOR)

    result = open_finals_and_superscore_week(
        built["database"], _StubAflClient(), built["bracket"].bracket_id, 1, actor=ACTOR
    )
    assert result["finals_already_open"] is True
    assert result["superscore_already_open"] is True
    assert result["finals_state"] == "open"
    assert result["superscore_state"] == "open"


def test_paired_open_rejects_a_finals_plan_with_no_main_trigger():
    """Issue #211 P1 (Codex review): `build_finals_week_preflight` never
    requires a configured lockout plan at all -- opening this pairing with
    no main/remaining trigger would synchronise that same absence onto SS,
    silently leaving both streams' selections without a round lockout."""
    built = _seed(_database_for_test(9507), 9507, with_lockout_triggers=False)
    with pytest.raises(PairedOpenWeekError, match="main/remaining lockout trigger"):
        open_finals_and_superscore_week(
            built["database"], _StubAflClient(built["afl_round_id"]), built["bracket"].bracket_id, 1, actor=ACTOR
        )
    lifecycle = CompetitionLifecycleRepository(built["database"])
    assert lifecycle.get_round(built["week1_round_id"]) is None
    assert lifecycle.get_round(built["ss1_round_id"]) is None


def test_paired_open_advances_a_superscore_round_already_set_up_but_still_upcoming():
    """Issue #211 P2 (Codex review): a lifecycle row existing does not mean
    a round is already open -- `app.superscore_round.setup_round` creates
    SS's lifecycle row in `upcoming` state without opening it (only
    `open_round` does). The paired action must still advance it, not
    mistake the pre-existing `upcoming` row for "already open" and leave
    it stuck there forever."""
    database = _database_for_test(9508)
    built = _seed(database, 9508)
    afl_round_id = built["afl_round_id"]
    confirm_afl_mapping(
        database,
        KnownRound({(9508, afl_round_id)}),
        built["ss1_round_id"],
        9508,
        afl_round_id,
        reason="pre-existing SS mapping",
    )
    setup_round(database, built["ss1_round_id"], reason="pre-existing SS setup")

    lifecycle = CompetitionLifecycleRepository(database)
    assert lifecycle.get_round(built["ss1_round_id"]).state == "upcoming"

    result = open_finals_and_superscore_week(
        database, _StubAflClient(afl_round_id), built["bracket"].bracket_id, 1, actor=ACTOR
    )
    assert result["superscore_already_open"] is False
    assert result["superscore_state"] == "open"
    assert lifecycle.get_round(built["ss1_round_id"]).state == "open"


def test_paired_open_fails_closed_when_ss_carries_a_stale_trigger_key_not_in_the_finals_plan():
    """Issue #211 P1 (Codex review): `LockoutTriggerRepository` has no
    delete/deactivate primitive, so a trigger key SS holds that the finals
    plan no longer has can never be silently dropped -- synchronisation
    must fail closed and mutate nothing rather than leave that stale
    trigger active."""
    database = _database_for_test(9509)
    built = _seed(database, 9509)
    afl_round_id = built["afl_round_id"]
    open_finals_and_superscore_week(database, _StubAflClient(afl_round_id), built["bracket"].bracket_id, 1, actor=ACTOR)

    # An operator (or an earlier, since-superseded plan) leaves SS with a
    # trigger key the current finals plan doesn't have. Its sequence (0)
    # stays below the finals-mirrored main trigger's (1) so this setup
    # step itself doesn't trip the selective-precedes-main ordering rule.
    LockoutTriggerRepository(database).configure(
        built["ss1_round_id"], "ss-only", "selective", 0, [7777], actor=ACTOR, reason="stale SS-only trigger"
    )

    with pytest.raises(LockoutPlanDivergedError, match="ss-only"):
        open_finals_and_superscore_week(
            database, _StubAflClient(afl_round_id), built["bracket"].bracket_id, 1, actor=ACTOR
        )


def test_synchronise_lockout_plan_refuses_to_silently_update_only_the_mapping_head_on_a_frozen_divergence():
    """Issue #211 P1 (Codex review, round 2): once SS's round has been set
    up, its AFL mapping is frozen onto its own `bbbffl_round_lifecycle`
    row -- correcting only the mutable `round_afl_mapping` head here would
    never reach that frozen snapshot, which calculations actually
    consume. Synchronisation must fail closed and mutate nothing rather
    than silently report success while SS keeps scoring against a
    different real AFL round than the one its concurrent finals week
    actually uses."""
    database = _database_for_test(9511)
    built = _seed(database, 9511)
    afl_round_id = built["afl_round_id"]
    other_afl_round_id = afl_round_id + 1

    # Finals must actually be opened -- `resolve_concurrent_finals_afl_
    # mapping` only reads the finals week's *frozen* lifecycle mapping,
    # which `open_finals_week` is what creates.
    open_finals_week(database, built["bracket"].bracket_id, 1, actor=ACTOR)

    confirm_afl_mapping(
        database,
        KnownRound({(9511, other_afl_round_id)}),
        built["ss1_round_id"],
        9511,
        other_afl_round_id,
        reason="SS frozen against a different AFL round than finals",
    )
    setup_round(database, built["ss1_round_id"], reason="pre-existing SS setup")

    with pytest.raises(FrozenMappingDivergedError, match="frozen AFL mapping"):
        synchronise_lockout_plan_from_finals(
            database, KnownRound({(9511, afl_round_id)}), built["ss1_round_id"], actor=ACTOR
        )

    # Nothing mutated: SS's mapping head still points at the divergent,
    # already-frozen round -- never silently advanced toward finals'.
    ss_mapping = RoundMappingRepository(database).resolve(built["ss1_round_id"])
    assert ss_mapping.afl_round_id == other_afl_round_id


def test_paired_open_still_rejects_a_missing_main_trigger_when_finals_was_already_opened_standalone():
    """Issue #211 P1 (Codex review, round 2): the main-trigger requirement
    must gate on whether *SuperScore* is about to open, not on whether
    Finals is already open -- otherwise a finals week opened through the
    still-supported standalone `/open` endpoint (bypassing this check
    entirely) lets a retried paired action synchronise an empty lockout
    plan onto SS and still open it."""
    database = _database_for_test(9512)
    built = _seed(database, 9512, with_lockout_triggers=False)
    afl_round_id = built["afl_round_id"]
    open_finals_week(database, built["bracket"].bracket_id, 1, actor=ACTOR)

    with pytest.raises(PairedOpenWeekError, match="main/remaining lockout trigger"):
        open_finals_and_superscore_week(
            database, _StubAflClient(afl_round_id), built["bracket"].bracket_id, 1, actor=ACTOR
        )
    lifecycle = CompetitionLifecycleRepository(database)
    assert lifecycle.get_round(built["ss1_round_id"]) is None


def test_resync_reports_a_genuine_sequence_swap_it_cannot_apply_one_trigger_at_a_time():
    """Issue #211 P1 (Codex review): `LockoutTriggerRepository.configure`'s
    sequence-uniqueness check always compares against every currently-
    persisted trigger for the round, so a genuine two-key sequence swap
    can never be applied one trigger at a time without a transient
    collision. Synchronisation must detect and report this, never proceed
    with a stale/half-applied plan."""
    database = _database_for_test(9510)
    built = _seed(database, 9510, with_lockout_triggers=False)
    afl_round_id = built["afl_round_id"]
    trigger_repo = LockoutTriggerRepository(database)
    trigger_repo.configure(built["week1_round_id"], "early-1", "selective", 1, [1111], actor=ACTOR, reason="e1")
    trigger_repo.configure(built["week1_round_id"], "early-2", "selective", 2, [2222], actor=ACTOR, reason="e2")
    trigger_repo.configure(built["week1_round_id"], "main", "main", 3, [3333], actor=ACTOR, reason="main")

    # SS mirrors the initial plan: early-1=1, early-2=2, main=3.
    open_finals_and_superscore_week(database, _StubAflClient(afl_round_id), built["bracket"].bracket_id, 1, actor=ACTOR)

    # Finals swaps early-1 and early-2's sequences (via a safe vacate/
    # reoccupy sequence on the finals side itself -- this is test setup,
    # not the code under test).
    trigger_repo.configure(built["week1_round_id"], "early-1", "selective", 0, [1111], actor=ACTOR, reason="vacate e1")
    trigger_repo.configure(built["week1_round_id"], "early-2", "selective", 1, [2222], actor=ACTOR, reason="e2 to 1")
    trigger_repo.configure(built["week1_round_id"], "early-1", "selective", 2, [1111], actor=ACTOR, reason="e1 to 2")

    with pytest.raises(LockoutPlanDivergedError, match="cycle"):
        open_finals_and_superscore_week(
            database, _StubAflClient(afl_round_id), built["bracket"].bracket_id, 1, actor=ACTOR
        )


def test_resync_of_a_mixed_plan_with_a_free_move_and_a_genuine_cycle_mutates_nothing():
    """Issue #211 P1 (Codex review, round 2): planning the *entire* move
    graph before writing anything means a plan mixing an independently-
    resolvable free move (main's sequence shifting) with a genuinely
    unresolvable two-key cycle (early-1/early-2 swapping) must mutate
    nothing at all -- not even the free move -- when the cycle makes the
    whole plan unsynchronisable. The previous version applied `main`'s
    move (its own committed `configure()` call) before discovering the
    cycle on a later pass, leaving SS half-synchronised despite the
    fail-closed contract."""
    database = _database_for_test(9513)
    built = _seed(database, 9513, with_lockout_triggers=False)
    afl_round_id = built["afl_round_id"]
    trigger_repo = LockoutTriggerRepository(database)
    trigger_repo.configure(built["week1_round_id"], "early-1", "selective", 1, [1111], actor=ACTOR, reason="e1")
    trigger_repo.configure(built["week1_round_id"], "early-2", "selective", 2, [2222], actor=ACTOR, reason="e2")
    trigger_repo.configure(built["week1_round_id"], "main", "main", 3, [3333], actor=ACTOR, reason="main")

    # SS mirrors the initial plan: early-1=1, early-2=2, main=3.
    open_finals_and_superscore_week(database, _StubAflClient(afl_round_id), built["bracket"].bracket_id, 1, actor=ACTOR)

    # Finals swaps early-1/early-2 (a genuine cycle) *and* independently
    # moves main to a free sequence -- submitted together.
    trigger_repo.configure(built["week1_round_id"], "early-1", "selective", 0, [1111], actor=ACTOR, reason="vacate e1")
    trigger_repo.configure(built["week1_round_id"], "early-2", "selective", 1, [2222], actor=ACTOR, reason="e2 to 1")
    trigger_repo.configure(built["week1_round_id"], "early-1", "selective", 2, [1111], actor=ACTOR, reason="e1 to 2")
    trigger_repo.replace(
        built["week1_round_id"], "main", trigger_type="main", sequence=4, afl_match_ids=[3333], reason="main to 4"
    )

    with pytest.raises(LockoutPlanDivergedError, match="cycle"):
        open_finals_and_superscore_week(
            database, _StubAflClient(afl_round_id), built["bracket"].bracket_id, 1, actor=ACTOR
        )

    # The free move (main's sequence) must not have been applied to SS
    # either, even though it was independently resolvable in isolation --
    # the whole synchronisation is all-or-nothing.
    ss_triggers = {t.trigger_key: t for t in LockoutTriggerRepository(database).list_triggers(built["ss1_round_id"])}
    assert ss_triggers["main"].sequence == 3


def test_resync_applies_a_resolvable_multi_trigger_shift_when_no_cycle_exists():
    """Regression for the `_plan_trigger_sync` refactor (issue #211 P1,
    Codex review, round 2): a plan with no genuine cycle -- a new early
    trigger inserted ahead of an existing main, shifting it out -- must
    still apply successfully in one synchronisation call."""
    database = _database_for_test(9514)
    built = _seed(database, 9514, with_lockout_triggers=False)
    afl_round_id = built["afl_round_id"]
    trigger_repo = LockoutTriggerRepository(database)
    trigger_repo.configure(built["week1_round_id"], "main", "main", 1, [3333], actor=ACTOR, reason="main")

    open_finals_and_superscore_week(database, _StubAflClient(afl_round_id), built["bracket"].bracket_id, 1, actor=ACTOR)
    ss_triggers = {
        t.trigger_key: t.sequence for t in LockoutTriggerRepository(database).list_triggers(built["ss1_round_id"])
    }
    assert ss_triggers == {"main": 1}

    # Finals inserts a new early trigger ahead of main, shifting main out
    # to a later sequence -- via a safe vacate/reoccupy on the finals side.
    trigger_repo.configure(built["week1_round_id"], "main", "main", 99, [3333], actor=ACTOR, reason="vacate main")
    trigger_repo.configure(
        built["week1_round_id"], "early-1", "selective", 1, [1111], actor=ACTOR, reason="insert early-1"
    )
    trigger_repo.configure(built["week1_round_id"], "main", "main", 2, [3333], actor=ACTOR, reason="main to 2")

    result = synchronise_lockout_plan_from_finals(
        database, KnownRound({(9514, afl_round_id)}), built["ss1_round_id"], actor=ACTOR
    )
    assert set(result["synced_trigger_keys"]) == {"early-1", "main"}

    ss_triggers = {
        t.trigger_key: t.sequence for t in LockoutTriggerRepository(database).list_triggers(built["ss1_round_id"])
    }
    assert ss_triggers == {"main": 2, "early-1": 1}


def test_resync_reorders_around_the_selective_precedes_main_rule_not_just_sequence_occupancy():
    """Issue #211 P1 (Codex review, round 3): the planner must also model
    `LockoutTriggerRepository.configure`'s selective-precedes-main
    ordering rule, not just bare sequence occupancy. SS starts at
    s1=1,s2=2,main=3; Finals validly moves to s1=1 (new match coverage,
    same sequence), s2=4, main=5 -- applying s2's move before main's would
    be rejected by the real repository (s2's new sequence 4 would sit at
    or past main's still-current sequence 3), even though the planner's
    occupancy check alone sees sequence 4 as free."""
    database = _database_for_test(9515)
    built = _seed(database, 9515, with_lockout_triggers=False)
    afl_round_id = built["afl_round_id"]
    trigger_repo = LockoutTriggerRepository(database)
    trigger_repo.configure(built["week1_round_id"], "s1", "selective", 1, [1111], actor=ACTOR, reason="s1")
    trigger_repo.configure(built["week1_round_id"], "s2", "selective", 2, [2222], actor=ACTOR, reason="s2")
    trigger_repo.configure(built["week1_round_id"], "main", "main", 3, [3333], actor=ACTOR, reason="main")

    open_finals_and_superscore_week(database, _StubAflClient(afl_round_id), built["bracket"].bracket_id, 1, actor=ACTOR)
    ss_triggers = {
        t.trigger_key: t.sequence for t in LockoutTriggerRepository(database).list_triggers(built["ss1_round_id"])
    }
    assert ss_triggers == {"s1": 1, "s2": 2, "main": 3}

    # Finals moves main out of the way first, then s2 past its old
    # position, then main to its final sequence -- all valid, stepwise,
    # real writes on the finals side (test setup, not the code under test).
    trigger_repo.configure(built["week1_round_id"], "main", "main", 99, [3333], actor=ACTOR, reason="vacate main")
    trigger_repo.configure(built["week1_round_id"], "s2", "selective", 4, [2222], actor=ACTOR, reason="s2 to 4")
    trigger_repo.configure(built["week1_round_id"], "s1", "selective", 1, [1112], actor=ACTOR, reason="s1 new match")
    trigger_repo.configure(built["week1_round_id"], "main", "main", 5, [3333], actor=ACTOR, reason="main to 5")

    result = synchronise_lockout_plan_from_finals(
        database, KnownRound({(9515, afl_round_id)}), built["ss1_round_id"], actor=ACTOR
    )
    assert set(result["synced_trigger_keys"]) == {"s1", "s2", "main"}

    ss_triggers = {
        t.trigger_key: t.sequence for t in LockoutTriggerRepository(database).list_triggers(built["ss1_round_id"])
    }
    assert ss_triggers == {"s1": 1, "s2": 4, "main": 5}


def test_synchronise_lockout_plan_leaves_the_mapping_untouched_when_the_trigger_plan_cannot_be_validated():
    """Issue #211 P2 (Codex review, round 3): `LockoutPlanDivergedError`
    must mutate nothing at all, including the mapping -- validating the
    trigger plan before calling `confirm_afl_mapping` (not after) means a
    stale SS-only trigger key still blocks the whole synchronisation
    before the mapping head is ever touched."""
    database = _database_for_test(9516)
    built = _seed(database, 9516)
    afl_round_id = built["afl_round_id"]
    other_afl_round_id = afl_round_id + 1
    open_finals_week(database, built["bracket"].bracket_id, 1, actor=ACTOR)

    # SS has its own, different (unfrozen -- no `setup_round` yet) mapping,
    # and a stale trigger key the finals plan doesn't have.
    confirm_afl_mapping(
        database,
        KnownRound({(9516, other_afl_round_id)}),
        built["ss1_round_id"],
        9516,
        other_afl_round_id,
        reason="SS's own, different mapping",
    )
    LockoutTriggerRepository(database).configure(
        built["ss1_round_id"], "ss-only", "selective", 0, [7777], actor=ACTOR, reason="stale SS-only trigger"
    )

    with pytest.raises(LockoutPlanDivergedError, match="ss-only"):
        synchronise_lockout_plan_from_finals(
            database, KnownRound({(9516, afl_round_id), (9516, other_afl_round_id)}), built["ss1_round_id"], actor=ACTOR
        )

    # The mapping must still read SS's original, unsynchronised value --
    # never silently advanced toward finals' despite the overall failure.
    ss_mapping = RoundMappingRepository(database).resolve(built["ss1_round_id"])
    assert ss_mapping.afl_round_id == other_afl_round_id


def test_synchronise_lockout_plan_refuses_to_revise_an_already_activated_ss_trigger():
    """Issue #211 P1 (Codex review, round 4): `LockoutTriggerRepository.
    configure()` permanently refuses to revise a trigger that has already
    durably activated -- checked only at write time. Without preflighting
    this, a plan with several pending changes could commit an earlier,
    still-editable trigger before discovering a later one has already
    irreversibly locked, leaving SS half-synchronised despite the
    fail-closed contract. Must mutate nothing."""
    database = _database_for_test(9517)
    built = _seed(database, 9517, with_lockout_triggers=False)
    afl_round_id = built["afl_round_id"]
    trigger_repo = LockoutTriggerRepository(database)
    trigger_repo.configure(built["week1_round_id"], "s1", "selective", 1, [1111], actor=ACTOR, reason="s1")
    trigger_repo.configure(built["week1_round_id"], "main", "main", 2, [9999], actor=ACTOR, reason="main")

    open_finals_and_superscore_week(database, _StubAflClient(afl_round_id), built["bracket"].bracket_id, 1, actor=ACTOR)
    ss_triggers = {t.trigger_key: t for t in trigger_repo.list_triggers(built["ss1_round_id"])}
    assert set(ss_triggers) == {"s1", "main"}

    # SS's "s1" trigger has already durably activated -- test setup (a
    # direct row insert mirroring what `app.lockouts.LockoutRepository.
    # _materialize_round_triggers` would itself have written), not the
    # code under test.
    with transaction(database) as conn:
        conn.execute(
            "INSERT INTO bbbffl_round_lockout_trigger_activation "
            "(trigger_id, revision, afl_match_id, observed_status, effective_lock_at, activation_reason, "
            "evaluated_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ss_triggers["s1"].trigger_id,
                ss_triggers["s1"].revision,
                1111,
                "LIVE",
                "2026-01-01T00:00:00+00:00",
                "match_status_live",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    # Finals changes "s1"'s match coverage -- SS can no longer safely
    # mirror this since its own "s1" is already irreversibly locked.
    trigger_repo.configure(built["week1_round_id"], "s1", "selective", 1, [1112], actor=ACTOR, reason="s1 changed")

    with pytest.raises(LockoutPlanDivergedError, match="s1"):
        synchronise_lockout_plan_from_finals(
            database, KnownRound({(9517, afl_round_id)}), built["ss1_round_id"], actor=ACTOR
        )

    # Nothing mutated: SS's "s1" trigger still has its original match ids.
    unchanged = trigger_repo.get(built["ss1_round_id"], "s1")
    assert unchanged.afl_match_ids == (1111,)


def test_paired_open_rejects_a_finals_trigger_whose_match_ids_are_not_in_the_currently_mapped_round():
    """Issue #211 P1 (Codex review, round 4): a main trigger merely
    *existing* is not enough -- if finals' mapping was corrected to a
    different AFL round after its triggers were configured against the
    old one (the supported pre-open `RoundMappingRepository.correct`
    path), those triggers' match IDs can reference a round they no longer
    belong to and can never activate on either stream. Must fail closed
    and mutate nothing."""
    database = _database_for_test(9518)
    built = _seed(database, 9518)
    afl_round_id = built["afl_round_id"]
    other_afl_round_id = afl_round_id + 1

    RoundMappingRepository(database).correct(
        built["week1_round_id"],
        9518,
        other_afl_round_id,
        KnownRound({(9518, afl_round_id), (9518, other_afl_round_id)}),
        reason="finals week remapped to a different AFL round",
    )

    stub = _StubAflClient(other_afl_round_id, matches_by_round={afl_round_id: [9999], other_afl_round_id: [5555]})
    with pytest.raises(PairedOpenWeekError, match="match"):
        open_finals_and_superscore_week(database, stub, built["bracket"].bracket_id, 1, actor=ACTOR)

    lifecycle = CompetitionLifecycleRepository(database)
    assert lifecycle.get_round(built["week1_round_id"]) is None
    assert lifecycle.get_round(built["ss1_round_id"]) is None


def test_paired_open_rejects_stale_cached_match_evidence_when_validating_trigger_coverage():
    """Issue #211 P1 (Codex review, round 5): a resilient production
    `afl_client` can serve `get_matches` from its own last-known-good
    cache during a live AFL-API outage, returning successfully rather
    than raising -- validating trigger match coverage against that stale
    a read could wrongly approve match IDs no longer actually in the
    mapped round's real fixture. Must fail closed on stale evidence, the
    same way `app.round_preflight.configure_preflight_trigger` already
    does for its own membership check."""
    database = _database_for_test(9521)
    built = _seed(database, 9521)
    afl_round_id = built["afl_round_id"]

    stub = _StaleAflClient(afl_round_id)
    with pytest.raises(PairedOpenWeekError, match="stale cache"):
        open_finals_and_superscore_week(database, stub, built["bracket"].bracket_id, 1, actor=ACTOR)

    lifecycle = CompetitionLifecycleRepository(database)
    assert lifecycle.get_round(built["week1_round_id"]) is None
    assert lifecycle.get_round(built["ss1_round_id"]) is None


def test_apply_trigger_sync_rolls_back_the_whole_plan_on_a_concurrent_activation_mid_loop():
    """Issue #211 P1 (Codex review, round 6): a live lockout evaluation can
    activate a still-pending SS trigger in the narrow window between
    `_validate_trigger_sync_plan`'s own activation read and this loop's
    write for it -- `LockoutTriggerRepository._configure_locked` itself
    then raises `TriggerAlreadyActivatedError`. Round 5 translated this
    into `LockoutPlanDivergedError` but left whatever had already been
    written independently committed; round 6 correctly rejected that --
    the newly-activated trigger's configuration is now permanently
    frozen and can never converge with finals' current plan, so that
    "partial" result was not actually recoverable by retrying. Every
    write in the same call must now share one transaction: the whole
    plan rolls back together, translated into the same
    `LockoutPlanDivergedError` every other unresolvable divergence
    already raises."""
    from app.finals_superscore_open import _apply_trigger_sync, _validate_trigger_sync_plan

    database = _database_for_test(9522)
    built = _seed(database, 9522, with_lockout_triggers=False)
    afl_round_id = built["afl_round_id"]
    trigger_repo = LockoutTriggerRepository(database)
    trigger_repo.configure(built["week1_round_id"], "s1", "selective", 1, [1111], actor=ACTOR, reason="s1")
    trigger_repo.configure(built["week1_round_id"], "main", "main", 2, [9999], actor=ACTOR, reason="main")

    open_finals_and_superscore_week(database, _StubAflClient(afl_round_id), built["bracket"].bracket_id, 1, actor=ACTOR)
    ss_triggers_by_key = {t.trigger_key: t for t in trigger_repo.list_triggers(built["ss1_round_id"])}

    # Finals changes both triggers' match coverage -- neither has
    # activated on SS yet, and neither needs a sequence change.
    trigger_repo.configure(built["week1_round_id"], "s1", "selective", 1, [1112], actor=ACTOR, reason="s1 changed")
    trigger_repo.configure(built["week1_round_id"], "main", "main", 2, [8888], actor=ACTOR, reason="main changed")
    finals_triggers = trigger_repo.list_triggers(built["week1_round_id"])

    ordered_plan, _unchanged, _removed = _validate_trigger_sync_plan(
        built["ss1_round_id"], finals_triggers, ss_triggers_by_key, set()
    )
    assert [t.trigger_key for t in ordered_plan] == ["s1", "main"]

    # Simulate a concurrent lockout evaluation activating "main" *after*
    # validation already ran but *before* this loop reaches its own write
    # for it -- test setup (a direct row insert), not the code under test.
    with transaction(database) as conn:
        conn.execute(
            "INSERT INTO bbbffl_round_lockout_trigger_activation "
            "(trigger_id, revision, afl_match_id, observed_status, effective_lock_at, activation_reason, "
            "evaluated_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ss_triggers_by_key["main"].trigger_id,
                ss_triggers_by_key["main"].revision,
                9999,
                "LIVE",
                "2026-01-01T00:00:00+00:00",
                "match_status_live",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    with pytest.raises(TriggerAlreadyActivatedError):
        with transaction(database) as conn:
            _apply_trigger_sync(
                conn, trigger_repo, built["ss1_round_id"], ordered_plan, ss_triggers_by_key, 1, ACTOR, "resync"
            )

    # Nothing was applied: "s1" (ordered before "main") was rolled back
    # together with "main", even though its own write would otherwise
    # have succeeded on its own.
    s1 = trigger_repo.get(built["ss1_round_id"], "s1")
    assert s1.afl_match_ids == (1111,)
    main = trigger_repo.get(built["ss1_round_id"], "main")
    assert main.afl_match_ids == (9999,)


def test_synchronise_lockout_plan_locked_recheck_catches_a_trigger_added_after_the_prechecks_read(monkeypatch):
    """Issue #211 P1 (Codex review, round 7): `synchronise_lockout_plan_
    from_finals` runs an unlocked pre-check (purely a fast-fail before
    `confirm_afl_mapping` mutates the mapping) before the real, locked
    decision `_synchronise_triggers_locked` makes. A genuinely concurrent
    trigger change landing in the narrow window right after that
    pre-check's own read must still be caught by the fresh, locked
    re-read/re-validation -- not silently missed because the pre-check's
    now-stale snapshot was reused for the actual write decision."""
    database = _database_for_test(9523)
    built = _seed(database, 9523)
    afl_round_id = built["afl_round_id"]
    ss_round_id = built["ss1_round_id"]
    open_finals_week(database, built["bracket"].bracket_id, 1, actor=ACTOR)

    real_list_triggers = LockoutTriggerRepository.list_triggers
    calls = {"ss_reads": 0}

    def _spy(self, bbbffl_round_id, *, include_removed=False):
        result = real_list_triggers(self, bbbffl_round_id, include_removed=include_removed)
        if bbbffl_round_id == ss_round_id:
            calls["ss_reads"] += 1
            if calls["ss_reads"] == 1:
                # Simulate a concurrent operator adding a stale SS-only
                # trigger right after the pre-check's own read returns --
                # test setup, not the code under test.
                self.configure(
                    ss_round_id, "concurrent-stale", "selective", 0, [7777], actor=ACTOR, reason="concurrent change"
                )
        return result

    monkeypatch.setattr(LockoutTriggerRepository, "list_triggers", _spy)

    with pytest.raises(LockoutPlanDivergedError, match="concurrent-stale"):
        synchronise_lockout_plan_from_finals(database, KnownRound({(9523, afl_round_id)}), ss_round_id, actor=ACTOR)


def test_synchronise_lockout_plan_locked_recheck_uses_the_fresh_finals_trigger_state_not_the_prechecks_stale_one(
    monkeypatch,
):
    """Issue #211 P1 (Codex review, round 8): round 7 only guaranteed SS's
    own trigger read was fresh under lock -- a finals trigger revised in
    the same narrow window (via `app.round_preflight.
    configure_preflight_trigger`, itself layered over the same
    `LockoutTriggerRepository.configure`) must be reflected too, not
    silently missed because the pre-check's earlier, unlocked read of
    *finals'* trigger set was reused for the actual write decision."""
    database = _database_for_test(9524)
    built = _seed(database, 9524, with_lockout_triggers=False)
    afl_round_id = built["afl_round_id"]
    ss_round_id = built["ss1_round_id"]
    finals_round_id = built["week1_round_id"]
    LockoutTriggerRepository(database).configure(finals_round_id, "main", "main", 1, [9999], actor=ACTOR, reason="main")
    open_finals_week(database, built["bracket"].bracket_id, 1, actor=ACTOR)

    real_list_triggers = LockoutTriggerRepository.list_triggers
    calls = {"finals_reads": 0}

    def _spy(self, bbbffl_round_id, *, include_removed=False):
        result = real_list_triggers(self, bbbffl_round_id, include_removed=include_removed)
        if bbbffl_round_id == finals_round_id:
            calls["finals_reads"] += 1
            if calls["finals_reads"] == 1:
                # Simulate a concurrent scorer revising finals' main
                # trigger right after the pre-check's own read returns --
                # test setup, not the code under test.
                self.configure(
                    finals_round_id, "main", "main", 1, [8888], actor=ACTOR, reason="concurrent finals revision"
                )
        return result

    monkeypatch.setattr(LockoutTriggerRepository, "list_triggers", _spy)

    result = synchronise_lockout_plan_from_finals(
        database, KnownRound({(9524, afl_round_id)}), ss_round_id, actor=ACTOR
    )
    assert result["synced_trigger_keys"] == ["main"]

    ss_main = LockoutTriggerRepository(database).get(ss_round_id, "main")
    assert ss_main.afl_match_ids == (8888,)


def test_open_paired_route_returns_409_not_500_for_a_diverged_lockout_plan(finals_client):
    """Issue #211 P2 (Codex review, round 2): `LockoutPlanDivergedError`
    reaching this route must translate to the same 409 every other
    paired-open conflict returns, never an uncaught 500."""
    database = finals_client.app.state.database
    built = _seed(database, 9506)
    finals_client.app.state.afl_client = _StubAflClient(built["afl_round_id"])
    finals_client.post(f"/api/admin/finals/{built['bracket'].bracket_id}/weeks/1/open-paired")

    LockoutTriggerRepository(database).configure(
        built["ss1_round_id"], "ss-only", "selective", 0, [7777], actor=ACTOR, reason="stale SS-only trigger"
    )

    response = finals_client.post(f"/api/admin/finals/{built['bracket'].bracket_id}/weeks/1/open-paired")
    assert response.status_code == 409, response.text


@pytest.fixture
def finals_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)
    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def test_open_paired_route_end_to_end_opens_both_streams(finals_client):
    built = _seed(finals_client.app.state.database, 9504)
    finals_client.app.state.afl_client = _StubAflClient(built["afl_round_id"])
    response = finals_client.post(f"/api/admin/finals/{built['bracket'].bracket_id}/weeks/1/open-paired")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["finals_state"] == "open"
    assert body["superscore_state"] == "open"
    assert body["superscore_round_id"] == built["ss1_round_id"]


def test_open_paired_route_409s_without_a_superscore_round(finals_client):
    built = _seed(finals_client.app.state.database, 9505, with_ss1=False)
    response = finals_client.post(f"/api/admin/finals/{built['bracket'].bracket_id}/weeks/1/open-paired")
    assert response.status_code == 409


def _database_for_test(year):
    """A standalone SQLite database for the pure service-level tests above
    (no TestClient/app needed) -- mirrors `tests.db_helpers.migrated_
    connection`'s shape but through the same `app.db.connect` path
    `tests.finals_seeding_helpers.build_2026_replay_season` expects."""
    from tests.db_helpers import migrated_connection

    return migrated_connection()
