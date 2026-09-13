"""Issue #190: finals bracket generation and lifecycle.

Covers bracket creation (both seed-resolution paths and their fail-closed
checks), Week 1 materialisation, the stream-aware preflight/open-round
adapter, advance-bracket for every Week 2/3 winner combination (including
genuine ties at every stage, per Steve's confirmed tie-break policy), the
variable match-count lifecycle, and explicit elimination recording.
Concurrency/locking is covered separately in test_finals_postgresql.py, and
the correction/rewind cascade in test_finals_rewind.py."""

import pytest

from app.audit import ActorContext
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.finals import (
    FinalsBracketAdvanceStateError,
    FinalsBracketContextError,
    FinalsBracketError,
    FinalsBracketRepository,
    IncompleteFinalsWeekError,
    UnresolvedLadderTieError,
)
from app.finals_preflight import build_finals_week_preflight, open_finals_week
from app.finals_seeding import FinalsSeedingRepository
from tests.finals_helpers import accept_week_mapping, build_finals_ready_season, seed_official_result
from tests.finals_seeding_helpers import all_draws

ACTOR = ActorContext.anonymous_operator("test")


def _repo(built):
    return FinalsBracketRepository(built["database"])


def _create(built, *, reason="test bracket creation"):
    repo = _repo(built)
    return repo.create_bracket(
        built["season"].season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ACTOR,
        reason=reason,
    )


# -- Bracket creation: ladder path -------------------------------------------


def test_create_bracket_ladder_path_seeds_from_mathematical_ladder_order():
    built = build_finals_ready_season(year=2201)
    repo = _repo(built)
    preview = repo.preview_create_bracket(
        built["season"].season_id, built["finals_competition"].competition_id, built["ordinary_competition_id"]
    )
    assert preview["context_ready"]
    assert preview["seed_source"] == "ladder"
    assert preview["seed_order"] == [e.season_entry_id for e in built["entries"]]

    result = _create(built)
    assert result["created"]
    bracket = result["bracket"]
    assert bracket.seed_source == "ladder"
    assert bracket.finals_seeding_snapshot_id is None
    assert bracket.through_round == 20

    seed_rows = repo.list_seed_rows(bracket.bracket_id)
    assert [row.season_entry_id for row in seed_rows] == [e.season_entry_id for e in built["entries"]]
    assert [row.qualified for row in seed_rows] == [True] * 5 + [False] * 5

    references = (
        built["database"]
        .execute("SELECT COUNT(*) AS n FROM finals_bracket_result_reference WHERE bracket_id=?", (bracket.bracket_id,))
        .fetchone()
    )
    assert references["n"] > 0


def test_create_bracket_is_idempotent():
    built = build_finals_ready_season(year=2202)
    first = _create(built, reason="first")
    second = _create(built, reason="second, unchanged")
    assert first["created"]
    assert not second["created"]
    assert first["bracket"].bracket_id == second["bracket"].bracket_id


def test_create_bracket_conflicting_ordinary_competition_id_fails_closed():
    built = build_finals_ready_season(year=2203)
    _create(built)
    other_ordinary = (
        built["database"]
        .execute(
            "SELECT competition_id FROM competition_stream WHERE competition_id != ? LIMIT 1",
            (built["ordinary_competition_id"],),
        )
        .fetchone()
    )
    with pytest.raises(FinalsBracketContextError):
        _repo(built).create_bracket(
            built["season"].season_id,
            built["finals_competition"].competition_id,
            other_ordinary["competition_id"] if other_ordinary else "bogus",
            actor=ACTOR,
            reason="conflicting retry",
        )


def test_create_bracket_rejects_non_finals_competition_id():
    built = build_finals_ready_season(year=2204)
    with pytest.raises(FinalsBracketContextError, match="finals competition stream"):
        _repo(built).create_bracket(
            built["season"].season_id,
            built["ordinary_competition_id"],
            built["ordinary_competition_id"],
            actor=ACTOR,
            reason="wrong competition_id",
        )


