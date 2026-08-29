"""Synthetic 2026-shaped replay fixtures for Issue #57 rule determinism."""

import dataclasses
from contextlib import contextmanager

from app.afl_client import Match, Player, Team
from app.scorer_decisions import finalize
from app.service import build_matchup_state
from tests.conftest import CATS, PIES, FakeAflClient, stat_line


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


def test_candidate_replaces_target_override_instead_of_double_counting(
    teams, decisions, single_match, players_on_one_match
):
    decisions.set_dnp("team_a", "Forward1", True)
    decisions.set_dnp("team_a", "Midfield1", True)
    decisions.set_override("team_a", "Forward1", 100, "Synthetic scorer override")
    stats = {100: {9: stat_line(9, goals=2, disposals=20)}}
    recommendation = _team(
        build_matchup_state(FakeAflClient([single_match], players_on_one_match, stats), teams, decisions)
    ).interchange_recommendation
    outcomes = {candidate.target_position: candidate.team_outcome for candidate in recommendation.candidates}
    assert outcomes == {"Forward1": 100, "Midfield1": 120}
    assert recommendation.recommended_targets == ["Midfield1"]


def test_interchange_on_ordinary_bye_has_no_candidates(teams, decisions, single_match, players_on_one_match):
    bye_team = Team(3000, "Bye Club")
    players = dict(players_on_one_match)
    players[9] = Player(9, "Player 9", bye_team)
    decisions.set_dnp("team_a", "Forward1", True)
    result = _team(build_matchup_state(FakeAflClient([single_match], players, {}, byes=(bye_team,)), teams, decisions))
    recommendation = result.interchange_recommendation
    assert result.interchange.participation_evidence.state == "club_bye"
    assert recommendation.state == "no_eligible_replacement"
    assert recommendation.candidates == []


def test_interchange_unresolved_and_explicit_rejection_are_distinct(
    teams, decisions, single_match, players_on_one_match
):
    client = FakeAflClient([single_match], players_on_one_match, {})
    unresolved = _team(build_matchup_state(client, teams, decisions))
    assert unresolved.interchange.dnp is False
    assert unresolved.interchange.dnp_ruling is None
    decisions.set_dnp("team_a", "Interchange", False, reason="Scorer confirmed available")
    rejected = _team(build_matchup_state(client, teams, decisions))
    assert rejected.interchange.dnp is False
    assert rejected.interchange.dnp_ruling is False


class _FreshnessBatch:
    def __init__(self):
        self.fresh = True

    def is_evidence_fresh(self):
        return self.fresh


class _AdvisoryStaleClient(FakeAflClient):
    """Only the innermost batch observes each call, like ResilientAflClient."""

    def __init__(self, matches, players):
        super().__init__(matches, players, {100: {9: stat_line(9, goals=1)}, 200: {1: stat_line(1)}})
        self._active_batch = None

    @contextmanager
    def evidence_batch(self):
        batch = _FreshnessBatch()
        previous = self._active_batch
        self._active_batch = batch
        try:
            yield batch
        finally:
            self._active_batch = previous

    def get_match_player_stats(self, match_id):
        value = super().get_match_player_stats(match_id)
        if self._active_batch is not None and match_id == 200:
            self._active_batch.fresh = False
        return value


def test_stale_advisory_dnp_evidence_does_not_block_fresh_official_finalisation(teams, decisions, players_on_one_match):
    dnp_club = Team(2001, "DNP Club")
    opponent = Team(2002, "Opponent")
    players = dict(players_on_one_match)
    players[1] = Player(1, "Player 1", dnp_club)
    matches = [
        Match(100, CATS, PIES, "CONCLUDED"),
        Match(200, dnp_club, opponent, "CONCLUDED"),
    ]
    decisions.set_dnp("team_a", "Forward1", True)
    client = _AdvisoryStaleClient(matches, players)
    with client.evidence_batch() as authoritative_batch:
        result = build_matchup_state(client, teams, decisions)
        assert result.status == "AWAITING_SCORER_SIGNOFF"
        position = next(p for p in _team(result).positions if p.position == "Forward1")
        assert position.participation_evidence.state == "participated_zero_stats"
        assert authoritative_batch.is_evidence_fresh() is True
        finalize(result, decisions, "Official dependencies fresh", afl_client=authoritative_batch)
    assert decisions.get_matchup_state().finalized is True
