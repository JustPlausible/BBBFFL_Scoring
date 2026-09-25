"""Issue #237: production-safe fresh-season and phase initialization
(`app.season_setup`) against real migrated databases -- every boundary's
clean start, repeat/idempotent call, missing-prerequisite and invalid-state
refusal (with domain tables and the audit trail left unchanged),
cross-season isolation, and the full journey of a genuinely fresh season
from no structure through preseason draft readiness, a completed
home-and-away season, Finals and SuperScore."""

import pytest

from app.audit import AuditEventRepository
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.db import transaction
from app.draft import DraftRepository
from app.finals import FinalsBracketRepository
from app.fixtures import FixtureRepository
from app.opening_round import OpeningRoundRuleRepository
from app.player_pool import PlayerPoolRepository
from app.round_mapping import RoundMappingRepository
from app.season import SeasonRepository
from app.season_setup import (
    SeasonSetupAflError,
    SeasonSetupError,
    accept_draft_order,
    accept_opening_round_rules,
    build_season_setup,
    configure_squad_limit,
    initialize_finals,
    initialize_ordinary_competition,
    initialize_superscore,
    live_source_provider,
    preview_opening_round,
    refresh_player_pool,
)
from app.superscore_round import ensure_round, ensure_stream, get_stream
from tests.db_helpers import migrated_connection
from tests.finals_helpers import seed_finals_seeding_snapshot_row
from tests.finals_seeding_helpers import all_draws, build_2026_replay_season
from tests.midseason_draft_helpers import KnownRound, dominant_scores
from tests.season_setup_helpers import (
    SCORER,
    STRUCTURE_TABLES,
    SetupAfl,
    fresh_season,
    opening_round_fixture,
    season_players,
    table_counts,
)

REASON = "issue #237 test"
# The same AFL season (77) as `SetupAfl()`, but its fixture has no Opening
# Round, so the draft's Opening Round gate passes without any rule.
NO_OPENING = SetupAfl(rounds=opening_round_fixture(with_opening=False)[0], matches={})


def _steps(database, season_id):
    return {step["key"]: step for step in build_season_setup(database, season_id)["steps"]}


def _ready_for_draft(database, season, afl, *, squad_limit=4):
    refresh_player_pool(database, afl, season.season_id, afl.afl_season_id, actor=SCORER, reason=REASON)
    initialize_ordinary_competition(database, season.season_id, actor=SCORER, reason=REASON)
    configure_squad_limit(database, season.season_id, squad_limit, actor=SCORER, reason=REASON)


def _play_out_regular_season(database, season, entries):
    """Freeze the fixture, map, open and publish every regular-season round
    of the setup-created ordinary competition through the normal lifecycle
    services -- a focused persisted-state stand-in for a season's weekly
    operation, so Finals initialization runs against real final rounds."""
    fixtures = FixtureRepository(database)
    fixtures.save_draft(season.season_id, [entry.season_entry_id for entry in entries])
    fixtures.freeze(season.season_id)
    competition = next(c for c in SeasonRepository(database).list_competitions(season.season_id))
    lifecycle = CompetitionLifecycleRepository(database)
    mapping = RoundMappingRepository(database)
    for logical in SeasonRepository(database).list_rounds(competition.competition_id):
        pair = (season.year, 2000 + logical.sequence)
        mapping.accept(logical.bbbffl_round_id, *pair, KnownRound({pair}))
        round_ = lifecycle.create_ordinary_round(logical.bbbffl_round_id)
        for state in ("open", "live", "review"):
            lifecycle.transition(round_.bbbffl_round_id, state)
        lifecycle.publish_results(round_.bbbffl_round_id, dominant_scores(lifecycle, round_.bbbffl_round_id, entries))


# -- Player pool ------------------------------------------------------------------


def test_player_pool_clean_start_then_idempotent_refresh():
    database = migrated_connection()
    season, _entries = fresh_season(database)
    afl = SetupAfl(players=season_players(30))

    first = refresh_player_pool(database, afl, season.season_id, 77, actor=SCORER, reason=REASON)
    assert (first["inserted"], first["updated"], first["unchanged"], first["pool_size"]) == (30, 0, 0, 30)
    pool = PlayerPoolRepository(database)
    assert len(pool.list_selectable(season.season_id)) == 30
    assert {p.source_provider for p in pool.list_selectable(season.season_id)} == {live_source_provider(77)}

    renamed = season_players(30)
    renamed[0] = renamed[0].__class__(renamed[0].canonical_player_id, "Renamed Player", renamed[0].team)
    second = refresh_player_pool(
        database,
        SetupAfl(players=renamed + season_players(2, start=5000)),
        season.season_id,
        77,
        actor=SCORER,
        reason=REASON,
    )
    assert (second["inserted"], second["updated"], second["unchanged"], second["pool_size"]) == (2, 1, 29, 32)
    assert pool.get(season.season_id, renamed[0].canonical_player_id).display_name == "Renamed Player"

    events = AuditEventRepository(database).list_events(action="player_pool.season.refreshed")
    assert [(e.actor_id, e.actor_role, e.reason) for e in events] == [("scorer-coach-id", "scorer", REASON)] * 2