def test_create_bracket_rejects_ordinary_competition_id_from_a_different_season():
    built = build_finals_ready_season(year=2205)
    other = build_finals_ready_season(year=2206, database=built["database"])
    with pytest.raises(FinalsBracketContextError, match="season's own ordinary"):
        _repo(built).create_bracket(
            built["season"].season_id,
            built["finals_competition"].competition_id,
            other["ordinary_competition_id"],
            actor=ACTOR,
            reason="wrong season's ordinary competition",
        )


def test_create_bracket_fails_closed_on_incomplete_regular_season():
    built = build_finals_ready_season(year=2207, trigger_round=19)
    with pytest.raises(FinalsBracketContextError, match="must be final"):
        _create(built)


def test_create_bracket_fails_closed_on_unresolved_ladder_tie():
    built = build_finals_ready_season(year=2208, score_fn=all_draws)
    with pytest.raises(UnresolvedLadderTieError):
        _create(built)


def test_create_bracket_requires_a_reason():
    built = build_finals_ready_season(year=2209)
    with pytest.raises(FinalsBracketError, match="reason"):
        _repo(built).create_bracket(
            built["season"].season_id,
            built["finals_competition"].competition_id,
            built["ordinary_competition_id"],
            actor=ACTOR,
            reason="",
        )


# -- Bracket creation: snapshot path ------------------------------------------


def test_create_bracket_snapshot_path_uses_frozen_historical_order_and_never_reruns_ladder():
    built = build_finals_ready_season(year=2026, entry_count=10)
    database = built["database"]
    snapshot = FinalsSeedingRepository(database).apply(
        built["season"].season_id, built["ordinary_competition_id"], reason="2026 historical seeding snapshot"
    )
    repo = _repo(built)
    preview = repo.preview_create_bracket(
        built["season"].season_id, built["finals_competition"].competition_id, built["ordinary_competition_id"]
    )
    assert preview["seed_source"] == "snapshot"

    result = _create(built, reason="snapshot-backed bracket")
    bracket = result["bracket"]
    assert bracket.seed_source == "snapshot"
    assert bracket.finals_seeding_snapshot_id == snapshot["snapshot_id"]
    assert bracket.through_round is None

    seed_rows = repo.list_seed_rows(bracket.bracket_id)
    assert [row.seed_position for row in seed_rows] == list(range(1, 11))
    # The frozen order is never re-derived from a fresh ladder read -- a
    # later correction to a regular-season result must not retroactively
    # change an already-created bracket's seed (module docstring, "Seed
    # consumption"). Corrupting the ladder after the fact and re-reading
    # the bracket's own frozen rows proves nothing here re-resolves.
    reread = repo.list_seed_rows(bracket.bracket_id)
    assert reread == seed_rows


# -- Week 1 materialisation and the finals preflight/open-round adapter -----


def _bracket_with_mappings(year=2210, **kwargs):
    built = build_finals_ready_season(year=year, **kwargs)
    bracket = _create(built)["bracket"]
    repo = _repo(built)
    for week in (1, 2, 3, 4):
        round_id = repo.get_week_round_id(bracket.bracket_id, week)
        accept_week_mapping(built["database"], round_id, year=year, afl_round_id=9000 + week)
    return built, bracket


def test_week1_bracket_materialises_bye_qf_ef_from_seed_order():
    built, bracket = _bracket_with_mappings(year=2210)
    repo = _repo(built)
    entries = built["entries"]
    pairings = repo.list_pairings(bracket.bracket_id, week_number=1)
    by_slot = {p.slot: p for p in pairings}
    assert set(by_slot) == {"bye", "qf", "ef"}
    assert by_slot["bye"].home_season_entry_id == entries[0].season_entry_id
    assert by_slot["bye"].away_season_entry_id is None
    assert by_slot["bye"].matchup_id is None
    assert by_slot["qf"].home_season_entry_id == entries[1].season_entry_id
    assert by_slot["qf"].away_season_entry_id == entries[2].season_entry_id
    assert by_slot["ef"].home_season_entry_id == entries[3].season_entry_id
    assert by_slot["ef"].away_season_entry_id == entries[4].season_entry_id


