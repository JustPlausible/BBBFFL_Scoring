"""Sporting-rule and persisted effective-result tests for the ladder."""

from decimal import Decimal

from app.competition_lifecycle import CompetitionLifecycleRepository
from app.ladder import LadderRepository, OfficialMatchupInput, calculate_ladder
from app.round_mapping import RoundMappingRepository
from app.season import SeasonRepository
from tests.round_review_helpers import full_round, progress_to_review
from tests.test_competition_lifecycle import KnownRound


def result(round_number, matchup_id, home, away, home_score, away_score, version=1):
    return OfficialMatchupInput(round_number, matchup_id, version, home, away, Decimal(home_score), Decimal(away_score))


def by_id(snapshot):
    return {row.season_entry_id: row for row in snapshot.rows}


def round_scope(database, round_id):
    return database.execute(
        "SELECT c.season_id, c.competition_id FROM bbbffl_round r "
        "JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (round_id,),
    ).fetchone()


def test_win_draw_accumulation_percentage_and_ppg():
    snapshot = calculate_ladder(
        "s",
        "ordinary",
        2,
        ("a", "b"),
        (result(1, "m1", "a", "b", "120", "100"), result(2, "m2", "b", "a", "90", "90")),
    )
    a, b = by_id(snapshot)["a"], by_id(snapshot)["b"]
    assert (a.played, a.wins, a.draws, a.losses, a.competition_points) == (2, 1, 1, 0, 6)
    assert (b.played, b.wins, b.draws, b.losses, b.competition_points) == (2, 0, 1, 1, 2)
    assert (a.points_for, a.points_against, a.percentage, a.points_per_game) == (
        Decimal(210),
        Decimal(190),
        Decimal(210) / Decimal(190) * 100,
        Decimal(105),
    )


def test_ordering_uses_points_then_percentage_then_points_for():
    # A has fewer PF and lower percentage but a win makes points decisive.
    points = calculate_ladder(
        "s",
        "ordinary",
        1,
        ("a", "b", "c", "d"),
        (result(1, "m1", "a", "b", "2", "1"), result(1, "m2", "c", "d", "100", "100")),
    )
    assert [row.season_entry_id for row in points.rows][:2] == ["a", "c"]

    percentage = calculate_ladder(
        "s",
        "ordinary",
        1,
        ("a", "b", "c", "d"),
        (result(1, "m1", "a", "b", "100", "50"), result(1, "m2", "c", "d", "90", "60")),
    )
    assert [row.season_entry_id for row in percentage.rows][:2] == ["a", "c"]

    # Equal points and percentage (200%); PF is the third criterion.
    points_for = calculate_ladder(
        "s",
        "ordinary",
        1,
        ("a", "b", "c", "d"),
        (result(1, "m1", "a", "b", "100", "50"), result(1, "m2", "c", "d", "120", "60")),
    )
    assert [row.season_entry_id for row in points_for.rows][:2] == ["c", "a"]


def test_exact_equality_is_an_explicit_shared_sporting_rank():
    snapshot = calculate_ladder("s", "ordinary", 1, ("z", "a"), (result(1, "m", "z", "a", "88", "88"),))
    assert [row.rank for row in snapshot.rows] == [1, 1]
    assert all(row.tied and row.tie_group == ("a", "z") for row in snapshot.rows)


def test_round_boundary_and_rebuild_are_deterministic_and_season_labelled():
    inputs = (result(2, "m2", "b", "a", "10", "20"), result(1, "m1", "a", "b", "5", "10"))
    after_one = calculate_ladder("2026", "ordinary-2026", 1, ("b", "a"), inputs)
    assert by_id(after_one)["a"].played == 1
    assert len(after_one.result_references) == 1
    assert after_one.result_references[0].matchup_id == "m1"
    assert calculate_ladder("2026", "ordinary-2026", 2, ("b", "a"), inputs) == calculate_ladder(
        "2026", "ordinary-2026", 2, ("a", "b"), inputs
    )
    assert calculate_ladder("2027", "ordinary-2027", 2, ("a", "b"), ()).season_id == "2027"


def test_2026_documented_rules_replay_has_diagnosable_round_checkpoints():
    """Synthetic scores: the workbook is unavailable, but its confirmed
    arithmetic/order rules are exercised at more than the final checkpoint."""
    evidence = (
        result(1, "2026-r1-a", "a", "b", "100", "80"),
        result(1, "2026-r1-b", "c", "d", "90", "90"),
        result(2, "2026-r2-a", "a", "c", "70", "110"),
        result(2, "2026-r2-b", "b", "d", "95", "85"),
    )
    expected = {
        1: {
            "a": (1, 1, 0, 0, 100, 80, 4, Decimal(125), 1, False),
            "c": (1, 0, 1, 0, 90, 90, 2, Decimal(100), 2, True),
            "d": (1, 0, 1, 0, 90, 90, 2, Decimal(100), 2, True),
            "b": (1, 0, 0, 1, 80, 100, 0, Decimal(80), 4, False),
        },
        2: {
            "c": (2, 1, 1, 0, 200, 160, 6, Decimal(125), 1, False),
            "b": (2, 1, 0, 1, 175, 185, 4, Decimal(175) / Decimal(185) * 100, 2, False),
            "a": (2, 1, 0, 1, 170, 190, 4, Decimal(170) / Decimal(190) * 100, 3, False),
            "d": (2, 0, 1, 1, 175, 185, 2, Decimal(175) / Decimal(185) * 100, 4, False),
        },
    }
    for boundary, checkpoint in expected.items():
        snapshot = calculate_ladder("2026", "ordinary-2026", boundary, ("a", "b", "c", "d"), evidence)
        actual = by_id(snapshot)
        for entry_id, values in checkpoint.items():
            row = actual[entry_id]
            observed = (
                row.played,
                row.wins,
                row.draws,
                row.losses,
                row.points_for,
                row.points_against,
                row.competition_points,
                row.percentage,
                row.rank,
                row.tied,
            )
            assert observed == values, f"Round {boundary}, entry {entry_id}: expected {values}, got {observed}"
        assert len(snapshot.result_references) == boundary * 2


