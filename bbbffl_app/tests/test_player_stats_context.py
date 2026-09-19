"""Issue #181: `app.player_stats_context.PlayerStatsContext`'s read-only
scoring-context aggregate, over real calculated `bbbffl_matchup_calculation`
snapshots (reusing `tests.test_calculations.setup_round`, not a hand-rolled
snapshot format that could drift from the real one)."""

from app.calculations import MatchupCalculationService
from app.db import transaction
from app.player_stats_context import PlayerStatsContext
from app.season import _now
from tests.test_calculations import Facts, setup_round


def _season_id_for_round(db, round_id):
    row = db.execute(
        "SELECT c.season_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (round_id,),
    ).fetchone()
    return row["season_id"]


def test_current_season_points_aggregates_calculated_rounds():
    db, lifecycle, round_, stats = setup_round(year=8001)
    season_id = _season_id_for_round(db, round_.bbbffl_round_id)
    MatchupCalculationService(db, Facts(stats)).calculate_round(
        round_.bbbffl_round_id, upstream_revision="stats-1", observed_at=_now()
    )

    result = PlayerStatsContext(db).current_season_points(season_id)
    # `setup_round` gives every entry but the first a scoring F1 player with
    # `goals=index+1` -- each such player appears in exactly one match, so
    # every one of them has exactly one game played with a positive score.
    assert result, "expected at least one aggregated player"
    for canonical_id, entry in result.items():
        assert entry.games == 1
        assert entry.total_points > 0
        assert entry.average_points == entry.total_points


def test_current_season_points_is_empty_for_a_season_with_no_calculated_rounds():
    db, lifecycle, round_, stats = setup_round(year=8002)
    season_id = _season_id_for_round(db, round_.bbbffl_round_id)
    assert PlayerStatsContext(db).current_season_points(season_id) == {}


def test_previous_completed_season_points_finds_the_most_recent_earlier_completed_season():
    db, lifecycle, round_, stats = setup_round(year=8010)
    previous_season_id = _season_id_for_round(db, round_.bbbffl_round_id)
    MatchupCalculationService(db, Facts(stats)).calculate_round(
        round_.bbbffl_round_id, upstream_revision="stats-1", observed_at=_now()
    )
    with transaction(db) as conn:
        conn.execute("UPDATE bbbffl_season SET lifecycle_state='completed' WHERE season_id=?", (previous_season_id,))

    db2, lifecycle2, round2, stats2 = setup_round(db, year=8011)
    current_season_id = _season_id_for_round(db, round2.bbbffl_round_id)

    year, result = PlayerStatsContext(db).previous_completed_season_points(current_season_id)
    assert year == 8010
    assert result, "expected the previous completed season's aggregated stats"


def test_previous_completed_season_points_is_none_with_no_earlier_completed_season():
    db, lifecycle, round_, stats = setup_round(year=8020)
    season_id = _season_id_for_round(db, round_.bbbffl_round_id)
    year, result = PlayerStatsContext(db).previous_completed_season_points(season_id)
    assert year is None
    assert result == {}