def test_player_pool_refresh_never_deletes_or_changes_eligibility_or_ownership():
    database = migrated_connection()
    season, entries = fresh_season(database)
    refresh_player_pool(
        database, SetupAfl(players=season_players(12)), season.season_id, 77, actor=SCORER, reason=REASON
    )
    pool = PlayerPoolRepository(database)
    owned = pool.get(season.season_id, 1000)
    from app.player_pool import OwnershipRepository

    OwnershipRepository(database).configure_squad_limit(season.season_id, 2)
    OwnershipRepository(database).acquire(owned.season_player_id, entries[0].season_entry_id)
    result = refresh_player_pool(
        database, SetupAfl(players=season_players(11, start=1001)), season.season_id, 77, actor=SCORER, reason=REASON
    )
    assert result["missing_from_source"] == [1000]
    assert pool.get(season.season_id, 1000) is not None
    assert pool.get(season.season_id, 1000).eligible is True
    assert [p.season_player_id for p in OwnershipRepository(database).current_squad(entries[0].season_entry_id)] == [
        owned.season_player_id
    ]


@pytest.mark.parametrize(
    ("afl", "season_id_override", "error", "message"),
    [
        (SetupAfl(year=2026), None, SeasonSetupError, "refusing cross-season setup"),
        (SetupAfl(), 999, SeasonSetupError, "does not publish an AFL season"),
        (SetupAfl(fresh=False), None, SeasonSetupAflError, "stale cache"),
    ],
)
def test_player_pool_refuses_wrong_season_unknown_season_and_stale_evidence(afl, season_id_override, error, message):
    database = migrated_connection()
    season, _entries = fresh_season(database)
    before = table_counts(database, *STRUCTURE_TABLES)
    with pytest.raises(error, match=message):
        refresh_player_pool(
            database, afl, season.season_id, season_id_override or afl.afl_season_id, actor=SCORER, reason=REASON
        )
    assert table_counts(database, *STRUCTURE_TABLES) == before


def test_player_pool_refuses_replay_data_source_and_mixing_afl_seasons():
    database = migrated_connection()
    season, _entries = fresh_season(database)

    class ReplayLike:
        def get_seasons(self):
            return SetupAfl().get_seasons()

    with pytest.raises(SeasonSetupAflError, match="BBBFFL_AFL_MODE=live"):
        refresh_player_pool(database, ReplayLike(), season.season_id, 77, actor=SCORER, reason=REASON)

    refresh_player_pool(database, SetupAfl(), season.season_id, 77, actor=SCORER, reason=REASON)
    before = table_counts(database, *STRUCTURE_TABLES)
    # A second AFL season id that also claims the same year must not be
    # mixed into an already-populated pool.
    with pytest.raises(SeasonSetupError, match="refusing to mix"):
        refresh_player_pool(database, SetupAfl(afl_season_id=88), season.season_id, 88, actor=SCORER, reason=REASON)
    assert table_counts(database, *STRUCTURE_TABLES) == before


def test_setup_actions_require_an_explicit_reason():
    database = migrated_connection()
    season, _entries = fresh_season(database)
    with pytest.raises(SeasonSetupError, match="explicit reason"):
        initialize_ordinary_competition(database, season.season_id, actor=SCORER, reason="  ")


# -- Ordinary competition -------------------------------------------------------------


def test_ordinary_competition_clean_start_and_repeat_is_a_no_op():
    database = migrated_connection()
    season, _entries = fresh_season(database, regular_season_round_count=20)

    created = initialize_ordinary_competition(database, season.season_id, actor=SCORER, reason=REASON)
    assert created["created"] is True
    assert created["round_count"] == 20
    assert created["rules_version"] == "2027 ordinary rules (v1)"
    seasons = SeasonRepository(database)
    [competition] = seasons.list_competitions(season.season_id)
    assert (competition.stream_type, competition.stream_key) == ("ordinary", "ordinary")
    rounds = seasons.list_rounds(competition.competition_id)
    assert [(r.sequence, r.round_key, r.label) for r in rounds] == [
        (n, f"round-{n}", f"Round {n}") for n in range(1, 21)
    ]

    before = table_counts(database, *STRUCTURE_TABLES)
    repeat = initialize_ordinary_competition(database, season.season_id, actor=SCORER, reason=REASON)
    assert repeat["created"] is False
    assert repeat["competition_id"] == competition.competition_id
    assert table_counts(database, *STRUCTURE_TABLES) == before

    [event] = AuditEventRepository(database).list_events(action="season.ordinary_competition.initialized")
    assert (event.actor_id, event.actor_role, event.reason) == ("scorer-coach-id", "scorer", REASON)


