from app.afl_client import Match, PlayerStatLine, Team
from app.participation import DnpRecommendationState, ParticipationState, assess_participation

HOME = Team(1, "Home")
AWAY = Team(2, "Away")
MATCH = Match(10, HOME, AWAY, "CONCLUDED")


def test_stat_line_with_stats_is_authoritative_participation():
    evidence = assess_participation(
        afl_team_id=1,
        bye_team_ids=frozenset(),
        match=MATCH,
        stat_line=PlayerStatLine(20, goals=1),
    )
    assert evidence.state == ParticipationState.PLAYED_WITH_STATS
    assert evidence.dnp_recommendation == DnpRecommendationState.NOT_DNP


def test_legitimate_zero_stat_row_is_participation_not_dnp():
    evidence = assess_participation(afl_team_id=1, bye_team_ids=frozenset(), match=MATCH, stat_line=PlayerStatLine(20))
    assert evidence.state == ParticipationState.PARTICIPATED_ZERO_STATS
    assert evidence.dnp_recommendation == DnpRecommendationState.NOT_DNP


def test_missing_stat_row_remains_unknown_not_dnp():
    evidence = assess_participation(afl_team_id=1, bye_team_ids=frozenset(), match=MATCH, stat_line=None)
    assert evidence.state == ParticipationState.UNKNOWN
    assert evidence.dnp_recommendation == DnpRecommendationState.REVIEW_REQUIRED


def test_ordinary_bye_is_unavailable_not_dnp():
    evidence = assess_participation(afl_team_id=1, bye_team_ids=frozenset({1}), match=None, stat_line=None)
    assert evidence.state == ParticipationState.CLUB_BYE
    assert evidence.dnp_recommendation == DnpRecommendationState.NOT_DNP


def test_missing_bye_and_match_evidence_stays_unknown():
    evidence = assess_participation(afl_team_id=1, bye_team_ids=None, match=None, stat_line=None)
    assert evidence.state == ParticipationState.UNKNOWN
    assert evidence.dnp_recommendation == DnpRecommendationState.REVIEW_REQUIRED