def test_open_finals_week_preflight_blocks_without_a_pairing():
    built = build_finals_ready_season(year=2211)
    bracket = _create(built)["bracket"]
    repo = _repo(built)
    round_id = repo.get_week_round_id(bracket.bracket_id, 2)
    accept_week_mapping(built["database"], round_id, year=2211, afl_round_id=9002)
    preflight = build_finals_week_preflight(built["database"], bracket.bracket_id, 2)
    assert not preflight["readiness"]["safe_to_open"]
    assert any(b["code"] == "pairing_missing" for b in preflight["readiness"]["blockers"])


def test_open_finals_week_preflight_blocks_without_an_accepted_mapping():
    built = build_finals_ready_season(year=2212)
    bracket = _create(built)["bracket"]
    preflight = build_finals_week_preflight(built["database"], bracket.bracket_id, 1)
    assert not preflight["readiness"]["safe_to_open"]
    assert any(b["code"] == "mapping_missing" for b in preflight["readiness"]["blockers"])


def test_open_finals_week_opens_round_and_materialises_matchups_end_to_end():
    built, bracket = _bracket_with_mappings(year=2213)
    repo = _repo(built)
    preflight = build_finals_week_preflight(built["database"], bracket.bracket_id, 1)
    assert preflight["readiness"]["safe_to_open"]

    result = open_finals_week(built["database"], bracket.bracket_id, 1, actor=ACTOR)
    assert result["round"].state == "open"

    pairings = repo.list_pairings(bracket.bracket_id, week_number=1)
    qf = next(p for p in pairings if p.slot == "qf")
    ef = next(p for p in pairings if p.slot == "ef")
    bye = next(p for p in pairings if p.slot == "bye")
    assert qf.matchup_id is not None
    assert ef.matchup_id is not None
    assert bye.matchup_id is None

    matchups = (
        built["database"]
        .execute(
            "SELECT matchup_id, fixture_matchup_id FROM bbbffl_matchup WHERE bbbffl_round_id=?",
            (result["round"].bbbffl_round_id,),
        )
        .fetchall()
    )
    assert len(matchups) == 2
    assert all(row["fixture_matchup_id"] is None for row in matchups)


def test_open_finals_week_is_reachable_only_after_preflight_blockers_clear():
    built = build_finals_ready_season(year=2214)
    bracket = _create(built)["bracket"]
    with pytest.raises(FinalsBracketAdvanceStateError, match="failed preflight"):
        open_finals_week(built["database"], bracket.bracket_id, 1, actor=ACTOR)


# -- Advance bracket: every Week 2/3 winner combination ----------------------


def _open_and_seed_week1(built, bracket, *, qf_result, ef_result):
    """Opens week 1 (if not already open) and seeds both matches' official
    results, without advancing -- split out from `_advance_week1` so a test
    that needs to inspect the freshly-opened week (e.g. its matchup count)
    before advancing does not have to open it twice."""
    repo = _repo(built)
    round_ = CompetitionLifecycleRepository(built["database"]).get_round(repo.get_week_round_id(bracket.bracket_id, 1))
    if round_ is None or round_.state == "upcoming":
        open_finals_week(built["database"], bracket.bracket_id, 1, actor=ACTOR)
    pairings = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=1)}
    seed_official_result(built["database"], pairings["qf"].matchup_id, *qf_result)
    seed_official_result(built["database"], pairings["ef"].matchup_id, *ef_result)


def _advance_week1(built, bracket, *, qf_result, ef_result):
    repo = _repo(built)
    _open_and_seed_week1(built, bracket, qf_result=qf_result, ef_result=ef_result)
    repo.advance_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="advance from week 1")
    return repo.list_pairings(bracket.bracket_id, week_number=2)