def test_ordinary_competition_refuses_a_partial_structure_it_did_not_create():
    database = migrated_connection()
    season, _entries = fresh_season(database, regular_season_round_count=20)
    seasons = SeasonRepository(database)
    rules = seasons.create_rules_version(season.season_id, "ordinary", 1, "Rules")
    competition = seasons.create_competition(
        season.season_id, rules.rules_version_id, "ordinary", "Ordinary", "ordinary"
    )
    for n in (1, 2, 3):
        seasons.create_round(competition.competition_id, f"round-{n}", f"Round {n}", n)
    before = table_counts(database, *STRUCTURE_TABLES)
    with pytest.raises(SeasonSetupError, match=r"missing: \[4, 5"):
        initialize_ordinary_competition(database, season.season_id, actor=SCORER, reason=REASON)
    assert table_counts(database, *STRUCTURE_TABLES) == before
    assert _steps(database, season.season_id)["ordinary_competition"]["status"] == "conflict"


def test_ordinary_competition_reuses_a_lone_existing_ordinary_rules_version():
    database = migrated_connection()
    season, _entries = fresh_season(database)
    rules = SeasonRepository(database).create_rules_version(season.season_id, "ordinary", 1, "Agreed 2027 rules")
    created = initialize_ordinary_competition(database, season.season_id, actor=SCORER, reason=REASON)
    assert created["rules_version"] == "Agreed 2027 rules (v1)"
    [competition] = SeasonRepository(database).list_competitions(season.season_id)
    assert competition.rules_version_id == rules.rules_version_id


def test_ordinary_competition_refused_for_a_completed_season():
    database = migrated_connection()
    season, _entries = fresh_season(database)
    with transaction(database) as conn:
        conn.execute("UPDATE bbbffl_season SET lifecycle_state='completed' WHERE season_id=?", (season.season_id,))
    with pytest.raises(SeasonSetupError, match="completed"):
        initialize_ordinary_competition(database, season.season_id, actor=SCORER, reason=REASON)


def test_initialization_is_isolated_per_season():
    database = migrated_connection()
    old = build_2026_replay_season(database=database, year=2026)
    season, _entries = fresh_season(database, year=2027)
    old_before = {
        "rounds": len(SeasonRepository(database).list_rounds(old["competition"].competition_id)),
        "pool": len(PlayerPoolRepository(database).list_selectable(old["season"].season_id)),
    }
    _ready_for_draft(database, season, SetupAfl())
    assert len(SeasonRepository(database).list_rounds(old["competition"].competition_id)) == old_before["rounds"]
    assert len(PlayerPoolRepository(database).list_selectable(old["season"].season_id)) == old_before["pool"]
    [new_competition] = SeasonRepository(database).list_competitions(season.season_id)
    assert new_competition.competition_id != old["competition"].competition_id
    # The 2026 replay season's draft/finals state is untouched by 2027 setup.
    assert DraftRepository(database).status(season.season_id) is None


# -- Squad limit and draft order ---------------------------------------------------------


def test_squad_limit_repeat_is_a_no_op_and_change_after_draft_is_refused():
    database = migrated_connection()
    season, entries = fresh_season(database)
    _ready_for_draft(database, season, SetupAfl())
    assert configure_squad_limit(database, season.season_id, 4, actor=SCORER, reason=REASON)["changed"] is False
    accept_draft_order(
        database, NO_OPENING, season.season_id, [e.season_entry_id for e in entries], actor=SCORER, reason=REASON
    )
    before = table_counts(database, *STRUCTURE_TABLES)
    with pytest.raises(SeasonSetupError, match="cannot change after the season draft is accepted"):
        configure_squad_limit(database, season.season_id, 5, actor=SCORER, reason=REASON)
    assert table_counts(database, *STRUCTURE_TABLES) == before


def test_draft_order_refuses_each_missing_prerequisite_without_writing():
    database = migrated_connection()
    season, entries = fresh_season(database)
    order = [e.season_entry_id for e in entries]
    before = table_counts(database, *STRUCTURE_TABLES)
    with pytest.raises(SeasonSetupError) as refused:
        accept_draft_order(database, NO_OPENING, season.season_id, order, actor=SCORER, reason=REASON)
    assert "ordinary competition must be initialized" in str(refused.value)
    assert "configure the squad limit" in str(refused.value)
    assert table_counts(database, *STRUCTURE_TABLES) == before

    refresh_player_pool(
        database, SetupAfl(players=season_players(39)), season.season_id, 77, actor=SCORER, reason=REASON
    )
    initialize_ordinary_competition(database, season.season_id, actor=SCORER, reason=REASON)
    configure_squad_limit(database, season.season_id, 4, actor=SCORER, reason=REASON)
    with pytest.raises(SeasonSetupError, match="needs at least 40"):
        accept_draft_order(database, NO_OPENING, season.season_id, order, actor=SCORER, reason=REASON)
    assert DraftRepository(database).status(season.season_id) is None


def test_draft_order_requires_exactly_ten_entries_and_a_complete_order():
    database = migrated_connection()
    season, entries = fresh_season(database, entry_count=9)
    _ready_for_draft(database, season, SetupAfl())
    with pytest.raises(SeasonSetupError, match="exactly 10 season entries"):
        accept_draft_order(
            database, NO_OPENING, season.season_id, [e.season_entry_id for e in entries], actor=SCORER, reason=REASON
        )

    database = migrated_connection()
    season, entries = fresh_season(database)
    _ready_for_draft(database, season, SetupAfl())
    with pytest.raises(SeasonSetupError, match="exactly once"):
        accept_draft_order(
            database,
            NO_OPENING,
            season.season_id,
            [e.season_entry_id for e in entries[:9]] * 1 + [entries[0].season_entry_id],
            actor=SCORER,
            reason=REASON,
        )
    assert DraftRepository(database).status(season.season_id) is None


