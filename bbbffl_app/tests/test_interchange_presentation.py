"""Covers the three related Interchange improvements from live testing:

1. The Interchange row's presentation state (name/club/match_state/target).
2. Informational "potential" F/M/R/T scores, computed via the existing
   canonical scoring engine and never affecting any official score.
3. A named Interchange participating in matchup lifecycle determination
   even while unassigned to a scoring position.

`partial_teams` (see conftest.py) mirrors the real live scenario that
exposed these gaps: team_a has only its Interchange named, all eight
starting positions still await Friday team news.
"""

from app.afl_client import Match
from app.scoring import PlayerStats, score_position
from app.service import build_matchup_state
from app.teams import TeamConfig
from tests.conftest import CATS, PIES, TEAM_A_ROSTER, TEAM_B_ROSTER, FakeAflClient, stat_line


def _team(result, team_key):
    return next(t for t in result.teams if t.team_key == team_key)


# -- 1. Interchange row presentation ---------------------------------------


def test_interchange_row_has_sufficient_state_for_public_rendering(
    partial_teams, decisions, single_match, players_on_one_match
):
    stats = {100: {9: stat_line(9, goals=1, disposals=23, marks=2, hitouts=0, tackles=6)}}
    client = FakeAflClient([single_match], players_on_one_match, stats)

    result = build_matchup_state(client, partial_teams, decisions)

    ir = _team(result, "team_a").interchange
    assert ir.canonical_player_id == 9
    assert ir.player_name == "Player 9"
    assert ir.afl_club == "Cats"
    assert ir.match_state == "live"
    assert ir.target_position is None
    assert ir.potential_scores is not None


def test_unassigned_interchange_does_not_affect_team_score(
    partial_teams, decisions, single_match, players_on_one_match
):
    stats = {100: {9: stat_line(9, goals=10, disposals=99, marks=99, hitouts=99, tackles=99)}}
    client = FakeAflClient([single_match], players_on_one_match, stats)

    result = build_matchup_state(client, partial_teams, decisions)

    team_a = _team(result, "team_a")
    assert team_a.total_score == 0
    assert all(p.effective_score == 0 for p in team_a.positions)


def test_assigned_interchange_contributes_exactly_once(
    partial_teams, decisions, single_match, players_on_one_match
):
    decisions.set_interchange_assignment("team_a", "Forward1")
    stats = {100: {9: stat_line(9, goals=2, behinds=1)}}
    client = FakeAflClient([single_match], players_on_one_match, stats)

    result = build_matchup_state(client, partial_teams, decisions)

    team_a = _team(result, "team_a")
    fwd1 = next(p for p in team_a.positions if p.position == "Forward1")
    assert fwd1.slot_source == "interchange"
    assert fwd1.effective_score == 13  # 6*2 + 1
    assert team_a.total_score == 13  # counted exactly once, via the position row
    assert team_a.interchange.target_position == "Forward1"


# -- 2. Potential positional scores -----------------------------------------


def test_potential_scores_use_the_canonical_scoring_engine(
    partial_teams, decisions, single_match, players_on_one_match
):
    stats = {100: {9: stat_line(9, goals=3, behinds=2, disposals=27, marks=4, hitouts=10, tackles=5)}}
    client = FakeAflClient([single_match], players_on_one_match, stats)

    result = build_matchup_state(client, partial_teams, decisions)

    ir = _team(result, "team_a").interchange
    raw = PlayerStats(goals=3, behinds=2, disposals=27, marks=4, hitouts=10, tackles=5)
    assert ir.potential_scores.forward == score_position("Forward1", raw) == 20
    assert ir.potential_scores.midfield == score_position("Midfield1", raw) == 27
    assert ir.potential_scores.ruck == score_position("Ruck", raw) == 14
    assert ir.potential_scores.tackler == score_position("Tackler", raw) == 30


def test_potential_scores_update_from_fresh_stat_line(
    partial_teams, decisions, single_match, players_on_one_match
):
    client = FakeAflClient([single_match], players_on_one_match, {100: {9: stat_line(9, goals=1)}})
    first = _team(build_matchup_state(client, partial_teams, decisions), "team_a")

    client.stats_by_match = {100: {9: stat_line(9, goals=4)}}
    second = _team(build_matchup_state(client, partial_teams, decisions), "team_a")

    assert first.interchange.potential_scores.forward == 6
    assert second.interchange.potential_scores.forward == 24


