import sqlite3

import pytest

from app.afl_client import Match, Player, PlayerStatLine, Team
from app.db import DecisionsRepository, init_db
from app.scoring import ROSTER_SLOTS
from app.service import build_superscore_state, get_superscore_view
from app.teams import TeamConfig
from tests.conftest import FakeAflClient, stat_line

CATS = Team(team_id=2001, name="Cats")
PIES = Team(team_id=2002, name="Pies")
SEASON = 2026
AFL_ROUND = 20


def _entry(n: int, roster_overrides: dict | None = None) -> TeamConfig:
    base = n * 1000
    roster = {slot: base + i for i, slot in enumerate(ROSTER_SLOTS)}
    if roster_overrides:
        roster.update(roster_overrides)
    return TeamConfig(team_key=f"team_{n}", name=f"Coach {n}", roster=roster)


@pytest.fixture
def ten_entries():
    return [_entry(n) for n in range(1, 11)]


@pytest.fixture
def superscore_decisions():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return DecisionsRepository(conn, competition_key="superscore:2026:20")


@pytest.fixture
def match():
    return Match(match_id=500, home_team=CATS, away_team=PIES, status="LIVE")


def _players_for(entries, club=CATS):
    players = {}
    for entry in entries:
        for pid in entry.roster.values():
            players[pid] = Player(canonical_player_id=pid, name=f"Player {pid}", current_team=club)
    return players


def _disposals_stats(entries, disposals_by_team_key):
    """Every Midfield1 player scores `disposals` (== total_score, since every
    other slot is left with zero stats), everything else is zero."""
    stats = {}
    for entry in entries:
        team_key = entry.team_key
        pid = entry.roster["Midfield1"]
        stats[pid] = stat_line(pid, disposals=disposals_by_team_key.get(team_key, 0))
    return stats


def test_ten_entries_are_scored_using_the_existing_bbbffl_rules(
    ten_entries, superscore_decisions, match
):
    """SuperScore must reuse score_position() via build_matchup_state, not a
    separate scoring implementation -- this checks the standard Forward
    formula (6*goals + behinds) applies exactly as it does for Grand Final."""
    entry = ten_entries[0]
    forward_pid = entry.roster["Forward1"]
    players = _players_for(ten_entries)
    stats = {500: {forward_pid: stat_line(forward_pid, goals=3, behinds=2)}}
    client = FakeAflClient([match], players, stats)

    result = build_superscore_state(client, ten_entries, superscore_decisions, SEASON, AFL_ROUND)

    team = next(t for t in result.teams if t.team_key == entry.team_key)
    fwd1 = next(p for p in team.positions if p.position == "Forward1")
    assert fwd1.calculated_score == 20  # 6*3 + 2, same formula as Grand Final
    assert fwd1.effective_score == 20


def test_ten_entries_load_and_score_independently(ten_entries, superscore_decisions, match):
    players = _players_for(ten_entries)
    disposals = {f"team_{n}": n * 10 for n in range(1, 11)}
    stats_by_pid = _disposals_stats(ten_entries, disposals)
    client = FakeAflClient([match], players, {500: stats_by_pid})

    result = build_superscore_state(client, ten_entries, superscore_decisions, SEASON, AFL_ROUND)

    assert len(result.teams) == 10
    for team in result.teams:
        expected = disposals[team.team_key]
        assert team.total_score == expected


def test_superscore_does_not_synthesise_head_to_head_matches(ten_entries, superscore_decisions, match):
    """SuperScoreResult has no leader/margin/pairing concept at all -- ten
    entries are compared directly, never folded into five fake matchups."""
    players = _players_for(ten_entries)
    client = FakeAflClient([match], players, {500: {}})

    result = build_superscore_state(client, ten_entries, superscore_decisions, SEASON, AFL_ROUND)

    assert not hasattr(result, "leader_team_key")
    assert not hasattr(result, "margin")
    assert len(result.teams) == 10
    assert len(result.standings) == 10


def test_entries_are_ranked_highest_to_lowest(ten_entries, superscore_decisions, match):
    players = _players_for(ten_entries)
    disposals = {f"team_{n}": n * 10 for n in range(1, 11)}  # team_10 highest
    client = FakeAflClient([match], players, {500: _disposals_stats(ten_entries, disposals)})

    result = build_superscore_state(client, ten_entries, superscore_decisions, SEASON, AFL_ROUND)

    scores = [s.total_score for s in result.standings]
    assert scores == sorted(scores, reverse=True)
    assert result.standings[0].team_key == "team_10"
    assert result.standings[0].rank == 1
    assert result.standings[-1].team_key == "team_1"


def test_tied_scores_remain_tied_with_no_tiebreaker(ten_entries, superscore_decisions, match):
    players = _players_for(ten_entries)
    disposals = {f"team_{n}": 5 for n in range(1, 11)}
    disposals["team_1"] = 100
    disposals["team_2"] = 100  # tie for first
    client = FakeAflClient([match], players, {500: _disposals_stats(ten_entries, disposals)})

    result = build_superscore_state(client, ten_entries, superscore_decisions, SEASON, AFL_ROUND)

    top_two = {s.team_key for s in result.standings if s.rank == 1}
    assert top_two == {"team_1", "team_2"}
    # Standard competition ranking: the next distinct score skips ahead
    # rather than fabricating a "rank 2".
    third = next(s for s in result.standings if s.team_key not in top_two)
    assert third.rank == 3


