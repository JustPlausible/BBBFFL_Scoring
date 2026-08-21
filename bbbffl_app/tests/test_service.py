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
    final_match = Match(match_id=100, home_team=CATS, away_team=PIES, status="CONCLUDED")
    client = FakeAflClient([final_match], players_on_one_match, {})

    result = build_matchup_state(client, teams, decisions)

    assert result.status == "AWAITING_SCORER_SIGNOFF"


def test_explicit_finalisation_moves_matchup_to_final(teams, decisions, players_on_one_match):
    final_match = Match(match_id=100, home_team=CATS, away_team=PIES, status="CONCLUDED")
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


def test_unnamed_position_does_not_trigger_afl_api_player_lookup(
    partial_teams, decisions, single_match, players_on_one_match
):
    """The Thursday-night loophole: team_a only has its Interchange named.
    Nothing about the other eight null slots may reach afl-api -- e.g. as a
    request for /api/v1/players/None."""
    client = FakeAflClient([single_match], players_on_one_match, {})

    build_matchup_state(client, partial_teams, decisions)

    assert None not in client.get_player_calls
    assert 1 not in client.get_player_calls  # team_a's unnamed Forward1 slot
    assert 9 in client.get_player_calls  # team_a's named Interchange


def test_unnamed_position_scores_zero(partial_teams, decisions, single_match, players_on_one_match):
    client = FakeAflClient([single_match], players_on_one_match, {})

    result = build_matchup_state(client, partial_teams, decisions)

    team_a = next(t for t in result.teams if t.team_key == "team_a")
    fwd1 = _positions_by_name(team_a)["Forward1"]
    assert fwd1.slot_source == "unnamed"
    assert fwd1.match_state == "unnamed"
    assert fwd1.canonical_player_id is None
    assert fwd1.calculated_score == 0
    assert fwd1.effective_score == 0


def test_named_players_alongside_unnamed_positions_resolve_and_score_normally(
    partial_teams, decisions, single_match, players_on_one_match
):
    decisions.set_interchange_assignment("team_a", "Forward1")
    stats = {100: {9: stat_line(9, goals=2, behinds=0)}}
    client = FakeAflClient([single_match], players_on_one_match, stats)

    result = build_matchup_state(client, partial_teams, decisions)

    team_a = next(t for t in result.teams if t.team_key == "team_a")
    positions = _positions_by_name(team_a)
    fwd1 = positions["Forward1"]
    assert fwd1.slot_source == "interchange"
    assert fwd1.calculated_score == 12  # 6*2 goals
    for pos_name, pos in positions.items():
        if pos_name != "Forward1":
            assert pos.slot_source == "unnamed"
            assert pos.calculated_score == 0
    assert team_a.total_score == 12

    # team_b is fully named and resolves/scores completely normally,
    # unaffected by team_a's gaps.
    team_b = next(t for t in result.teams if t.team_key == "team_b")
    assert all(pos.slot_source == "starting" for pos in team_b.positions)


def test_unnamed_is_distinct_from_scorer_marked_dnp(
    partial_teams, decisions, single_match, players_on_one_match
):
    # A *named* team_b position marked DNP by the scorer, for comparison.
    decisions.set_dnp("team_b", "Forward1", True)
    client = FakeAflClient([single_match], players_on_one_match, {})

    result = build_matchup_state(client, partial_teams, decisions)

    team_a = next(t for t in result.teams if t.team_key == "team_a")
    team_b = next(t for t in result.teams if t.team_key == "team_b")
    unnamed = _positions_by_name(team_a)["Forward1"]
    dnp_vacant = _positions_by_name(team_b)["Forward1"]

    assert unnamed.slot_source == "unnamed"
    assert unnamed.match_state == "unnamed"
    assert dnp_vacant.slot_source == "vacant"
    assert dnp_vacant.match_state == "vacant"
    assert unnamed.slot_source != dnp_vacant.slot_source
    assert unnamed.match_state != dnp_vacant.match_state


def test_lifecycle_ignores_unnamed_positions(partial_teams, decisions, players_on_one_match):
    """team_a's eight unnamed positions must not keep the matchup stuck LIVE
    once every AFL match that a *named* position depends on is final."""
    final_match = Match(match_id=100, home_team=CATS, away_team=PIES, status="CONCLUDED")
    client = FakeAflClient([final_match], players_on_one_match, {})

    result = build_matchup_state(client, partial_teams, decisions)

    assert result.status == "AWAITING_SCORER_SIGNOFF"


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
    final_match = Match(match_id=100, home_team=CATS, away_team=PIES, status="CONCLUDED")
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


