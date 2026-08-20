import dataclasses

import pytest

from app.afl_client import AflApiError, Match
from app.service import build_matchup_state, get_matchup_view
from tests.conftest import CATS, PIES, FakeAflClient, stat_line


def _positions_by_name(team_result):
    return {p.position: p for p in team_result.positions}


def test_starting_player_scores_from_afl_stats(teams, decisions, single_match, players_on_one_match):
    stats = {100: {1: stat_line(1, goals=3, behinds=1)}}
    client = FakeAflClient([single_match], players_on_one_match, stats)

    result = build_matchup_state(client, teams, decisions)

    team_a = next(t for t in result.teams if t.team_key == "team_a")
    fwd1 = _positions_by_name(team_a)["Forward1"]
    assert fwd1.slot_source == "starting"
    assert fwd1.calculated_score == 19  # 6*3 + 1
    assert fwd1.effective_score == 19
    assert fwd1.match_state == "live"


def test_dnp_starter_leaves_position_vacant_and_recommends_interchange(
    teams, decisions, single_match, players_on_one_match
):
    decisions.set_dnp("team_a", "Forward1", True)
    client = FakeAflClient([single_match], players_on_one_match, {})

    result = build_matchup_state(client, teams, decisions)

    team_a = next(t for t in result.teams if t.team_key == "team_a")
    fwd1 = _positions_by_name(team_a)["Forward1"]
    assert fwd1.slot_source == "vacant"
    assert fwd1.match_state == "vacant"
    assert fwd1.calculated_score == 0
    assert fwd1.recommended_interchange is True


def test_interchange_assignment_scores_as_replaced_position(
    teams, decisions, single_match, players_on_one_match
):
    decisions.set_dnp("team_a", "Forward1", True)
    decisions.set_interchange_assignment("team_a", "Forward1")
    stats = {100: {9: stat_line(9, goals=2, behinds=1)}}  # team_a's Interchange player
    client = FakeAflClient([single_match], players_on_one_match, stats)

    result = build_matchup_state(client, teams, decisions)

    team_a = next(t for t in result.teams if t.team_key == "team_a")
    fwd1 = _positions_by_name(team_a)["Forward1"]
    assert fwd1.slot_source == "interchange"
    assert fwd1.canonical_player_id == 9
    assert fwd1.calculated_score == 13  # 6*2 + 1, scored using the Forward formula
    assert team_a.interchange.target_position == "Forward1"
    assert fwd1.recommended_interchange is False


def test_interchange_can_replace_a_position_even_without_starter_dnp(
    teams, decisions, single_match, players_on_one_match
):
    """Supports the Thursday-night loophole: interchange assigned ahead of any DNP."""
    decisions.set_interchange_assignment("team_a", "Midfield1")
    stats = {100: {9: stat_line(9, disposals=25), 4: stat_line(4, disposals=5)}}
    client = FakeAflClient([single_match], players_on_one_match, stats)

    result = build_matchup_state(client, teams, decisions)

    team_a = next(t for t in result.teams if t.team_key == "team_a")
    mid1 = _positions_by_name(team_a)["Midfield1"]
    assert mid1.slot_source == "interchange"
    assert mid1.calculated_score == 25  # the original starter's 5 disposals are ignored


def test_interchange_player_dnp_scores_zero_when_assigned(
    teams, decisions, single_match, players_on_one_match
):
    decisions.set_dnp("team_a", "Forward1", True)
    decisions.set_interchange_assignment("team_a", "Forward1")
    decisions.set_dnp("team_a", "Interchange", True)
    client = FakeAflClient([single_match], players_on_one_match, {})

    result = build_matchup_state(client, teams, decisions)

    team_a = next(t for t in result.teams if t.team_key == "team_a")
    fwd1 = _positions_by_name(team_a)["Forward1"]
    assert fwd1.match_state == "dnp"
    assert fwd1.calculated_score == 0


def test_score_override_changes_effective_but_not_calculated_score(
    teams, decisions, single_match, players_on_one_match
):
    stats = {100: {7: stat_line(7, marks=2, hitouts=10)}}
    client = FakeAflClient([single_match], players_on_one_match, stats)
    decisions.set_override("team_a", "Ruck", 100.0, "Scorer correction for late data")

    result = build_matchup_state(client, teams, decisions)

    team_a = next(t for t in result.teams if t.team_key == "team_a")
    ruck = _positions_by_name(team_a)["Ruck"]
    assert ruck.calculated_score == 12  # 2 marks + 10 hitouts, unaffected
    assert ruck.override_score == 100.0
    assert ruck.effective_score == 100.0
    assert ruck.override_reason == "Scorer correction for late data"
    assert team_a.total_score == sum(p.effective_score for p in team_a.positions)


