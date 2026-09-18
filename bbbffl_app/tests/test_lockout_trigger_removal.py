"""Issue #219: the safe Scorer-facing correction path for an unnecessary,
unactivated lockout trigger -- e.g. a mistakenly-created selective trigger
(`early-1`) alongside a valid `main` trigger, which the domain previously
had no supported way to remove at all (only to revise/retarget via
`LockoutTriggerRepository.replace`/`configure`). Covers the new `remove`
repository primitive, the round-preflight HTTP correction route built on
it, and its effect on Finals-to-SuperScore lockout-plan synchronisation
(`app.finals_superscore_open`) -- Finals remains authoritative; correcting
it must bring the concurrent SS round into agreement automatically,
without the Scorer separately repairing SS's own trigger rows."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext, AuditEventRepository
from app.authorization import Principal, Role
from app.finals_preflight import open_finals_week
from app.finals_superscore_open import (
    LockoutPlanDivergedError,
    open_finals_and_superscore_week,
    synchronise_lockout_plan_from_finals,
)
from app.lockouts import (
    LockoutRepository,
    LockoutTriggerRepository,
    StaleTriggerRevisionError,
    TriggerAlreadyActivatedError,
    TriggerAlreadyRemovedError,
)
from tests.db_helpers import migrated_connection
from tests.test_competition_lifecycle import configured
from tests.test_finals import ACTOR as FINALS_ACTOR
from tests.test_lockouts import STAGE_B_MATCH_ID, STAGE_B_START, FakeMatchFacts, stage_b_match

ACTOR = ActorContext.anonymous_operator("test")


# -- Domain: LockoutTriggerRepository.remove ---------------------------------


def test_an_unactivated_selective_trigger_can_be_removed():
    db = migrated_connection()
    round_, _entries = configured(db, 2601)
    triggers = LockoutTriggerRepository(db)
    triggers.create(round_.bbbffl_round_id, "early-1", "selective", 1, [9001], actor=ACTOR, reason="mistaken early")
    triggers.create(round_.bbbffl_round_id, "main", "main", 2, [9002], actor=ACTOR, reason="main")

    triggers.remove(round_.bbbffl_round_id, "early-1", actor=ACTOR, reason="unnecessary -- no Thursday match")

    remaining = triggers.list_triggers(round_.bbbffl_round_id)
    assert [t.trigger_key for t in remaining] == ["main"]


def test_a_main_only_plan_remains_valid_after_removing_the_selective_trigger():
    db = migrated_connection()
    round_, _entries = configured(db, 2602)
    triggers = LockoutTriggerRepository(db)
    triggers.create(round_.bbbffl_round_id, "early-1", "selective", 1, [9001], actor=ACTOR, reason="mistaken early")
    triggers.create(round_.bbbffl_round_id, "main", "main", 2, [9002], actor=ACTOR, reason="main")
    triggers.remove(round_.bbbffl_round_id, "early-1", actor=ACTOR, reason="unnecessary")

    remaining = triggers.list_triggers(round_.bbbffl_round_id)
    assert len(remaining) == 1
    assert remaining[0].trigger_type == "main"
    assert remaining[0].afl_match_ids == (9002,)
    # No selective trigger appears in the round's active plan at all.
    assert not any(t.trigger_type == "selective" for t in remaining)


def test_removal_requires_a_reason():
    db = migrated_connection()
    round_, _entries = configured(db, 2603)
    triggers = LockoutTriggerRepository(db)
    triggers.create(round_.bbbffl_round_id, "early-1", "selective", 1, [9001], actor=ACTOR, reason="mistaken early")
    with pytest.raises(ValueError, match="reason"):
        triggers.remove(round_.bbbffl_round_id, "early-1", actor=ACTOR, reason="")
    with pytest.raises(ValueError, match="reason"):
        triggers.remove(round_.bbbffl_round_id, "early-1", actor=ACTOR, reason="   ")


def test_removal_of_an_unknown_trigger_key_raises_keyerror():
    db = migrated_connection()
    round_, _entries = configured(db, 2604)
    triggers = LockoutTriggerRepository(db)
    with pytest.raises(KeyError):
        triggers.remove(round_.bbbffl_round_id, "does-not-exist", actor=ACTOR, reason="x")


def test_removing_an_already_removed_trigger_is_rejected_not_a_silent_no_op():
    db = migrated_connection()
    round_, _entries = configured(db, 2605)
    triggers = LockoutTriggerRepository(db)
    triggers.create(round_.bbbffl_round_id, "early-1", "selective", 1, [9001], actor=ACTOR, reason="mistaken early")
    triggers.remove(round_.bbbffl_round_id, "early-1", actor=ACTOR, reason="unnecessary")
    with pytest.raises(TriggerAlreadyRemovedError):
        triggers.remove(round_.bbbffl_round_id, "early-1", actor=ACTOR, reason="again")


def test_removal_rejects_a_stale_revision_without_mutating_the_current_configuration():
    """Codex review (PR #220, P1): a Scorer viewing a stale preflight page
    (revision 1) must never remove a trigger a concurrent operator has
    since reconfigured (revision 2) -- the identical optimistic-concurrency
    guard `configure` already enforces."""
    db = migrated_connection()
    round_, _entries = configured(db, 2615)
    triggers = LockoutTriggerRepository(db)
    triggers.create(round_.bbbffl_round_id, "early-1", "selective", 1, [9001], actor=ACTOR, reason="mistaken early")
    triggers.replace(
        round_.bbbffl_round_id,
        "early-1",
        trigger_type="selective",
        sequence=1,
        afl_match_ids=[9002],
        actor=ACTOR,
        reason="a concurrent operator retargeted it",
    )
    with pytest.raises(StaleTriggerRevisionError):
        triggers.remove(round_.bbbffl_round_id, "early-1", actor=ACTOR, reason="stale removal", expected_revision=1)

    still_active = triggers.get(round_.bbbffl_round_id, "early-1")
    assert still_active.removed_at is None
    assert still_active.afl_match_ids == (9002,), "the newer configuration must survive a stale removal attempt"

    # The matching (current) revision is accepted.
    triggers.remove(round_.bbbffl_round_id, "early-1", actor=ACTOR, reason="now-correct removal", expected_revision=2)
    assert triggers.get(round_.bbbffl_round_id, "early-1").removed_at is not None


def test_removal_after_activation_is_rejected():
    db = migrated_connection()
    round_, _entries = configured(db, 2606)
    triggers = LockoutTriggerRepository(db)
    triggers.create(round_.bbbffl_round_id, "early-1", "selective", 1, [STAGE_B_MATCH_ID], actor=ACTOR, reason="early")
    triggers.create(round_.bbbffl_round_id, "main", "main", 2, [9999], actor=ACTOR, reason="main")

    LockoutRepository(db)._materialize_round_triggers(
        round_.bbbffl_round_id,
        match_facts=FakeMatchFacts([stage_b_match()]),
        evaluation_at=STAGE_B_START,
    )

    with pytest.raises(TriggerAlreadyActivatedError):
        triggers.remove(round_.bbbffl_round_id, "early-1", actor=ACTOR, reason="too late")
    # Never silently frozen out of the active plan either -- still present,
    # unchanged, exactly as `replace`'s identical irreversibility guarantee
    # leaves it.
    assert triggers.get(round_.bbbffl_round_id, "early-1").removed_at is None


def test_removed_trigger_is_excluded_from_list_triggers_but_visible_via_include_removed():
    db = migrated_connection()
    round_, _entries = configured(db, 2607)
    triggers = LockoutTriggerRepository(db)
    triggers.create(round_.bbbffl_round_id, "early-1", "selective", 1, [9001], actor=ACTOR, reason="mistaken early")
    triggers.create(round_.bbbffl_round_id, "main", "main", 2, [9002], actor=ACTOR, reason="main")
    triggers.remove(round_.bbbffl_round_id, "early-1", actor=ACTOR, reason="unnecessary")

    assert [t.trigger_key for t in triggers.list_triggers(round_.bbbffl_round_id)] == ["main"]
    all_including_removed = triggers.list_triggers(round_.bbbffl_round_id, include_removed=True)
    assert {t.trigger_key for t in all_including_removed} == {"early-1", "main"}
    removed = next(t for t in all_including_removed if t.trigger_key == "early-1")
    assert removed.removed_at is not None
    assert removed.removed_reason == "unnecessary"


def test_removed_trigger_never_activates_even_when_its_match_reaches_lock_boundary():
    """Issue #219 acceptance criterion: no selective trigger exists or
    later activates once converted to main-only."""
    db = migrated_connection()
    round_, _entries = configured(db, 2608)
    triggers = LockoutTriggerRepository(db)
    triggers.create(
        round_.bbbffl_round_id, "early-1", "selective", 1, [STAGE_B_MATCH_ID], actor=ACTOR, reason="mistaken early"
    )
    triggers.create(round_.bbbffl_round_id, "main", "main", 2, [9999], actor=ACTOR, reason="main")
    triggers.remove(round_.bbbffl_round_id, "early-1", actor=ACTOR, reason="unnecessary -- no Thursday match")

    # Evaluate well past the removed trigger's own match start -- if it
    # were still part of the active plan, this would activate it.
    LockoutRepository(db)._materialize_round_triggers(
        round_.bbbffl_round_id,
        match_facts=FakeMatchFacts([stage_b_match()]),
        evaluation_at=STAGE_B_START,
    )
    early = triggers.get(round_.bbbffl_round_id, "early-1")
    assert early.removed_at is not None
    activation = db.execute(
        "SELECT 1 FROM bbbffl_round_lockout_trigger_activation WHERE trigger_id=?", (early.trigger_id,)
    ).fetchone()
    assert activation is None, "a removed trigger must never durably activate"


def test_audit_and_revision_history_is_preserved_when_removing_a_trigger():
    """Removal is a header-level fact, exactly like activation -- never a
    delete of the trigger's row or its revision history, and always its
    own audited event distinct from LOCKOUT_TRIGGER_CONFIGURED."""
    db = migrated_connection()
    round_, _entries = configured(db, 2609)
    triggers = LockoutTriggerRepository(db)
    triggers.create(round_.bbbffl_round_id, "early-1", "selective", 1, [9001], actor=ACTOR, reason="mistaken early")
    triggers.replace(
        round_.bbbffl_round_id,
        "early-1",
        trigger_type="selective",
        sequence=1,
        afl_match_ids=[9001, 9003],
        actor=ACTOR,
        reason="broadened coverage",
    )
    removed_revision_before = triggers.get(round_.bbbffl_round_id, "early-1").revision
    triggers.remove(round_.bbbffl_round_id, "early-1", actor=ACTOR, reason="unnecessary -- no Thursday match")

    removed = triggers.list_triggers(round_.bbbffl_round_id, include_removed=True)
    early = next(t for t in removed if t.trigger_key == "early-1")
    # Revision is untouched by removal -- it is still whatever `create`
    # left it at, not bumped or reset.
    assert early.revision == removed_revision_before
    # Every revision this trigger key ever had is still directly readable.
    history = db.execute(
        "SELECT revision, trigger_type, sequence FROM bbbffl_round_lockout_trigger_revision "
        "WHERE trigger_id=? ORDER BY revision",
        (early.trigger_id,),
    ).fetchall()
    assert [row["revision"] for row in history] == [1, 2]

    events = AuditEventRepository(db).list_events()
    trigger_events = [e for e in events if e.entity_id == early.trigger_id]
    assert [e.action for e in trigger_events] == [
        "lockout.trigger.configured",
        "lockout.trigger.configured",
        "lockout.trigger.removed",
    ]
    removal_event = trigger_events[-1]
    assert removal_event.reason == "unnecessary -- no Thursday match"
    assert removal_event.after_state["removed"] is True
    assert removal_event.after_state["afl_match_ids"] == [9001, 9003]


def test_reconfiguring_a_removed_trigger_key_un_removes_it():
    db = migrated_connection()
    round_, _entries = configured(db, 2610)
    triggers = LockoutTriggerRepository(db)
    triggers.create(round_.bbbffl_round_id, "early-1", "selective", 1, [9001], actor=ACTOR, reason="mistaken early")
    triggers.remove(round_.bbbffl_round_id, "early-1", actor=ACTOR, reason="unnecessary")
    assert [t.trigger_key for t in triggers.list_triggers(round_.bbbffl_round_id)] == []

    triggers.configure(
        round_.bbbffl_round_id, "early-1", "selective", 1, [9005], actor=ACTOR, reason="actually needed after all"
    )
    active = triggers.list_triggers(round_.bbbffl_round_id)
    assert [t.trigger_key for t in active] == ["early-1"]
    assert active[0].removed_at is None
    assert active[0].afl_match_ids == (9005,)


# -- HTTP: the round-preflight Scorer-facing correction route ---------------


@pytest.fixture
def preflight_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)
    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _operator(client):
    from app.routes.round_preflight import require_round_operator

    principal = Principal(Role.ADMIN, None, "Admin", session_id=None)
    client.app.dependency_overrides[require_round_operator] = lambda: principal
    return principal


def test_remove_trigger_route_removes_an_unactivated_trigger(preflight_client):
    _operator(preflight_client)
    db = preflight_client.app.state.database
    round_, _entries = configured(db, 2611)
    LockoutTriggerRepository(db).create(
        round_.bbbffl_round_id, "early-1", "selective", 1, [9001], actor=ACTOR, reason="mistaken early"
    )
    LockoutTriggerRepository(db).create(round_.bbbffl_round_id, "main", "main", 2, [9002], actor=ACTOR, reason="main")

    response = preflight_client.post(
        f"/api/admin/round-preflight/{round_.bbbffl_round_id}/lockout-trigger/early-1/remove",
        json={"reason": "unnecessary -- no Thursday match"},
    )
    assert response.status_code == 200, response.text
    triggers = [t["trigger_key"] for t in response.json()["lockout_triggers"]]
    assert triggers == ["main"]


def test_remove_trigger_route_requires_a_reason(preflight_client):
    _operator(preflight_client)
    db = preflight_client.app.state.database
    round_, _entries = configured(db, 2612)
    LockoutTriggerRepository(db).create(
        round_.bbbffl_round_id, "early-1", "selective", 1, [9001], actor=ACTOR, reason="mistaken early"
    )
    response = preflight_client.post(
        f"/api/admin/round-preflight/{round_.bbbffl_round_id}/lockout-trigger/early-1/remove", json={"reason": ""}
    )
    assert response.status_code == 400
    assert "reason" in response.json()["detail"]


def test_remove_trigger_route_rejects_a_stale_revision(preflight_client):
    """Codex review (PR #220, P1): mirrors `configure_trigger`'s own
    `expected_revision` guard, at the HTTP boundary."""
    _operator(preflight_client)
    db = preflight_client.app.state.database
    round_, _entries = configured(db, 2616)
    LockoutTriggerRepository(db).create(
        round_.bbbffl_round_id, "early-1", "selective", 1, [9001], actor=ACTOR, reason="mistaken early"
    )
    LockoutTriggerRepository(db).replace(
        round_.bbbffl_round_id,
        "early-1",
        trigger_type="selective",
        sequence=1,
        afl_match_ids=[9003],
        actor=ACTOR,
        reason="a concurrent operator retargeted it",
    )
    response = preflight_client.post(
        f"/api/admin/round-preflight/{round_.bbbffl_round_id}/lockout-trigger/early-1/remove",
        json={"reason": "stale removal", "expected_revision": 1},
    )
    assert response.status_code == 409
    assert "changed since it was loaded" in response.json()["detail"]
    still_active = LockoutTriggerRepository(db).get(round_.bbbffl_round_id, "early-1")
    assert still_active.removed_at is None
    assert still_active.afl_match_ids == (9003,)


def test_remove_trigger_route_409s_once_activated(preflight_client):
    _operator(preflight_client)
    db = preflight_client.app.state.database
    round_, _entries = configured(db, 2613)
    LockoutTriggerRepository(db).create(
        round_.bbbffl_round_id, "early-1", "selective", 1, [STAGE_B_MATCH_ID], actor=ACTOR, reason="early"
    )
    LockoutRepository(db)._materialize_round_triggers(
        round_.bbbffl_round_id, match_facts=FakeMatchFacts([stage_b_match()]), evaluation_at=STAGE_B_START
    )
    response = preflight_client.post(
        f"/api/admin/round-preflight/{round_.bbbffl_round_id}/lockout-trigger/early-1/remove",
        json={"reason": "too late"},
    )
    assert response.status_code == 409
    assert "activated" in response.json()["detail"]


def test_remove_trigger_route_404s_for_an_unknown_trigger_key(preflight_client):
    _operator(preflight_client)
    db = preflight_client.app.state.database
    round_, _entries = configured(db, 2614)
    response = preflight_client.post(
        f"/api/admin/round-preflight/{round_.bbbffl_round_id}/lockout-trigger/does-not-exist/remove",
        json={"reason": "x"},
    )
    assert response.status_code == 404


# -- Finals-to-SuperScore synchronisation after a pre-activation correction -
#
# Reuses `tests.test_finals_superscore_open`'s own established `_seed`/
# `_StubAflClient`/`KnownRound` fixtures (the same shapes issue #211's
# extensive existing coverage already relies on) rather than hand-rolling a
# second bracket/mapping setup convention.


def test_removing_a_finals_selective_trigger_before_activation_synchronises_ss_to_main_only():
    """The exact issue #219 replay shape: Finals Week 1 has a mistaken
    `early-1` selective trigger alongside a valid `main`; SS's own lockout
    plan was already synchronised from that (now-mistaken) plan. Removing
    `early-1` on Finals and re-synchronising must bring SS into agreement
    automatically -- SS's own `early-1` disappears too, both streams end
    up main-only, and neither exposes a selective trigger that could later
    activate."""
    from app.superscore_round import confirm_afl_mapping, setup_round
    from tests.finals_helpers import KnownRound
    from tests.test_finals_superscore_open import _database_for_test, _seed

    database = _database_for_test(2620)
    built = _seed(database, 2620, with_lockout_triggers=False)
    week1_round_id = built["week1_round_id"]
    ss1_round_id = built["ss1_round_id"]
    afl_round_id = built["afl_round_id"]
    trigger_repo = LockoutTriggerRepository(database)
    trigger_repo.configure(
        week1_round_id, "early-1", "selective", 1, [1111], actor=FINALS_ACTOR, reason="mistaken early match"
    )
    trigger_repo.configure(week1_round_id, "main", "main", 2, [9999], actor=FINALS_ACTOR, reason="main")
    open_finals_week(database, built["bracket"].bracket_id, 1, actor=FINALS_ACTOR)

    validator = KnownRound({(2620, afl_round_id)})
    confirm_afl_mapping(database, validator, ss1_round_id, 2620, afl_round_id, reason="initial SS mapping")
    setup_round(database, ss1_round_id, reason="SS round setup")

    sync1 = synchronise_lockout_plan_from_finals(database, validator, ss1_round_id, actor=FINALS_ACTOR)
    assert set(sync1["synced_trigger_keys"]) == {"early-1", "main"}
    ss_keys_before = {t.trigger_key for t in trigger_repo.list_triggers(ss1_round_id)}
    assert ss_keys_before == {"early-1", "main"}

    # The pre-activation correction: remove the mistaken selective trigger
    # from Finals -- the authoritative side.
    trigger_repo.remove(week1_round_id, "early-1", actor=FINALS_ACTOR, reason="no Thursday match this week")
    assert {t.trigger_key for t in trigger_repo.list_triggers(week1_round_id)} == {"main"}

    sync2 = synchronise_lockout_plan_from_finals(database, validator, ss1_round_id, actor=FINALS_ACTOR)
    assert sync2["removed_trigger_keys"] == ["early-1"]
    assert sync2["changed"] is True

    ss_keys_after = {t.trigger_key for t in trigger_repo.list_triggers(ss1_round_id)}
    assert ss_keys_after == {"main"}, "SS must be brought into agreement automatically -- no manual repair needed"
    finals_keys_after = {t.trigger_key for t in trigger_repo.list_triggers(week1_round_id)}
    assert finals_keys_after == {"main"}

    # Neither stream exposes a selective trigger that could later activate.
    assert not any(t.trigger_type == "selective" for t in trigger_repo.list_triggers(week1_round_id))
    assert not any(t.trigger_type == "selective" for t in trigger_repo.list_triggers(ss1_round_id))

    # SS's own removed row still carries full audit/history -- never a
    # destructive delete either, mirroring the Finals-side guarantee.
    ss_removed = next(
        t for t in trigger_repo.list_triggers(ss1_round_id, include_removed=True) if t.trigger_key == "early-1"
    )
    assert ss_removed.removed_at is not None


def test_paired_open_replay_scenario_opens_both_streams_main_only_after_correction():
    """End-to-end validation target from issue #219: remove the mistaken
    `early-1` before activation, retain only `main`, then open the paired
    Finals+SuperScore week through the supported workflow -- both streams
    must use the corrected main-only plan."""
    from tests.test_finals_superscore_open import _database_for_test, _seed, _StubAflClient

    database = _database_for_test(2621)
    built = _seed(database, 2621, with_lockout_triggers=False)
    week1_round_id = built["week1_round_id"]
    afl_round_id = built["afl_round_id"]
    trigger_repo = LockoutTriggerRepository(database)

    trigger_repo.configure(
        week1_round_id, "early-1", "selective", 1, [1111], actor=FINALS_ACTOR, reason="mistaken early"
    )
    trigger_repo.configure(week1_round_id, "main", "main", 2, [9999], actor=FINALS_ACTOR, reason="main")
    trigger_repo.remove(week1_round_id, "early-1", actor=FINALS_ACTOR, reason="no Thursday match this week")

    result = open_finals_and_superscore_week(
        database, _StubAflClient(afl_round_id), built["bracket"].bracket_id, 1, actor=FINALS_ACTOR
    )
    assert result["finals_state"] == "open"
    assert result["superscore_state"] == "open"
    assert set(result["lockout_sync"]["synced_trigger_keys"]) == {"main"}

    finals_keys = {t.trigger_key for t in trigger_repo.list_triggers(week1_round_id)}
    ss_keys = {t.trigger_key for t in trigger_repo.list_triggers(built["ss1_round_id"])}
    assert finals_keys == {"main"}
    assert ss_keys == {"main"}
    assert not any(t.trigger_type == "selective" for t in trigger_repo.list_triggers(week1_round_id))
    assert not any(t.trigger_type == "selective" for t in trigger_repo.list_triggers(built["ss1_round_id"]))


def test_an_ss_only_trigger_with_no_finals_counterpart_still_fails_closed_alongside_a_finals_removal():
    """The pre-existing "SS carries a trigger key finals never had at all"
    divergence must still fail closed (issue #211) even when a *different*
    key was legitimately removed on Finals and would otherwise be safe to
    auto-mirror -- issue #219's new auto-removal path must never be
    mistaken for a general "reconcile anything different" mechanism."""
    from app.superscore_round import confirm_afl_mapping, setup_round
    from tests.finals_helpers import KnownRound
    from tests.test_finals_superscore_open import _database_for_test, _seed

    database = _database_for_test(2622)
    built = _seed(database, 2622, with_lockout_triggers=False)
    week1_round_id = built["week1_round_id"]
    ss1_round_id = built["ss1_round_id"]
    afl_round_id = built["afl_round_id"]
    trigger_repo = LockoutTriggerRepository(database)
    trigger_repo.configure(
        week1_round_id, "early-1", "selective", 1, [1111], actor=FINALS_ACTOR, reason="mistaken early"
    )
    trigger_repo.configure(week1_round_id, "main", "main", 2, [9999], actor=FINALS_ACTOR, reason="main")
    open_finals_week(database, built["bracket"].bracket_id, 1, actor=FINALS_ACTOR)

    validator = KnownRound({(2622, afl_round_id)})
    confirm_afl_mapping(database, validator, ss1_round_id, 2622, afl_round_id, reason="initial SS mapping")
    setup_round(database, ss1_round_id, reason="SS round setup")
    synchronise_lockout_plan_from_finals(database, validator, ss1_round_id, actor=FINALS_ACTOR)

    trigger_repo.remove(week1_round_id, "early-1", actor=FINALS_ACTOR, reason="no Thursday match this week")
    # An unrelated, genuinely SS-only trigger an operator configured
    # directly -- never existed on Finals at all.
    trigger_repo.configure(
        ss1_round_id, "ss-only", "selective", 0, [7777], actor=FINALS_ACTOR, reason="stale SS-only trigger"
    )

    with pytest.raises(LockoutPlanDivergedError, match="ss-only"):
        synchronise_lockout_plan_from_finals(database, validator, ss1_round_id, actor=FINALS_ACTOR)