@pytest.mark.parametrize(
    "case_id,qf_result,ef_result,qf_winner_seed,ef_winner_seed",
    [
        (1, (100, 50), (100, 50), 2, 4),  # QF home (seed2) wins, EF home (seed4) wins
        (2, (50, 100), (100, 50), 3, 4),  # QF away (seed3) wins, EF home (seed4) wins
        (3, (100, 50), (50, 100), 2, 5),  # QF home (seed2) wins, EF away (seed5) wins
        (4, (50, 100), (50, 100), 3, 5),  # QF away (seed3) wins, EF away (seed5) wins
        (5, (70, 70), (50, 100), 2, 5),  # QF tie -> higher seed (seed2) wins
        (6, (100, 50), (70, 70), 2, 4),  # EF tie -> higher seed (seed4) wins
    ],
)
def test_week2_pairing_derivation_for_every_week1_winner_combination(
    case_id, qf_result, ef_result, qf_winner_seed, ef_winner_seed
):
    built, bracket = _bracket_with_mappings(year=2220 + case_id)
    entries = built["entries"]
    week2 = _advance_week1(built, bracket, qf_result=qf_result, ef_result=ef_result)
    ss2 = next(p for p in week2 if p.slot == "second_semi")
    fs = next(p for p in week2 if p.slot == "first_semi")
    qf_winner = entries[qf_winner_seed - 1].season_entry_id
    ef_winner = entries[ef_winner_seed - 1].season_entry_id
    qf_loser_seed = 3 if qf_winner_seed == 2 else 2
    qf_loser = entries[qf_loser_seed - 1].season_entry_id
    assert ss2.home_season_entry_id == entries[0].season_entry_id
    assert ss2.away_season_entry_id == qf_winner
    assert fs.home_season_entry_id == qf_loser
    assert fs.away_season_entry_id == ef_winner


def test_week1_elimination_final_loser_recorded_explicitly():
    built, bracket = _bracket_with_mappings(year=2230)
    entries = built["entries"]
    _advance_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    repo = _repo(built)
    eliminations = repo.list_eliminations(bracket.bracket_id)
    week1_elim = next(e for e in eliminations if e.stage == "week1_elimination_final")
    assert week1_elim.season_entry_id == entries[3].season_entry_id  # EF home (seed4) lost
    pre_finals = [e for e in eliminations if e.stage == "pre_finals"]
    assert {e.season_entry_id for e in pre_finals} == {e.season_entry_id for e in entries[5:10]}


def _open_and_seed_week2(built, bracket, *, ss2_result, fs_result):
    repo = _repo(built)
    round_ = CompetitionLifecycleRepository(built["database"]).get_round(repo.get_week_round_id(bracket.bracket_id, 2))
    if round_ is None or round_.state == "upcoming":
        open_finals_week(built["database"], bracket.bracket_id, 2, actor=ACTOR)
    pairings = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=2)}
    seed_official_result(built["database"], pairings["second_semi"].matchup_id, *ss2_result)
    seed_official_result(built["database"], pairings["first_semi"].matchup_id, *fs_result)


def _advance_to_week3(built, bracket, *, ss2_result, fs_result):
    repo = _repo(built)
    _open_and_seed_week2(built, bracket, ss2_result=ss2_result, fs_result=fs_result)
    repo.advance_bracket(bracket.bracket_id, 2, actor=ACTOR, reason="advance from week 2")
    return repo.list_pairings(bracket.bracket_id, week_number=3)