def test_draft_order_clean_start_repeat_and_conflicting_order():
    database = migrated_connection()
    season, entries = fresh_season(database)
    _ready_for_draft(database, season, SetupAfl())
    order = [e.season_entry_id for e in reversed(entries)]

    assert (
        accept_draft_order(database, NO_OPENING, season.season_id, order, actor=SCORER, reason=REASON)["created"]
        is True
    )
    status = DraftRepository(database).status(season.season_id)
    assert (status.total_picks, status.completed_picks, status.target_squad_size) == (40, 0, 4)
    assert DraftRepository(database).next_pick(season.season_id).current_season_entry_id == order[0]

    before = table_counts(database, *STRUCTURE_TABLES)
    assert (
        accept_draft_order(database, NO_OPENING, season.season_id, order, actor=SCORER, reason=REASON)["created"]
        is False
    )
    with pytest.raises(SeasonSetupError, match="already been accepted"):
        accept_draft_order(database, NO_OPENING, season.season_id, list(reversed(order)), actor=SCORER, reason=REASON)
    assert table_counts(database, *STRUCTURE_TABLES) == before
    [event] = AuditEventRepository(database).list_events(action="draft.order.accepted")
    assert (event.actor_id, event.actor_role) == ("scorer-coach-id", "scorer")


# -- Opening Round ------------------------------------------------------------------------


def test_opening_round_not_applicable_when_the_fixture_has_no_round_zero():
    database = migrated_connection()
    season, entries = fresh_season(database)
    rounds, matches = opening_round_fixture(with_opening=False)
    afl = SetupAfl(rounds=rounds, matches=matches)
    _ready_for_draft(database, season, afl)
    preview = preview_opening_round(database, afl, season.season_id, 77)
    assert preview["applicable"] is False
    assert "no Opening Round" in preview["diagnostic"]
    with pytest.raises(SeasonSetupError, match="no Opening Round"):
        accept_opening_round_rules(database, afl, season.season_id, 77, {}, actor=SCORER, reason=REASON)
    # The season proceeds to the draft without manufacturing any rule.
    accept_draft_order(
        database, afl, season.season_id, [e.season_entry_id for e in entries], actor=SCORER, reason=REASON
    )
    assert OpeningRoundRuleRepository(database).list_accepted_for_season(season.season_id) == []
    assert _steps(database, season.season_id)["opening_round"]["status"] == "optional"


def test_opening_round_preview_derives_rules_from_the_live_fixture_and_accepts_atomically():
    database = migrated_connection()
    season, _entries = fresh_season(database)
    afl = SetupAfl()
    _ready_for_draft(database, season, afl)
    preview = preview_opening_round(database, afl, season.season_id, 77)
    assert preview["applicable"] and preview["ready"]
    assert [
        (r["afl_club_id"], r["afl_bye_round_number"], r["recommended_bbbffl_round_number"]) for r in preview["rules"]
    ] == [
        (1, 2, 2),
        (2, 2, 2),
        (3, 3, 3),
        (4, 4, 4),
    ]
    targets = {1: 2, 2: 2, 3: 3, 4: 5}  # an operator may deliberately diverge from the recommendation
    result = accept_opening_round_rules(database, afl, season.season_id, 77, targets, actor=SCORER, reason=REASON)
    assert result["created"] and len(result["accepted"]) == 4
    rules = {r.afl_club_id: r for r in OpeningRoundRuleRepository(database).list_accepted_for_season(season.season_id)}
    rounds = {
        r.sequence: r.bbbffl_round_id
        for r in SeasonRepository(database).list_rounds(
            SeasonRepository(database).list_competitions(season.season_id)[0].competition_id
        )
    }
    assert rules[4].bbbffl_round_id == rounds[5]
    assert (rules[1].afl_opening_round_id, rules[1].afl_bye_round_id, rules[1].evidence_classification) == (
        500,
        502,
        "known_fact",
    )

    before = table_counts(database, *STRUCTURE_TABLES)
    assert (
        accept_opening_round_rules(database, afl, season.season_id, 77, targets, actor=SCORER, reason=REASON)["created"]
        is False
    )
    with pytest.raises(SeasonSetupError, match="audited correction"):
        accept_opening_round_rules(database, afl, season.season_id, 77, {**targets, 4: 4}, actor=SCORER, reason=REASON)
    assert table_counts(database, *STRUCTURE_TABLES) == before
    assert _steps(database, season.season_id)["opening_round"]["status"] == "complete"


