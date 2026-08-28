"""Unit coverage for the shared display-only football (Goals.Behinds/Total)
presentation model in app/presentation.py -- consumed by both the Grand
Final and SuperScore views (public and Admin) via app/service.py, per the
scoreboard presentation brief.
"""

from app.presentation import football_score_for_position, format_football_line

# -- format_football_line ----------------------------------------------------


def test_format_football_line_whole_numbers():
    assert format_football_line(4, 5) == "4.5"
    assert format_football_line(4, 0) == "4.0"
    assert format_football_line(4, 1) == "4.1"
    assert format_football_line(0, 0) == "0.0"


def test_format_football_line_team_aggregate_example():
    assert format_football_line(26, 13) == "26.13"


# -- Forward: actual AFL goals/behinds ---------------------------------------


def test_forward_uses_actual_afl_goals_and_behinds_with_no_override():
    fb = football_score_for_position("Forward1", effective_score=24, stat_goals=4, stat_behinds=0)
    assert (fb.goals, fb.behinds) == (4, 0)
    assert fb.is_actual_afl is True
    assert fb.line == "4.0"


def test_forward_actual_goals_and_behinds_worked_examples():
    # Patrick Voss / Jack Higgins / Jake Waterman from the task brief.
    voss = football_score_for_position("Forward2", 24, stat_goals=4, stat_behinds=0)
    higgins = football_score_for_position("Forward1", 10, stat_goals=1, stat_behinds=4)
    waterman = football_score_for_position("Forward3", 20, stat_goals=3, stat_behinds=2)
    assert (voss.goals, voss.behinds, voss.line) == (4, 0, "4.0")
    assert (higgins.goals, higgins.behinds, higgins.line) == (1, 4, "1.4")
    assert (waterman.goals, waterman.behinds, waterman.line) == (3, 2, "3.2")
    assert all(fb.is_actual_afl for fb in (voss, higgins, waterman))


def test_forward_with_no_stat_line_falls_back_to_conversion():
    """An unnamed/vacant/DNP Forward (or one whose match hasn't produced
    stats yet) has no real goals/behinds to show -- falls back to the same
    total-based conversion as Midfield/Ruck/Tackler, never inventing AFL
    stats that were never observed."""
    fb = football_score_for_position("Forward1", effective_score=24, stat_goals=None, stat_behinds=None)
    assert (fb.goals, fb.behinds) == (4, 0)
    assert fb.is_actual_afl is False


# -- Non-forward BBBFFL positions: total // 6, total % 6 ---------------------


def test_midfield_29_points_displays_as_4_5():
    fb = football_score_for_position("Midfield1", effective_score=29)
    assert (fb.goals, fb.behinds) == (4, 5)
    assert fb.line == "4.5"
    assert fb.is_actual_afl is False


def test_midfield_24_points_displays_as_4_0():
    fb = football_score_for_position("Midfield2", effective_score=24)
    assert fb.line == "4.0"


def test_ruck_25_points_displays_as_4_1():
    fb = football_score_for_position("Ruck", effective_score=25)
    assert fb.line == "4.1"


def test_tackler_zero_points_displays_as_0_0():
    fb = football_score_for_position("Tackler", effective_score=0)
    assert fb.line == "0.0"


# -- Overrides: internal consistency is never allowed to break ---------------


def test_forward_override_that_still_matches_actual_stats_keeps_actual_goals_behinds():
    """A scorer override that merely confirms the existing total (e.g. a
    late/duplicate correction) shouldn't hide the player's real goals and
    behinds -- 6*4 + 0 == 24, so the actual AFL figures are still shown."""
    fb = football_score_for_position("Forward1", effective_score=24, stat_goals=4, stat_behinds=0)
    assert fb.is_actual_afl is True
    assert 6 * fb.goals + fb.behinds == 24


def test_forward_override_inconsistent_with_actual_stats_falls_back_and_stays_consistent():
    """Documented decision: when a scorer override changes a Forward's
    effective total so that it no longer equals 6*actual_goals +
    actual_behinds, the display switches to the same divmod-based
    conversion used for Midfield/Ruck/Tackler, rather than either (a)
    showing the stale actual AFL goals/behinds next to a mismatched total,
    or (b) silently rewriting the player's real AFL statistics. G*6 + B ==
    the displayed effective total is preserved either way."""
    # Actual stats: 4 goals, 0 behinds == 24. Scorer overrides to 30.
    fb = football_score_for_position("Forward1", effective_score=30, stat_goals=4, stat_behinds=0)
    assert fb.is_actual_afl is False
    assert 6 * fb.goals + fb.behinds == 30
    assert fb.line == "5.0"


def test_override_can_never_produce_inconsistent_goals_behinds_total():
    for position, total, sg, sb in [
        ("Forward1", 0, None, None),
        ("Forward1", 24, 4, 0),
        ("Forward1", 30, 4, 0),
        ("Forward2", 100, 1, 4),
        ("Midfield1", 29, None, None),
        ("Ruck", 0, None, None),
        ("Tackler", 18, None, None),
    ]:
        fb = football_score_for_position(position, total, sg, sb)
        assert 6 * fb.goals + fb.behinds == total, (position, total, sg, sb, fb)
