"""Issue #211 workflow improvement A: SS1-SS4 must be able to derive/
synchronise their AFL mapping and lockout-trigger plan from the exact
concurrent finals week, rather than a Scorer manually duplicating the
identical configuration onto SuperScore a second time (the confirmed
replay defect: SS1 failed closed with `lockout_plan_not_configured` until
an operator hand-copied the finals week's own plan onto it).

Coverage here proves `app.superscore_round.synchronise_lockout_plan_from_
finals`:

- derives SS's own AFL mapping and lockout triggers from the concurrent
  finals week's current plan, persisting them onto SS's *own* rows;
- is idempotent (a second, unchanged sync writes nothing new);
- re-synchronising after only *some* finals triggers changed updates only
  those SS trigger keys, leaving the others (and their revisions) alone;
- never touches the finals week's own rows -- the two streams keep fully
  separate persisted trigger/revision rows throughout.
"""

from app.audit import ActorContext
from app.finals import FinalsBracketRepository
from app.finals_preflight import open_finals_week
from app.finals_superscore_open import synchronise_lockout_plan_from_finals
from app.lockouts import LockoutTriggerRepository
from app.round_mapping import RoundMappingRepository
from app.superscore_round import ensure_round, ensure_stream
from tests.finals_helpers import KnownRound, accept_week_mapping, build_finals_ready_season

ACTOR = ActorContext.anonymous_operator("test")


def _seed(year, afl_round_id=9001, early_match_id=8213, main_match_id=8218):
    built = build_finals_ready_season(year=year)
    database = built["database"]
    bracket_repo = FinalsBracketRepository(database)
    bracket = bracket_repo.create_bracket(
        built["season"].season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ACTOR,
        reason="issue #211 lockout-sync test bracket",
    )["bracket"]
    week1_round_id = bracket_repo.get_week_round_id(bracket.bracket_id, 1)
    accept_week_mapping(database, week1_round_id, year=year, afl_round_id=afl_round_id)
    open_finals_week(database, bracket.bracket_id, 1, actor=ACTOR)

    trigger_repo = LockoutTriggerRepository(database)
    trigger_repo.configure(
        week1_round_id, "early-1", "selective", 1, [early_match_id], actor=ACTOR, reason="finals early lockout"
    )
    trigger_repo.configure(
        week1_round_id, "main", "main", 2, [main_match_id], actor=ACTOR, reason="finals main lockout"
    )

    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (built["season"].season_id,)
    ).fetchone()
    stream = ensure_stream(
        database, built["season"].season_id, rules_row["rules_version_id"], built["ordinary_competition_id"]
    )
    # Deliberately *not* `setup_round` here -- `app.superscore_round.
    # setup_round` (via `create_non_ordinary_round`) requires an accepted
    # AFL mapping to already exist before it can create the round's
    # lifecycle row, so a Scorer reaches this synchronisation step before
    # setup, exactly to avoid manually accepting that mapping by hand.
    ss1_round_id = ensure_round(database, stream.competition_id, 1, 1)

    validator = KnownRound({(year, afl_round_id)})
    built["bracket"] = bracket
    built["week1_round_id"] = week1_round_id
    built["ss1_round_id"] = ss1_round_id
    built["validator"] = validator
    built["trigger_repo"] = trigger_repo
    return built