def test_opening_round_refuses_incomplete_club_sets_bad_targets_and_unresolved_byes():
    database = migrated_connection()
    season, _entries = fresh_season(database, regular_season_round_count=20)
    afl = SetupAfl()
    _ready_for_draft(database, season, afl)
    before = table_counts(database, *STRUCTURE_TABLES)
    with pytest.raises(SeasonSetupError, match="exactly the clubs playing"):
        accept_opening_round_rules(database, afl, season.season_id, 77, {1: 2, 2: 2, 3: 3}, actor=SCORER, reason=REASON)
    with pytest.raises(SeasonSetupError, match="not one of this season's ordinary rounds"):
        accept_opening_round_rules(
            database, afl, season.season_id, 77, {1: 2, 2: 2, 3: 3, 4: 21}, actor=SCORER, reason=REASON
        )
    rounds, matches = opening_round_fixture(unpublished_bye_round=3)
    unresolved = SetupAfl(rounds=rounds, matches=matches)
    preview = preview_opening_round(database, unresolved, season.season_id, 77)
    assert preview["ready"] is False
    assert {r["afl_club_id"] for r in preview["rules"] if r["unresolved"]} == {3, 4}
    with pytest.raises(SeasonSetupError, match="cannot be derived"):
        accept_opening_round_rules(
            database, unresolved, season.season_id, 77, {1: 2, 2: 2, 3: 3, 4: 4}, actor=SCORER, reason=REASON
        )
    assert table_counts(database, *STRUCTURE_TABLES) == before


def test_opening_round_requires_the_ordinary_competition_and_is_refused_after_pick_one():
    database = migrated_connection()
    season, entries = fresh_season(database)
    afl = SetupAfl()
    with pytest.raises(SeasonSetupError, match="initialize the ordinary competition"):
        preview_opening_round(database, afl, season.season_id, 77)

    _ready_for_draft(database, season, afl)
    # A season whose draft began before its Opening Round was configured
    # (reachable only outside this workflow, which now refuses it -- see
    # `test_draft_order_is_refused_until_the_live_opening_round_is_configured`).
    DraftRepository(database).accept_order(season.season_id, [e.season_entry_id for e in entries])
    draft = DraftRepository(database)
    pick = draft.next_pick(season.season_id)
    player = PlayerPoolRepository(database).list_available(season.season_id)[0]
    draft.execute_pick(season.season_id, pick.current_season_entry_id, player.season_player_id)
    before = table_counts(database, *STRUCTURE_TABLES)
    with pytest.raises(SeasonSetupError, match="before-Pick-1"):
        accept_opening_round_rules(
            database, afl, season.season_id, 77, {1: 2, 2: 2, 3: 3, 4: 4}, actor=SCORER, reason=REASON
        )
    assert table_counts(database, *STRUCTURE_TABLES) == before
    assert _steps(database, season.season_id)["opening_round"]["status"] == "not_applicable"


# -- Finals and SuperScore ------------------------------------------------------------------


def test_finals_and_superscore_are_blocked_before_the_home_and_away_season_is_complete():
    database = migrated_connection()
    season, _entries = fresh_season(database)
    _ready_for_draft(database, season, SetupAfl())
    before = table_counts(database, *STRUCTURE_TABLES)
    with pytest.raises(SeasonSetupError, match="not yet final"):
        initialize_finals(database, season.season_id, actor=SCORER, reason=REASON)
    with pytest.raises(SeasonSetupError, match="initialize Finals first"):
        initialize_superscore(database, season.season_id, actor=SCORER, reason=REASON)
    assert table_counts(database, *STRUCTURE_TABLES) == before
    steps = _steps(database, season.season_id)
    assert steps["finals"]["status"] == "blocked"
    assert steps["superscore"]["status"] == "blocked"


def test_finals_refuses_an_unresolved_ladder_tie_without_creating_a_stream():
    built = build_2026_replay_season(year=2051, score_fn=all_draws)
    database, season_id = built["database"], built["season"].season_id
    before = table_counts(database, *STRUCTURE_TABLES)
    with pytest.raises(SeasonSetupError, match="unresolved tie"):
        initialize_finals(database, season_id, actor=SCORER, reason=REASON)
    assert table_counts(database, *STRUCTURE_TABLES) == before
    assert _steps(database, season_id)["finals"]["status"] == "blocked"


def test_finals_refuses_the_2026_historical_snapshot_path():
    built = build_2026_replay_season(year=2052)
    database, season_id = built["database"], built["season"].season_id
    seed_finals_seeding_snapshot_row(
        database, season_id, built["competition"].competition_id, [e.season_entry_id for e in built["entries"]]
    )
    before = table_counts(database, *STRUCTURE_TABLES)
    with pytest.raises(SeasonSetupError, match="historical finals-seeding snapshot"):
        initialize_finals(database, season_id, actor=SCORER, reason=REASON)
    assert table_counts(database, *STRUCTURE_TABLES) == before