def test_repository_ignores_unfinalised_then_uses_only_effective_correction_and_keeps_history():
    db, lifecycle, round_, entries, _, _ = full_round(year=2026, afl_round=959)
    progress_to_review(lifecycle, round_.bbbffl_round_id)
    ladder = LadderRepository(db)
    scope = round_scope(db, round_.bbbffl_round_id)
    before = ladder.snapshot(scope["competition_id"], 1)
    assert all(row.played == 0 for row in before.rows)  # calculations/live/review state cannot leak in

    matchups = lifecycle.list_matchups(round_.bbbffl_round_id)
    scores = {matchup.matchup_id: (100, 50) for matchup in matchups}
    lifecycle.publish_results(round_.bbbffl_round_id, scores)
    first = ladder.snapshot(scope["competition_id"], 1)
    focus = matchups[0]
    assert by_id(first)[focus.home_season_entry_id].competition_points == 4

    lifecycle.correct_matchup_result(focus.matchup_id, 40, 50, reason="authorised replay correction")
    corrected = ladder.snapshot(scope["competition_id"], 1)
    assert by_id(corrected)[focus.home_season_entry_id].competition_points == 0
    assert by_id(corrected)[focus.away_season_entry_id].competition_points == 4
    assert next(ref for ref in corrected.result_references if ref.matchup_id == focus.matchup_id).official_version == 2
    assert [version.version for version in lifecycle.result_history(focus.matchup_id)] == [1, 2]
    assert lifecycle.effective_result(focus.matchup_id).version == 2

    # A second season in the same database cannot see 2026's five results.
    _, _, other_round, _, _, _ = full_round(db=db, year=2027, afl_round=960)
    other_scope = round_scope(db, other_round.bbbffl_round_id)
    other = ladder.snapshot(other_scope["competition_id"], 1)
    assert all(row.played == 0 for row in other.rows)
    assert other.result_references == ()


def test_repository_isolates_two_ordinary_competition_streams_in_one_season():
    db, lifecycle, round_a, entries, _, _ = full_round(year=2028, afl_round=961)
    scope_a = round_scope(db, round_a.bbbffl_round_id)

    rules_id = db.execute(
        "SELECT rules_version_id FROM competition_stream WHERE competition_id=?",
        (scope_a["competition_id"],),
    ).fetchone()["rules_version_id"]
    seasons = SeasonRepository(db)
    competition_b = seasons.create_competition(scope_a["season_id"], rules_id, "ordinary-b", "Ordinary B", "ordinary")
    round_b = seasons.create_round(competition_b.competition_id, "round-1", "Round 1", 1)
    RoundMappingRepository(db).accept(round_b.bbbffl_round_id, 2028, 962, KnownRound(2028, 962))
    lifecycle_b = CompetitionLifecycleRepository(db)
    lifecycle_b.create_ordinary_round(round_b.bbbffl_round_id)

    for repository, round_id in (
        (lifecycle, round_a.bbbffl_round_id),
        (lifecycle_b, round_b.bbbffl_round_id),
    ):
        progress_to_review(repository, round_id)
    matchups_a = lifecycle.list_matchups(round_a.bbbffl_round_id)
    matchups_b = lifecycle_b.list_matchups(round_b.bbbffl_round_id)
    lifecycle.publish_results(round_a.bbbffl_round_id, {match.matchup_id: (100, 50) for match in matchups_a})
    lifecycle_b.publish_results(round_b.bbbffl_round_id, {match.matchup_id: (40, 80) for match in matchups_b})

    snapshot_a = LadderRepository(db).snapshot(scope_a["competition_id"], 1)
    snapshot_b = LadderRepository(db).snapshot(competition_b.competition_id, 1)
    refs_a = {reference.matchup_id for reference in snapshot_a.result_references}
    refs_b = {reference.matchup_id for reference in snapshot_b.result_references}

    assert snapshot_a.competition_id == scope_a["competition_id"]
    assert snapshot_b.competition_id == competition_b.competition_id
    assert {row.season_entry_id for row in snapshot_a.rows} == {entry.season_entry_id for entry in entries}
    assert {row.season_entry_id for row in snapshot_b.rows} == {entry.season_entry_id for entry in entries}
    assert refs_a == {match.matchup_id for match in matchups_a}
    assert refs_b == {match.matchup_id for match in matchups_b}
    assert refs_a.isdisjoint(refs_b)
    assert all(row.played == 1 for row in snapshot_a.rows + snapshot_b.rows)
    assert all(by_id(snapshot_a)[match.home_season_entry_id].competition_points == 4 for match in matchups_a)
    assert all(by_id(snapshot_b)[match.away_season_entry_id].competition_points == 4 for match in matchups_b)