def test_clearing_override_reverts_to_calculated_score(
    teams, decisions, single_match, players_on_one_match
):
    stats = {100: {7: stat_line(7, marks=2, hitouts=10)}}
    client = FakeAflClient([single_match], players_on_one_match, stats)
    decisions.set_override("team_a", "Ruck", 100.0, "temporary")
    decisions.set_override("team_a", "Ruck", None, None)

    result = build_matchup_state(client, teams, decisions)

    team_a = next(t for t in result.teams if t.team_key == "team_a")
    ruck = _positions_by_name(team_a)["Ruck"]
    assert ruck.override_score is None
    assert ruck.effective_score == 12


def test_matchup_stays_live_while_a_relevant_match_is_in_progress(
    teams, decisions, single_match, players_on_one_match
):
    client = FakeAflClient([single_match], players_on_one_match, {})
    result = build_matchup_state(client, teams, decisions)
    assert result.status == "LIVE"


def test_matchup_awaits_signoff_once_all_relevant_matches_are_final(
    teams, decisions, players_on_one_match
):
    final_match = Match(match_id=100, home_team=CATS, away_team=PIES, status="FINAL")
    client = FakeAflClient([final_match], players_on_one_match, {})

    result = build_matchup_state(client, teams, decisions)

    assert result.status == "AWAITING_SCORER_SIGNOFF"


def test_explicit_finalisation_moves_matchup_to_final(teams, decisions, players_on_one_match):
    final_match = Match(match_id=100, home_team=CATS, away_team=PIES, status="FINAL")
    client = FakeAflClient([final_match], players_on_one_match, {})

    decisions.finalize("Grand Final result confirmed by scorer")
    result = build_matchup_state(client, teams, decisions)

    assert result.status == "FINAL"
    assert result.finalized_note == "Grand Final result confirmed by scorer"


def test_stats_fetched_once_per_unique_match_not_per_player(
    teams, decisions, single_match, players_on_one_match
):
    client = FakeAflClient([single_match], players_on_one_match, {})
    build_matchup_state(client, teams, decisions)
    assert client.stats_fetch_calls == [100]


def test_starting_dnp_flag_persists_even_when_interchange_covers_the_position(
    teams, decisions, single_match, players_on_one_match
):
    """A scorer must be able to see and clear the starter's own DNP decision
    independent of whatever the interchange is currently doing -- otherwise
    the admin UI can't reverse it without first clearing the interchange."""
    decisions.set_dnp("team_a", "Forward1", True)
    decisions.set_interchange_assignment("team_a", "Forward1")
    client = FakeAflClient([single_match], players_on_one_match, {})

    result = build_matchup_state(client, teams, decisions)

    team_a = next(t for t in result.teams if t.team_key == "team_a")
    fwd1 = _positions_by_name(team_a)["Forward1"]
    assert fwd1.slot_source == "interchange"
    assert fwd1.starting_dnp is True


def test_get_matchup_view_serves_frozen_snapshot_after_finalize_without_calling_afl_api(
    teams, decisions, single_match, players_on_one_match
):
    final_match = Match(match_id=100, home_team=CATS, away_team=PIES, status="FINAL")
    stats = {100: {1: stat_line(1, goals=3, behinds=1)}}
    client = FakeAflClient([final_match], players_on_one_match, stats)

    pre_finalize_result = build_matchup_state(client, teams, decisions)
    assert pre_finalize_result.status == "AWAITING_SCORER_SIGNOFF"

    snapshot = dataclasses.asdict(pre_finalize_result)
    decisions.finalize("Signed off", snapshot)

    class ExplodingClient:
        def __getattr__(self, name):
            def _boom(*args, **kwargs):
                raise AflApiError(f"afl-api should not be called after finalize (called {name})")

            return _boom

    view = get_matchup_view(ExplodingClient(), teams, decisions)

    assert view["status"] == "FINAL"
    assert view["finalized_note"] == "Signed off"
    team_a = next(t for t in view["teams"] if t["team_key"] == "team_a")
    fwd1 = next(p for p in team_a["positions"] if p["position"] == "Forward1")
    assert fwd1["effective_score"] == 19


def test_get_matchup_view_stays_live_before_finalize(
    teams, decisions, single_match, players_on_one_match
):
    client = FakeAflClient([single_match], players_on_one_match, {})
    view = get_matchup_view(client, teams, decisions)
    assert view["status"] == "LIVE"