def test_finals_from_the_live_ladder_then_superscore_are_idempotent():
    built = build_2026_replay_season(year=2053)
    database, season_id = built["database"], built["season"].season_id
    assert _steps(database, season_id)["finals"]["status"] == "available"

    finals = initialize_finals(database, season_id, actor=SCORER, reason=REASON)
    assert finals["created"] and finals["seed_source"] == "ladder" and finals["finals_stream_created"]
    bracket = FinalsBracketRepository(database).get_bracket_by_id(finals["bracket_id"])
    seeds = [row.season_entry_id for row in FinalsBracketRepository(database).list_seed_rows(bracket.bracket_id)]
    assert seeds == [e.season_entry_id for e in built["entries"]]  # dominant_scores: entries[0] is ladder-first
    before = table_counts(database, *STRUCTURE_TABLES)
    assert initialize_finals(database, season_id, actor=SCORER, reason=REASON)["created"] is False
    assert table_counts(database, *STRUCTURE_TABLES) == before
    [stream_event] = AuditEventRepository(database).list_events(action="finals.stream.created")
    assert (stream_event.actor_id, stream_event.reason) == ("scorer-coach-id", REASON)

    superscore = initialize_superscore(database, season_id, actor=SCORER, reason=REASON)
    assert superscore["created"] and superscore["created_rounds"] == ["SS1", "SS2", "SS3", "SS4"]
    stream = get_stream(database, season_id)
    assert [(r.sequence, r.round_key) for r in SeasonRepository(database).list_rounds(stream.competition_id)] == [
        (1, "ss1"),
        (2, "ss2"),
        (3, "ss3"),
        (4, "ss4"),
    ]
    before = table_counts(database, *STRUCTURE_TABLES)
    assert initialize_superscore(database, season_id, actor=SCORER, reason=REASON)["created"] is False
    assert table_counts(database, *STRUCTURE_TABLES) == before
    steps = _steps(database, season_id)
    assert steps["finals"]["status"] == "complete"
    assert steps["superscore"]["status"] == "complete"


def test_superscore_completes_a_partial_2026_style_structure_but_refuses_a_foreign_one():
    built = build_2026_replay_season(year=2054)
    database, season_id = built["database"], built["season"].season_id
    initialize_finals(database, season_id, actor=SCORER, reason=REASON)
    rules_id = built["competition"].rules_version_id
    stream = ensure_stream(database, season_id, rules_id, built["competition"].competition_id)
    ensure_round(database, stream.competition_id, 1, 1)
    result = initialize_superscore(database, season_id, actor=SCORER, reason=REASON)
    assert result["stream_created"] is False
    assert result["created_rounds"] == ["SS2", "SS3", "SS4"]

    other = build_2026_replay_season(year=2055)
    database2, season2 = other["database"], other["season"].season_id
    initialize_finals(database2, season2, actor=SCORER, reason=REASON)
    stream2 = ensure_stream(
        database2, season2, other["competition"].rules_version_id, other["competition"].competition_id
    )
    SeasonRepository(database2).create_round(stream2.competition_id, "ss1", "Super 1", 1)
    before = table_counts(database2, *STRUCTURE_TABLES)
    with pytest.raises(SeasonSetupError, match="differently-shaped"):
        initialize_superscore(database2, season2, actor=SCORER, reason=REASON)
    assert table_counts(database2, *STRUCTURE_TABLES) == before


# -- The full journey ---------------------------------------------------------------------------


def test_fresh_season_journey_from_nothing_to_finals_and_superscore():
    """A clean, production-like database: no season at all, then every
    initialization boundary in order through the supported service path --
    the ordinary weekly lifecycle in between is driven by the existing
    lifecycle services, not by this module."""
    database = migrated_connection()
    assert SeasonRepository(database).list_seasons() == []
    season, entries = fresh_season(database, regular_season_round_count=9)
    afl = SetupAfl()
    setup = build_season_setup(database, season.season_id)
    assert setup["next_step"] == "player_pool"

    refresh_player_pool(database, afl, season.season_id, 77, actor=SCORER, reason=REASON)
    assert build_season_setup(database, season.season_id)["next_step"] == "ordinary_competition"
    initialize_ordinary_competition(database, season.season_id, actor=SCORER, reason=REASON)
    accept_opening_round_rules(
        database, afl, season.season_id, 77, {1: 2, 2: 2, 3: 3, 4: 4}, actor=SCORER, reason=REASON
    )
    configure_squad_limit(database, season.season_id, 4, actor=SCORER, reason=REASON)
    assert build_season_setup(database, season.season_id)["next_step"] == "draft_order"
    accept_draft_order(
        database, afl, season.season_id, [e.season_entry_id for e in entries], actor=SCORER, reason=REASON
    )
    steps = _steps(database, season.season_id)
    assert steps["draft_order"]["status"] == "complete"
    assert steps["draft_order"]["links"]["draft_board"] == f"/admin/draft/{season.season_id}"
    assert build_season_setup(database, season.season_id)["next_step"] == "fixture_draw"

    _play_out_regular_season(database, season, entries)
    assert _steps(database, season.season_id)["finals"]["status"] == "available"
    finals = initialize_finals(database, season.season_id, actor=SCORER, reason=REASON)
    assert finals["seed_source"] == "ladder"
    initialize_superscore(database, season.season_id, actor=SCORER, reason=REASON)
    final = build_season_setup(database, season.season_id)
    assert {step["key"]: step["status"] for step in final["steps"]} == {
        "entries": "complete",
        "player_pool": "complete",
        "ordinary_competition": "complete",
        "opening_round": "complete",
        "squad_limit": "complete",
        "draft_order": "complete",
        "fixture_draw": "complete",
        "finals": "complete",
        "superscore": "complete",
    }
    assert final["next_step"] is None