def _strip_football_display_fields(snapshot: dict) -> dict:
    """Mimics a FINAL snapshot recorded before the football-style display
    fields existed -- dataclasses.asdict() of a MatchupResult built with an
    older PositionResult/TeamResult that never had them."""
    display_keys = (
        "display_goals",
        "display_behinds",
        "display_is_actual_afl",
        "display_adjusted_by_override",
        "football_line",
    )
    for team in snapshot["teams"]:
        for key in display_keys:
            team.pop(key, None)
        for position in team["positions"]:
            for key in display_keys:
                position.pop(key, None)
    return snapshot


def test_get_matchup_view_backfills_display_fields_onto_a_legacy_finalized_snapshot(
    teams, decisions, single_match, players_on_one_match
):
    """A Grand Final finalised before this presentation layer existed must
    keep serving from get_matchup_view() without a KeyError, and the
    backfilled figures must stay internally consistent."""
    stats = {100: {1: stat_line(1, goals=3, behinds=1)}}
    client = FakeAflClient([single_match], players_on_one_match, stats)
    pre = build_matchup_state(client, teams, decisions)

    legacy_snapshot = _strip_football_display_fields(dataclasses.asdict(pre))
    decisions.finalize("Signed off (legacy)", legacy_snapshot)

    view = get_matchup_view(client, teams, decisions)

    assert view["status"] == "FINAL"
    team_a = next(t for t in view["teams"] if t["team_key"] == "team_a")
    assert "football_line" in team_a
    for position in team_a["positions"]:
        assert "football_line" in position
        assert 6 * position["display_goals"] + position["display_behinds"] == position["effective_score"]
    assert (
        sum(p["display_goals"] for p in team_a["positions"]),
        sum(p["display_behinds"] for p in team_a["positions"]),
    ) == (team_a["display_goals"], team_a["display_behinds"])


# -- Football-style (Goals.Behinds/Total) presentation ------------------------


def test_forward_position_display_uses_actual_afl_goals_and_behinds(
    teams, decisions, single_match, players_on_one_match
):
    stats = {100: {1: stat_line(1, goals=3, behinds=1)}}
    client = FakeAflClient([single_match], players_on_one_match, stats)

    result = build_matchup_state(client, teams, decisions)

    fwd1 = _positions_by_name(next(t for t in result.teams if t.team_key == "team_a"))["Forward1"]
    assert fwd1.effective_score == 19
    assert (fwd1.display_goals, fwd1.display_behinds) == (3, 1)
    assert fwd1.display_is_actual_afl is True
    assert fwd1.football_line == "3.1"


def test_forward_override_inconsistent_with_actual_stats_stays_internally_consistent(
    teams, decisions, single_match, players_on_one_match
):
    """A scorer override that no longer matches the Forward's actual AFL
    goals/behinds must never leave the row showing G*6 + B != the displayed
    effective total (see app/presentation.py's documented decision)."""
    stats = {100: {1: stat_line(1, goals=3, behinds=1)}}  # actual: 3.1 == 19
    client = FakeAflClient([single_match], players_on_one_match, stats)
    decisions.set_override("team_a", "Forward1", 30.0, "corrected after review")

    result = build_matchup_state(client, teams, decisions)

    fwd1 = _positions_by_name(next(t for t in result.teams if t.team_key == "team_a"))["Forward1"]
    assert fwd1.effective_score == 30.0
    assert fwd1.display_is_actual_afl is False
    assert 6 * fwd1.display_goals + fwd1.display_behinds == fwd1.effective_score
    assert fwd1.football_line == "5.0"