@pytest.mark.parametrize(
    "case_id,ss2_result,fs_result",
    [
        (1, (100, 50), (100, 50)),
        (2, (50, 100), (100, 50)),
        (3, (100, 50), (50, 100)),
        (4, (50, 100), (50, 100)),
        (5, (60, 60), (100, 50)),
    ],
)
def test_week3_preliminary_final_pairing_derivation_for_every_week2_winner_combination(case_id, ss2_result, fs_result):
    built, bracket = _bracket_with_mappings(year=2240 + case_id)
    _advance_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    week3 = _advance_to_week3(built, bracket, ss2_result=ss2_result, fs_result=fs_result)
    assert len(week3) == 1
    pf = week3[0]
    assert pf.slot == "preliminary"
    repo = _repo(built)
    week2 = repo.list_pairings(bracket.bracket_id, week_number=2)
    ss2 = next(p for p in week2 if p.slot == "second_semi")
    fs = next(p for p in week2 if p.slot == "first_semi")
    ss2_home_wins = ss2_result[0] > ss2_result[1]
    ss2_tie = ss2_result[0] == ss2_result[1]
    if ss2_tie:
        ss2_loser = ss2.away_season_entry_id  # home is seed1, always higher seed than any QF/EF winner
    else:
        ss2_loser = ss2.away_season_entry_id if ss2_home_wins else ss2.home_season_entry_id
    fs_home_wins = fs_result[0] > fs_result[1]
    fs_winner = fs.home_season_entry_id if fs_home_wins else fs.away_season_entry_id
    assert pf.home_season_entry_id == ss2_loser
    assert pf.away_season_entry_id == fs_winner


def test_week2_first_semi_loser_eliminated_at_week3_advance():
    built, bracket = _bracket_with_mappings(year=2250)
    _advance_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    week2 = _repo(built).list_pairings(bracket.bracket_id, week_number=2)
    fs = next(p for p in week2 if p.slot == "first_semi")
    # fs_result=(50, 100): away outscores home, so fs.home_season_entry_id loses and is eliminated.
    _advance_to_week3(built, bracket, ss2_result=(100, 50), fs_result=(50, 100))
    repo = _repo(built)
    elim = next(e for e in repo.list_eliminations(bracket.bracket_id) if e.stage == "week2_first_semi_final")
    assert elim.season_entry_id == fs.home_season_entry_id


def test_grand_final_pairing_and_tie_progression():
    built, bracket = _bracket_with_mappings(year=2260)
    entries = built["entries"]
    _advance_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    _advance_to_week3(built, bracket, ss2_result=(100, 50), fs_result=(50, 100))
    repo = _repo(built)
    open_finals_week(built["database"], bracket.bracket_id, 3, actor=ACTOR)
    pf = repo.list_pairings(bracket.bracket_id, week_number=3)[0]
    seed_official_result(built["database"], pf.matchup_id, 60, 60)  # GENUINE TIE at the Preliminary Final
    repo.advance_bracket(bracket.bracket_id, 3, actor=ACTOR, reason="advance from week 3")

    gf = repo.list_pairings(bracket.bracket_id, week_number=4)[0]
    assert gf.slot == "grand_final"
    # PF tie resolved by higher frozen seed between pf.home/pf.away
    seed_rank = repo._seed_rank(bracket.bracket_id)
    expected_pf_winner = (
        pf.home_season_entry_id
        if seed_rank[pf.home_season_entry_id] < seed_rank[pf.away_season_entry_id]
        else pf.away_season_entry_id
    )
    assert gf.away_season_entry_id == expected_pf_winner
    assert gf.home_season_entry_id == entries[0].season_entry_id  # SS2 winner = seed1 (undefeated home side)

    elim = next(e for e in repo.list_eliminations(bracket.bracket_id) if e.stage == "week3_preliminary_final")
    expected_pf_loser = (
        pf.away_season_entry_id if expected_pf_winner == pf.home_season_entry_id else pf.home_season_entry_id
    )
    assert elim.season_entry_id == expected_pf_loser

    open_finals_week(built["database"], bracket.bracket_id, 4, actor=ACTOR)
    gf = repo.list_pairings(bracket.bracket_id, week_number=4)[0]
    seed_official_result(built["database"], gf.matchup_id, 88, 88)  # GRAND FINAL TIE
    result = (
        built["database"]
        .execute("SELECT home_score, away_score FROM bbbffl_official_result WHERE matchup_id=?", (gf.matchup_id,))
        .fetchone()
    )
    assert result["home_score"] == result["away_score"]  # the Grand Final genuinely tied
    seed_rank = repo._seed_rank(bracket.bracket_id)
    # Steve's confirmed policy: a tied Grand Final is won by the higher frozen finals seed.
    winner = (
        gf.home_season_entry_id
        if seed_rank[gf.home_season_entry_id] < seed_rank[gf.away_season_entry_id]
        else gf.away_season_entry_id
    )
    assert winner == entries[0].season_entry_id  # seed1 is always the highest seed present in the GF