def test_potential_scores_are_neutral_when_no_stats_available(
    partial_teams, decisions, single_match, players_on_one_match
):
    client = FakeAflClient([single_match], players_on_one_match, {})  # no stat row at all

    result = build_matchup_state(client, partial_teams, decisions)

    assert _team(result, "team_a").interchange.potential_scores is None


def test_potential_scores_do_not_affect_effective_or_team_scores(
    partial_teams, decisions, single_match, players_on_one_match
):
    stats = {100: {9: stat_line(9, goals=50)}}  # deliberately huge potential forward score
    client = FakeAflClient([single_match], players_on_one_match, stats)

    result = build_matchup_state(client, partial_teams, decisions)

    team_a = _team(result, "team_a")
    assert team_a.interchange.potential_scores.forward == 300
    assert team_a.total_score == 0  # still nothing assigned -- unaffected


# -- 3. Lifecycle relevance --------------------------------------------------


def test_live_named_interchange_keeps_matchup_live(
    partial_teams, decisions, single_match, players_on_one_match
):
    client = FakeAflClient([single_match], players_on_one_match, {})  # single_match status=LIVE
    result = build_matchup_state(client, partial_teams, decisions)
    assert result.status == "LIVE"


def test_yet_to_play_named_interchange_prevents_premature_signoff(
    partial_teams, decisions, players_on_one_match
):
    scheduled_match = Match(match_id=100, home_team=CATS, away_team=PIES, status="UPCOMING")
    client = FakeAflClient([scheduled_match], players_on_one_match, {})

    result = build_matchup_state(client, partial_teams, decisions)

    assert result.status == "LIVE"
    assert _team(result, "team_a").interchange.match_state == "yet_to_play"


def test_completed_named_interchange_allows_signoff_when_everything_else_is_complete(
    partial_teams, decisions, players_on_one_match
):
    final_match = Match(match_id=100, home_team=CATS, away_team=PIES, status="CONCLUDED")
    client = FakeAflClient([final_match], players_on_one_match, {})

    result = build_matchup_state(client, partial_teams, decisions)

    assert result.status == "AWAITING_SCORER_SIGNOFF"
    assert _team(result, "team_a").interchange.match_state == "completed"


def test_dnp_named_interchange_does_not_block_signoff(
    decisions, single_match, players_on_one_match
):
    """DNP semantics are unchanged: once a player is scorer-marked DNP we
    stop waiting on their match, whether they're a starter or Interchange.
    Isolated from `partial_teams` here -- team_b there is fully named and
    live, which would keep the matchup LIVE regardless of team_a's DNP and
    defeat the point of this test."""
    team_a_roster = dict.fromkeys(TEAM_A_ROSTER)
    team_a_roster["Interchange"] = TEAM_A_ROSTER["Interchange"]  # only Interchange named
    team_b_roster = dict.fromkeys(TEAM_B_ROSTER)  # nothing named at all, including Interchange
    isolated_teams = [
        TeamConfig(team_key="team_a", name="Alpha", roster=team_a_roster),
        TeamConfig(team_key="team_b", name="Bravo", roster=team_b_roster),
    ]
    decisions.set_dnp("team_a", "Interchange", True)
    client = FakeAflClient([single_match], players_on_one_match, {})  # match is LIVE

    result = build_matchup_state(client, isolated_teams, decisions)

    assert result.status == "AWAITING_SCORER_SIGNOFF"


def test_assigned_interchange_lifecycle_relevance_is_not_double_counted(
    partial_teams, decisions, single_match, players_on_one_match
):
    """Assigning Interchange to a position surfaces its match state via
    that position row too -- relevant_match_states is a set of state
    labels, so this can't double-count; matchup status must agree with the
    unassigned case for the same underlying match."""
    decisions.set_interchange_assignment("team_a", "Forward1")
    client = FakeAflClient([single_match], players_on_one_match, {})  # LIVE

    result = build_matchup_state(client, partial_teams, decisions)

    assert result.status == "LIVE"
