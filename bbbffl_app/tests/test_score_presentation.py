"""Unit coverage for the shared, dependency-free BBBFFL score display
formatting extracted for issue #261 (`app/score_presentation.py`) -- the
season model's public read models and `app.presentation` (the Grand
Final/SuperScore prototype) both reuse these exact functions rather than
each converting scores their own way.
"""

from app.score_presentation import (
    football_score_from_evidence,
    football_score_from_total,
    format_football_line,
    format_number,
)

# -- format_number: issue #261's SuperScore ".0" requirement ----------------


def test_integral_total_formats_without_trailing_zero():
    assert format_number(271.0) == 271
    assert format_number(246.0) == 246
    assert format_number(227.0) == 227
    assert isinstance(format_number(271.0), int)


def test_non_integral_total_still_displays_sensibly():
    """A genuinely fractional score (e.g. a half-point scorer override)
    must never be coerced into a misleading whole number."""
    assert format_number(227.5) == 227.5
    assert format_number(0.5) == 0.5


def test_integer_input_is_returned_unchanged():
    assert format_number(158) == 158


# -- format_football_line -----------------------------------------------


def test_format_football_line_whole_numbers():
    assert format_football_line(24, 14) == "24.14"
    assert format_football_line(38, 12) == "38.12"
    assert format_football_line(0, 0) == "0.0"


def test_format_football_line_fractional_uses_explicit_separator():
    assert format_football_line(4.5, 0) == "4.5 · 0"


# -- football_score_from_total -------------------------------------------


def test_football_score_from_total_is_the_traditional_divmod_conversion():
    assert football_score_from_total(158) == (26, 2)
    assert football_score_from_total(0) == (0, 0)


def test_football_score_from_total_is_none_for_an_unknown_total():
    assert football_score_from_total(None) is None


# -- football_score_from_evidence -----------------------------------------


def test_evidence_prefers_the_literal_stat_capable_line_when_consistent():
    result = football_score_from_evidence(24, is_actual_stat_capable=True, stat_goals=4, stat_behinds=0)
    assert result == (4, 0, True)


def test_evidence_preserves_a_literal_behind_total_of_six_or_more():
    """Codex P2 on PR #262: 1 goal + 8 behinds = 14 points -- a naive
    divmod(14, 6) would wrongly read as 2 goals 2 behinds, silently
    folding two of the real behinds into an extra goal."""
    result = football_score_from_evidence(14, is_actual_stat_capable=True, stat_goals=1, stat_behinds=8)
    assert result == (1, 8, True)


def test_evidence_falls_back_to_divmod_when_not_stat_capable():
    result = football_score_from_evidence(29, is_actual_stat_capable=False, stat_goals=None, stat_behinds=None)
    assert result == (4, 5, False)


def test_evidence_falls_back_to_divmod_when_stat_capable_but_no_stat_line():
    result = football_score_from_evidence(24, is_actual_stat_capable=True, stat_goals=None, stat_behinds=None)
    assert result == (4, 0, False)


def test_evidence_falls_back_to_divmod_when_stat_line_is_inconsistent_with_an_override():
    result = football_score_from_evidence(30, is_actual_stat_capable=True, stat_goals=4, stat_behinds=0)
    assert result == (5, 0, False)


def test_evidence_is_none_for_an_unknown_effective_score():
    assert football_score_from_evidence(None, is_actual_stat_capable=True) is None