def test_superscore_scorer_decisions_are_independent_between_entries(
    ten_entries, superscore_decisions, match
):
    players = _players_for(ten_entries)
    client = FakeAflClient([match], players, {500: {}})

    superscore_decisions.set_dnp("team_1", "Forward1", True)
    superscore_decisions.set_override("team_2", "Ruck", 55.0, "scorer correction")

    result = build_superscore_state(client, ten_entries, superscore_decisions, SEASON, AFL_ROUND)

    team_1 = next(t for t in result.teams if t.team_key == "team_1")
    team_2 = next(t for t in result.teams if t.team_key == "team_2")
    team_3 = next(t for t in result.teams if t.team_key == "team_3")

    assert next(p for p in team_1.positions if p.position == "Forward1").match_state == "vacant"
    assert next(p for p in team_2.positions if p.position == "Ruck").effective_score == 55.0
    # Untouched entries see none of the above.
    assert next(p for p in team_3.positions if p.position == "Forward1").match_state != "vacant"
    assert next(p for p in team_3.positions if p.position == "Ruck").effective_score == 0


def test_superscore_reaches_awaiting_signoff_once_matches_complete(
    ten_entries, superscore_decisions
):
    final_match = Match(match_id=500, home_team=CATS, away_team=PIES, status="FINAL")
    players = _players_for(ten_entries)
    client = FakeAflClient([final_match], players, {500: {}})

    result = build_superscore_state(client, ten_entries, superscore_decisions, SEASON, AFL_ROUND)

    assert result.status == "AWAITING_SCORER_SIGNOFF"


def test_superscore_only_becomes_final_after_explicit_signoff(ten_entries, superscore_decisions):
    final_match = Match(match_id=500, home_team=CATS, away_team=PIES, status="FINAL")
    players = _players_for(ten_entries)
    client = FakeAflClient([final_match], players, {500: {}})

    pre = build_superscore_state(client, ten_entries, superscore_decisions, SEASON, AFL_ROUND)
    assert pre.status == "AWAITING_SCORER_SIGNOFF"

    import dataclasses

    superscore_decisions.finalize("SuperScore round confirmed", dataclasses.asdict(pre))
    post = build_superscore_state(client, ten_entries, superscore_decisions, SEASON, AFL_ROUND)

    assert post.status == "FINAL"
    assert post.finalized_note == "SuperScore round confirmed"


def test_superscore_stays_live_while_a_match_is_in_progress(ten_entries, superscore_decisions, match):
    players = _players_for(ten_entries)
    client = FakeAflClient([match], players, {500: {}})

    result = build_superscore_state(client, ten_entries, superscore_decisions, SEASON, AFL_ROUND)

    assert result.status == "LIVE"


def test_superscore_requests_the_configured_round_not_afl_apis_current_round(
    ten_entries, superscore_decisions, match
):
    """Regression: afl-api's 'current round' can move on (round rollover) or
    lag a freshly deployed config. SuperScore must always score the round
    declared in its own config (AFL_ROUND=20 here), never whatever afl-api
    happens to consider current (21 here) -- otherwise the leaderboard could
    score/finalize the wrong round's matches under this round's
    competition_key."""
    players = _players_for(ten_entries)
    client = FakeAflClient([match], players, {500: {}}, current_round_number=21, year=SEASON)

    build_superscore_state(client, ten_entries, superscore_decisions, SEASON, AFL_ROUND)

    assert client.get_round_calls == [(client._season.season_id, AFL_ROUND)]


def test_superscore_rejects_a_season_year_mismatch(ten_entries, superscore_decisions, match):
    """A stale SuperScore config (e.g. still declaring last year's season)
    must be rejected rather than silently scored against afl-api's actual
    current season."""
    players = _players_for(ten_entries)
    client = FakeAflClient([match], players, {500: {}}, year=SEASON + 1)

    with pytest.raises(RuntimeError):
        build_superscore_state(client, ten_entries, superscore_decisions, SEASON, AFL_ROUND)


def test_get_superscore_view_serves_frozen_snapshot_after_finalize(ten_entries, superscore_decisions):
    from app.afl_client import AflApiError

    final_match = Match(match_id=500, home_team=CATS, away_team=PIES, status="FINAL")
    players = _players_for(ten_entries)
    disposals = {f"team_{n}": n * 10 for n in range(1, 11)}
    client = FakeAflClient([final_match], players, {500: _disposals_stats(ten_entries, disposals)})

    pre = build_superscore_state(client, ten_entries, superscore_decisions, SEASON, AFL_ROUND)
    import dataclasses

    superscore_decisions.finalize("Signed off", dataclasses.asdict(pre))

    class ExplodingClient:
        def __getattr__(self, name):
            def _boom(*args, **kwargs):
                raise AflApiError(f"afl-api should not be called after finalize (called {name})")

            return _boom

    view = get_superscore_view(ExplodingClient(), ten_entries, superscore_decisions, SEASON, AFL_ROUND)
    assert view["status"] == "FINAL"
    assert view["standings"][0]["team_key"] == "team_10"