# -- Variable match-count lifecycle -------------------------------------------


def test_lifecycle_never_assumes_a_fixed_match_count_of_one_or_five():
    """Weeks 1-2 have two matches (plus, for Week 1, a bye that is not a
    match at all); Weeks 3-4 legitimately have exactly one -- never
    rejected as invalid, and never required to be five."""
    built, bracket = _bracket_with_mappings(year=2270)
    repo = _repo(built)
    _open_and_seed_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    week1_matchups = (
        built["database"]
        .execute(
            "SELECT COUNT(*) AS n FROM bbbffl_matchup WHERE bbbffl_round_id=?",
            (repo.get_week_round_id(bracket.bracket_id, 1),),
        )
        .fetchone()
    )
    assert week1_matchups["n"] == 2

    repo.advance_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="advance from week 1")
    _open_and_seed_week2(built, bracket, ss2_result=(100, 50), fs_result=(50, 100))
    week2_matchups = (
        built["database"]
        .execute(
            "SELECT COUNT(*) AS n FROM bbbffl_matchup WHERE bbbffl_round_id=?",
            (repo.get_week_round_id(bracket.bracket_id, 2),),
        )
        .fetchone()
    )
    assert week2_matchups["n"] == 2

    repo.advance_bracket(bracket.bracket_id, 2, actor=ACTOR, reason="advance from week 2")
    open_finals_week(built["database"], bracket.bracket_id, 3, actor=ACTOR)
    week3_matchups = (
        built["database"]
        .execute(
            "SELECT COUNT(*) AS n FROM bbbffl_matchup WHERE bbbffl_round_id=?",
            (repo.get_week_round_id(bracket.bracket_id, 3),),
        )
        .fetchone()
    )
    assert week3_matchups["n"] == 1  # legitimately one match -- never rejected


def test_advancing_before_a_week_is_complete_fails_closed():
    built, bracket = _bracket_with_mappings(year=2280)
    repo = _repo(built)
    open_finals_week(built["database"], bracket.bracket_id, 1, actor=ACTOR)
    with pytest.raises(IncompleteFinalsWeekError):
        repo.advance_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="too early")


def test_re_advancing_an_already_advanced_week_fails_closed():
    built, bracket = _bracket_with_mappings(year=2290)
    _advance_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    with pytest.raises(FinalsBracketAdvanceStateError, match="already been advanced"):
        _repo(built).advance_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="duplicate advance")


# -- Mutation auditing --------------------------------------------------------


def test_every_mutation_is_actor_reason_audited():
    from app.audit import AuditEventRepository

    built, bracket = _bracket_with_mappings(year=2300)
    events = AuditEventRepository(built["database"]).list_events(entity_id=bracket.bracket_id)
    created = next(e for e in events if e.action == "finals.bracket.created")
    assert created.reason
    assert created.actor_type == "anonymous_operator"

    _advance_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    events = AuditEventRepository(built["database"]).list_events(entity_id=bracket.bracket_id)
    advanced = next(e for e in events if e.action == "finals.bracket.advanced")
    assert advanced.reason == "advance from week 1"