def test_an_ambiguous_second_finals_stream_fails_closed_even_after_a_bracket_exists():
    """Codex review, PR #247 (P2): the "already initialized" no-op and the
    SuperScore prerequisite must not accept a bracket while a second
    `finals` stream makes the Finals phase ambiguous."""
    built = build_2026_replay_season(year=2056)
    database, season_id = built["database"], built["season"].season_id
    initialize_finals(database, season_id, actor=SCORER, reason=REASON)
    SeasonRepository(database).create_competition(
        season_id, built["competition"].rules_version_id, "finals-extra", "Extra Finals", "finals"
    )
    before = table_counts(database, *STRUCTURE_TABLES)
    with pytest.raises(SeasonSetupError, match="2 finals competition streams"):
        initialize_finals(database, season_id, actor=SCORER, reason=REASON)
    with pytest.raises(SeasonSetupError, match="2 finals competition streams"):
        initialize_superscore(database, season_id, actor=SCORER, reason=REASON)
    assert table_counts(database, *STRUCTURE_TABLES) == before
    steps = _steps(database, season_id)
    assert steps["finals"]["status"] == "conflict"
    assert steps["superscore"]["status"] == "blocked"


def test_a_complete_but_differently_shaped_superscore_structure_is_a_conflict_not_complete():
    """Codex review, PR #247 (P2): all four `ss1`-`ss4` keys present but one
    with the wrong sequence must not read as complete -- it is exactly the
    structure `initialize_structure` refuses."""
    built = build_2026_replay_season(year=2057)
    database, season_id = built["database"], built["season"].season_id
    initialize_finals(database, season_id, actor=SCORER, reason=REASON)
    stream = ensure_stream(
        database, season_id, built["competition"].rules_version_id, built["competition"].competition_id
    )
    for number in (1, 2, 3):
        ensure_round(database, stream.competition_id, number, number)
    SeasonRepository(database).create_round(stream.competition_id, "ss4", "SS4", 9)
    step = _steps(database, season_id)["superscore"]
    assert step["status"] == "conflict"
    assert "ss4" in step["blockers"][0]
    with pytest.raises(SeasonSetupError, match="differently-shaped"):
        initialize_superscore(database, season_id, actor=SCORER, reason=REASON)


def test_draft_order_is_refused_until_the_live_opening_round_is_configured():
    """Codex review, PR #247 (P1): Opening Round rules cannot be added after
    Pick 1, so while the live fixture has an Opening Round the draft order
    is refused until every participating club's rule is accepted -- the
    draft can never start with the decision still open."""
    database = migrated_connection()
    season, entries = fresh_season(database)
    afl = SetupAfl()
    _ready_for_draft(database, season, afl)
    order = [e.season_entry_id for e in entries]
    before = table_counts(database, *STRUCTURE_TABLES)
    with pytest.raises(SeasonSetupError, match="has an Opening Round: accept its compensating-bye rules"):
        accept_draft_order(database, afl, season.season_id, order, actor=SCORER, reason=REASON)
    assert table_counts(database, *STRUCTURE_TABLES) == before

    class Down(SetupAfl):
        def get_rounds(self, afl_season_id):
            from app.afl_client import AflApiConnectionError

            raise AflApiConnectionError("/api/v1/seasons/77/rounds")

    with pytest.raises(SeasonSetupAflError):
        accept_draft_order(database, Down(), season.season_id, order, actor=SCORER, reason=REASON)
    assert table_counts(database, *STRUCTURE_TABLES) == before

    accept_opening_round_rules(
        database, afl, season.season_id, 77, {1: 2, 2: 2, 3: 3, 4: 4}, actor=SCORER, reason=REASON
    )
    result = accept_draft_order(database, afl, season.season_id, order, actor=SCORER, reason=REASON)
    assert result["created"] is True
    assert result["opening_round"] == {"afl_season_id": 77, "opening_round": "configured", "rule_count": 4}


def test_draft_order_requires_a_live_populated_pool_for_the_opening_round_check():
    database = migrated_connection()
    season, entries = fresh_season(database)
    initialize_ordinary_competition(database, season.season_id, actor=SCORER, reason=REASON)
    configure_squad_limit(database, season.season_id, 1, actor=SCORER, reason=REASON)
    pool = PlayerPoolRepository(database)
    for n in range(10):
        pool.refresh_player(season.season_id, 70_000 + n, f"Legacy {n}", source_provider="afl-api-v1")
    with pytest.raises(SeasonSetupError, match="populated from exactly one live afl-api season"):
        accept_draft_order(
            database, NO_OPENING, season.season_id, [e.season_entry_id for e in entries], actor=SCORER, reason=REASON
        )
    assert DraftRepository(database).status(season.season_id) is None


