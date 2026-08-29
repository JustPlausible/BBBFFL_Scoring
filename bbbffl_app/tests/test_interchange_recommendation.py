"""Synthetic 2026-shaped replay fixtures for Issue #57 rule determinism."""

import dataclasses

from app.service import build_matchup_state
from tests.conftest import FakeAflClient, stat_line


def _team(result):
    return next(team for team in result.teams if team.team_key == "team_a")


def test_every_confirmed_dnp_target_is_evaluated_and_clear_best_is_advisory(
    teams, decisions, single_match, players_on_one_match
):
    decisions.set_dnp("team_a", "Forward1", True, reason="Synthetic replay withdrawal")
    decisions.set_dnp("team_a", "Midfield1", True, reason="Synthetic replay withdrawal")
    stats = {100: {9: stat_line(9, goals=2, behinds=1, disposals=20)}}
    before = _team(build_matchup_state(FakeAflClient([single_match], players_on_one_match, stats), teams, decisions))
    recommendation = before.interchange_recommendation
    assert [(c.target_position, c.team_outcome) for c in recommendation.candidates] == [
        ("Forward1", 13.0),
        ("Midfield1", 20.0),
    ]
    assert recommendation.state == "clear_best"
    assert recommendation.recommended_targets == ["Midfield1"]
    assert recommendation.advisory_only is True
    assert before.total_score == 0  # recommendation did not apply itself
    assert decisions.get_interchange_assignments() == {}


def test_equal_best_targets_surface_complete_tie(teams, decisions, single_match, players_on_one_match):
    decisions.set_dnp("team_a", "Forward1", True)
    decisions.set_dnp("team_a", "Forward2", True)
    stats = {100: {9: stat_line(9, goals=2, behinds=1)}}
    recommendation = _team(
        build_matchup_state(FakeAflClient([single_match], players_on_one_match, stats), teams, decisions)
    ).interchange_recommendation
    assert recommendation.state == "equal_best"
    assert recommendation.recommended_targets == ["Forward1", "Forward2"]


def test_intentional_vacancy_is_explicit_candidate(partial_teams, decisions, single_match, players_on_one_match):
    stats = {100: {9: stat_line(9, tackles=3)}}
    recommendation = _team(
        build_matchup_state(FakeAflClient([single_match], players_on_one_match, stats), partial_teams, decisions)
    ).interchange_recommendation
    assert any(
        c.target_position == "Tackler" and c.vacancy_kind == "intentional_vacancy" for c in recommendation.candidates
    )


def test_no_eligible_target_and_missing_interchange_evidence_states(
    teams, decisions, single_match, players_on_one_match
):
    result = _team(build_matchup_state(FakeAflClient([single_match], players_on_one_match, {}), teams, decisions))
    assert result.interchange_recommendation.state == "no_eligible_replacement"
    decisions.set_dnp("team_a", "Ruck", True)
    result = _team(build_matchup_state(FakeAflClient([single_match], players_on_one_match, {}), teams, decisions))
    assert result.interchange_recommendation.state == "awaiting_evidence"


def test_persisted_selection_alone_controls_score_and_original_player_survives(
    teams, decisions, single_match, players_on_one_match
):
    decisions.set_dnp("team_a", "Forward1", True)
    stats = {100: {9: stat_line(9, goals=2, behinds=1, disposals=20)}}
    advisory = _team(build_matchup_state(FakeAflClient([single_match], players_on_one_match, stats), teams, decisions))
    assert advisory.total_score == 0
    decisions.set_interchange_assignment("team_a", "Forward1", reason="Scorer accepted target")
    official = _team(build_matchup_state(FakeAflClient([single_match], players_on_one_match, stats), teams, decisions))
    position = next(p for p in official.positions if p.position == "Forward1")
    assert official.total_score == 13
    assert position.starting_player_id == 1
    assert position.canonical_player_id == 9
    # Recalculation is deterministic and does not rewrite the scorer selection.
    replay = _team(build_matchup_state(FakeAflClient([single_match], players_on_one_match, stats), teams, decisions))
    assert dataclasses.asdict(replay) == dataclasses.asdict(official)
    assert decisions.get_interchange_assignments()["team_a"].target_position == "Forward1"


def test_explicit_dnp_rejection_is_retained_separately_from_evidence(
    teams, decisions, single_match, players_on_one_match
):
    decisions.set_dnp("team_a", "Forward1", False, reason="Scorer verified participation")
    result = _team(build_matchup_state(FakeAflClient([single_match], players_on_one_match, {}), teams, decisions))
    position = next(p for p in result.positions if p.position == "Forward1")
    assert position.participation_evidence.state == "unknown"
    assert position.dnp_ruling is False
    assert position.starting_dnp is False