def test_team_football_totals_are_summed_from_player_display_rows_not_divmod_of_team_total(
    teams, decisions, single_match, players_on_one_match
):
    """Regression for the worked example in the task brief: a team scoring
    169 points from a realistic Forward/Midfield/Ruck/Tackler lineup must
    read 26.13 (169), not divmod(169, 6) == 28.1."""
    stats = {
        100: {
            1: stat_line(1, goals=4, behinds=0),  # Forward1: 4.0 (24)
            2: stat_line(2, goals=1, behinds=4),  # Forward2: 1.4 (10)
            3: stat_line(3, goals=3, behinds=2),  # Forward3: 3.2 (20)
            4: stat_line(4, disposals=29),  # Midfield1: 29 -> 4.5
            5: stat_line(5, disposals=24),  # Midfield2: 24 -> 4.0
            6: stat_line(6, disposals=19),  # Midfield3: 19 -> 3.1
            7: stat_line(7, marks=4, hitouts=21),  # Ruck: 25 -> 4.1
            8: stat_line(8, tackles=3),  # Tackler: 18 -> 3.0
        }
    }
    client = FakeAflClient([single_match], players_on_one_match, stats)

    result = build_matchup_state(client, teams, decisions)

    team_a = next(t for t in result.teams if t.team_key == "team_a")
    assert team_a.total_score == 169
    assert (team_a.display_goals, team_a.display_behinds) == (26, 13)
    assert team_a.football_line == "26.13"
    # The naive-but-wrong approach this guards against.
    assert divmod(int(team_a.total_score), 6) != (team_a.display_goals, team_a.display_behinds)

    # And the team total is exactly the sum of the position rows' own
    # display goals/behinds, not an independent computation.
    summed_goals = sum(p.display_goals for p in team_a.positions)
    summed_behinds = sum(p.display_behinds for p in team_a.positions)
    assert (summed_goals, summed_behinds) == (team_a.display_goals, team_a.display_behinds)


def test_unadjusted_forward_with_no_stat_line_is_not_flagged_as_override_adjusted(
    partial_teams, decisions, single_match, players_on_one_match
):
    """Regression: an ordinary Forward with no AFL stat line yet (unnamed,
    vacant, DNP, or yet_to_play) has display_is_actual_afl=False just like
    an override-adjusted row, but there was no override -- it must not be
    flagged as one."""
    client = FakeAflClient([single_match], players_on_one_match, {})

    result = build_matchup_state(client, partial_teams, decisions)

    fwd1 = _positions_by_name(next(t for t in result.teams if t.team_key == "team_a"))["Forward1"]
    assert fwd1.slot_source == "unnamed"
    assert fwd1.display_is_actual_afl is False
    assert fwd1.display_adjusted_by_override is False


def test_forward_override_inconsistent_with_stats_is_flagged_as_override_adjusted(
    teams, decisions, single_match, players_on_one_match
):
    stats = {100: {1: stat_line(1, goals=3, behinds=1)}}
    client = FakeAflClient([single_match], players_on_one_match, stats)
    decisions.set_override("team_a", "Forward1", 30.0, "corrected after review")

    result = build_matchup_state(client, teams, decisions)

    fwd1 = _positions_by_name(next(t for t in result.teams if t.team_key == "team_a"))["Forward1"]
    assert fwd1.display_adjusted_by_override is True


def test_forward_override_that_still_matches_actual_stats_is_not_flagged(
    teams, decisions, single_match, players_on_one_match
):
    """An override that merely reconfirms the existing total (6*3+1==19)
    keeps showing the real goals/behinds -- nothing was adjusted."""
    stats = {100: {1: stat_line(1, goals=3, behinds=1)}}
    client = FakeAflClient([single_match], players_on_one_match, stats)
    decisions.set_override("team_a", "Forward1", 19.0, "duplicate confirmation")

    result = build_matchup_state(client, teams, decisions)

    fwd1 = _positions_by_name(next(t for t in result.teams if t.team_key == "team_a"))["Forward1"]
    assert fwd1.display_is_actual_afl is True
    assert fwd1.display_adjusted_by_override is False


def test_non_forward_positions_convert_points_to_football_style(
    teams, decisions, single_match, players_on_one_match
):
    stats = {
        100: {
            4: stat_line(4, disposals=29),
            5: stat_line(5, disposals=24),
            6: stat_line(6, disposals=25),
            7: stat_line(7, marks=0, hitouts=0),
        }
    }
    client = FakeAflClient([single_match], players_on_one_match, stats)

    result = build_matchup_state(client, teams, decisions)

    positions = _positions_by_name(next(t for t in result.teams if t.team_key == "team_a"))
    assert positions["Midfield1"].football_line == "4.5"  # 29 -> 4.5
    assert positions["Midfield2"].football_line == "4.0"  # 24 -> 4.0
    assert positions["Midfield3"].football_line == "4.1"  # 25 -> 4.1
    assert positions["Ruck"].football_line == "0.0"  # 0 -> 0.0