def test_multiple_superscore_streams_are_refused_and_reported_as_a_conflict():
    """Codex review, PR #247 (P2): the schema permits two `superscore_stream`
    rows for one season; initialization must not pick one arbitrarily."""
    from app.db import transaction as tx
    from app.superscore_round import SuperScoreRoundError  # noqa: F401 -- mapped to SeasonSetupError

    built = build_2026_replay_season(year=2058)
    database, season_id = built["database"], built["season"].season_id
    initialize_finals(database, season_id, actor=SCORER, reason=REASON)
    rules_id, ordinary_id = built["competition"].rules_version_id, built["competition"].competition_id
    seasons = SeasonRepository(database)
    for key in ("superscore", "superscore-b"):
        competition = seasons.create_competition(season_id, rules_id, key, key, "superscore")
        with tx(database) as conn:
            conn.execute(
                "INSERT INTO superscore_stream VALUES (?, ?, ?, ?)",
                (competition.competition_id, season_id, ordinary_id, "2026-01-01T00:00:00+00:00"),
            )
    before = table_counts(database, *STRUCTURE_TABLES)
    with pytest.raises(SeasonSetupError, match="2 SuperScore streams"):
        initialize_superscore(database, season_id, actor=SCORER, reason=REASON)
    assert table_counts(database, *STRUCTURE_TABLES) == before
    assert _steps(database, season_id)["superscore"]["status"] == "conflict"


def test_draft_order_is_refused_while_any_team_already_owns_a_player():
    """Codex review, PR #247 (P2): a team with a pre-owned player could never
    complete its last snake pick, so the preseason draft needs empty squads."""
    from app.player_pool import OwnershipRepository

    database = migrated_connection()
    season, entries = fresh_season(database)
    _ready_for_draft(database, season, NO_OPENING)
    player = PlayerPoolRepository(database).list_available(season.season_id)[0]
    OwnershipRepository(database).acquire(player.season_player_id, entries[0].season_entry_id)
    before = table_counts(database, *STRUCTURE_TABLES)
    with pytest.raises(SeasonSetupError, match="already owned"):
        accept_draft_order(
            database, NO_OPENING, season.season_id, [e.season_entry_id for e in entries], actor=SCORER, reason=REASON
        )
    assert table_counts(database, *STRUCTURE_TABLES) == before
    assert _steps(database, season.season_id)["draft_order"]["status"] == "blocked"


def test_draft_order_is_refused_when_accepted_rules_outlive_a_fixture_without_an_opening_round():
    """Codex review, PR #247 (P2): stale accepted Opening Round rules plus a
    corrected fixture with no round 0 is a conflict, not "not required"."""
    database = migrated_connection()
    season, entries = fresh_season(database)
    afl = SetupAfl()
    _ready_for_draft(database, season, afl)
    accept_opening_round_rules(
        database, afl, season.season_id, 77, {1: 2, 2: 2, 3: 3, 4: 4}, actor=SCORER, reason=REASON
    )
    before = table_counts(database, *STRUCTURE_TABLES)
    with pytest.raises(SeasonSetupError, match="no longer has an Opening Round"):
        accept_draft_order(
            database, NO_OPENING, season.season_id, [e.season_entry_id for e in entries], actor=SCORER, reason=REASON
        )
    assert table_counts(database, *STRUCTURE_TABLES) == before


def test_an_accepted_rule_targeting_another_seasons_round_does_not_count_as_configured():
    """Codex review, PR #247 (P2): a rule whose AFL ids match the fixture
    but whose BBBFFL target is not one of this season's ordinary rounds is
    never applied by round preflight, so it must not satisfy the draft gate."""
    from tests.midseason_draft_helpers import KnownRound

    database = migrated_connection()
    other, _other_entries = fresh_season(database, year=2031)
    initialize_ordinary_competition(database, other.season_id, actor=SCORER, reason=REASON)
    [other_competition] = SeasonRepository(database).list_competitions(other.season_id)
    foreign_round = SeasonRepository(database).list_rounds(other_competition.competition_id)[1]

    season, entries = fresh_season(database)
    afl = SetupAfl()
    _ready_for_draft(database, season, afl)
    rules = OpeningRoundRuleRepository(database)
    known = KnownRound({(77, 500), (77, 502), (77, 503), (77, 504)})
    own_rounds = {
        r.sequence: r.bbbffl_round_id
        for r in SeasonRepository(database).list_rounds(
            SeasonRepository(database).list_competitions(season.season_id)[0].competition_id
        )
    }
    for club, bye, target in ((1, 502, own_rounds[2]), (2, 502, own_rounds[2]), (3, 503, own_rounds[3])):
        rules.accept(season.season_id, club, 77, 500, bye, target, known)
    rules.accept(season.season_id, 4, 77, 500, 504, foreign_round.bbbffl_round_id, known)

    preview = preview_opening_round(database, afl, season.season_id, 77)
    assert [r["afl_club_id"] for r in preview["rules"] if not r["accepted_matches_fixture"]] == [4]
    with pytest.raises(SeasonSetupError, match="Collingwood"):
        accept_draft_order(
            database, afl, season.season_id, [e.season_entry_id for e in entries], actor=SCORER, reason=REASON
        )
    assert DraftRepository(database).status(season.season_id) is None
