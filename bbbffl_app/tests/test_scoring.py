import pytest

from app.scoring import PlayerStats, score_position


def test_forward_scores_six_per_goal_plus_behinds():
    stats = PlayerStats(goals=3, behinds=2)
    assert score_position("Forward1", stats) == 3 * 6 + 2
    assert score_position("Forward2", stats) == score_position("Forward3", stats)


def test_midfield_scores_total_disposals():
    stats = PlayerStats(disposals=27, goals=1, behinds=1)
    assert score_position("Midfield1", stats) == 27


def test_ruck_scores_marks_plus_hitouts():
    stats = PlayerStats(marks=4, hitouts=31)
    assert score_position("Ruck", stats) == 35


def test_tackler_scores_six_per_tackle():
    stats = PlayerStats(tackles=5)
    assert score_position("Tackler", stats) == 30


def test_zero_stats_score_zero():
    stats = PlayerStats()
    for position in ("Forward1", "Midfield1", "Ruck", "Tackler"):
        assert score_position(position, stats) == 0


def test_interchange_cannot_be_scored_directly():
    with pytest.raises(ValueError):
        score_position("Interchange", PlayerStats(goals=5))


def test_only_null_inputs_required_by_the_position_withhold_a_score():
    partial = PlayerStats(
        goals=2,
        behinds=1,
        disposals=20,
        marks=3,
        hitouts=None,
        tackles=4,
    )
    assert score_position("Forward1", partial) == 13
    assert score_position("Midfield1", partial) == 20
    assert score_position("Ruck", partial) is None
    assert score_position("Tackler", partial) == 24