def test_synchronise_derives_mapping_and_triggers_from_the_concurrent_finals_week():
    built = _seed(8300)
    database, ss1_round_id, week1_round_id = built["database"], built["ss1_round_id"], built["week1_round_id"]

    # Before sync: SS1 has no mapping and no triggers at all.
    assert RoundMappingRepository(database).resolve(ss1_round_id) is None
    assert built["trigger_repo"].list_triggers(ss1_round_id) == []

    result = synchronise_lockout_plan_from_finals(database, built["validator"], ss1_round_id, actor=ACTOR)
    assert result["finals_round_id"] == week1_round_id
    assert result["mapping_synced"] is True
    assert sorted(result["synced_trigger_keys"]) == ["early-1", "main"]
    assert result["unchanged_trigger_keys"] == []
    assert result["changed"] is True

    ss_mapping = RoundMappingRepository(database).resolve(ss1_round_id)
    finals_mapping = RoundMappingRepository(database).resolve(week1_round_id)
    assert (ss_mapping.afl_season_id, ss_mapping.afl_round_id) == (
        finals_mapping.afl_season_id,
        finals_mapping.afl_round_id,
    )

    ss_triggers = {t.trigger_key: t for t in built["trigger_repo"].list_triggers(ss1_round_id)}
    finals_triggers = {t.trigger_key: t for t in built["trigger_repo"].list_triggers(week1_round_id)}
    assert set(ss_triggers) == {"early-1", "main"}
    for key in ss_triggers:
        assert ss_triggers[key].trigger_type == finals_triggers[key].trigger_type
        assert ss_triggers[key].sequence == finals_triggers[key].sequence
        assert ss_triggers[key].afl_match_ids == finals_triggers[key].afl_match_ids
        # Separate persisted rows throughout -- never the same trigger row.
        assert ss_triggers[key].trigger_id != finals_triggers[key].trigger_id

    audit = database.execute(
        "SELECT action, reason FROM audit_event WHERE action='superscore.lockout_plan.synchronised_from_finals' "
        "AND entity_id=?",
        (ss1_round_id,),
    ).fetchone()
    assert audit is not None


def test_resynchronising_with_no_finals_changes_is_a_true_no_op():
    built = _seed(8301)
    database, ss1_round_id = built["database"], built["ss1_round_id"]
    synchronise_lockout_plan_from_finals(database, built["validator"], ss1_round_id, actor=ACTOR)
    before = {t.trigger_key: t.revision for t in built["trigger_repo"].list_triggers(ss1_round_id)}
    before_mapping_revision = RoundMappingRepository(database).resolve(ss1_round_id).revision

    result = synchronise_lockout_plan_from_finals(database, built["validator"], ss1_round_id, actor=ACTOR)
    assert result["mapping_synced"] is False
    assert result["synced_trigger_keys"] == []
    assert sorted(result["unchanged_trigger_keys"]) == ["early-1", "main"]
    assert result["changed"] is False

    after = {t.trigger_key: t.revision for t in built["trigger_repo"].list_triggers(ss1_round_id)}
    assert after == before
    assert RoundMappingRepository(database).resolve(ss1_round_id).revision == before_mapping_revision


def test_resynchronising_after_a_partial_finals_change_updates_only_the_diverged_trigger():
    built = _seed(8302)
    database, ss1_round_id, week1_round_id = built["database"], built["ss1_round_id"], built["week1_round_id"]
    trigger_repo = built["trigger_repo"]
    synchronise_lockout_plan_from_finals(database, built["validator"], ss1_round_id, actor=ACTOR)

    # Only the finals week's "early-1" trigger changes (a second early match added).
    trigger_repo.configure(
        week1_round_id,
        "early-1",
        "selective",
        1,
        [8213, 8299],
        actor=ACTOR,
        reason="AFL added a second early match",
        expected_revision=1,
    )

    result = synchronise_lockout_plan_from_finals(database, built["validator"], ss1_round_id, actor=ACTOR)
    assert result["mapping_synced"] is False
    assert result["synced_trigger_keys"] == ["early-1"]
    assert result["unchanged_trigger_keys"] == ["main"]

    ss_triggers = {t.trigger_key: t for t in trigger_repo.list_triggers(ss1_round_id)}
    assert ss_triggers["early-1"].revision == 2
    assert set(ss_triggers["early-1"].afl_match_ids) == {8213, 8299}
    assert ss_triggers["main"].revision == 1


def test_synchronise_never_mutates_the_finals_weeks_own_rows():
    built = _seed(8303)
    database, ss1_round_id, week1_round_id = built["database"], built["ss1_round_id"], built["week1_round_id"]
    trigger_repo = built["trigger_repo"]
    before_finals_triggers = {t.trigger_key: t.revision for t in trigger_repo.list_triggers(week1_round_id)}
    before_finals_mapping = RoundMappingRepository(database).resolve(week1_round_id).revision

    synchronise_lockout_plan_from_finals(database, built["validator"], ss1_round_id, actor=ACTOR)

    after_finals_triggers = {t.trigger_key: t.revision for t in trigger_repo.list_triggers(week1_round_id)}
    assert after_finals_triggers == before_finals_triggers
    assert RoundMappingRepository(database).resolve(week1_round_id).revision == before_finals_mapping
