"""Orchestration: turns afl-api data + coach selections + scorer decisions
into the official BBBFFL Grand Final scoreboard.

    AFL stats -> calculated BBBFFL score -> optional scorer override -> effective BBBFFL score
    coach selection + AFL facts + scorer decisions -> official BBBFFL score

AFL statistics are treated as authoritative and are never modified here --
a scorer override changes only the resulting BBBFFL point score.
"""

import dataclasses
import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from app.afl_client import Match, MatchState, Player, PlayerStatLine, Team
from app.db import DecisionsRepository
from app.scoring import ROSTER_SLOTS, SCORABLE_POSITIONS, PlayerStats, score_position
from app.teams import TeamConfig

logger = logging.getLogger("bbbffl.service")

MatchupStatus = Literal["LIVE", "AWAITING_SCORER_SIGNOFF", "FINAL"]
PositionState = Literal["yet_to_play", "live", "completed", "dnp", "vacant"]


class AflDataSource(Protocol):
    """Duck-typed subset of AflApiClient this module depends on -- lets tests
    supply a fake without touching the network."""

    def get_current_season(self): ...

    def get_round(self, season_id: int, round_number: int): ...

    def get_matches(self, round_id: int) -> list[Match]: ...

    def get_player(self, canonical_player_id: int) -> Player: ...

    def get_match_player_stats(self, match_id: int) -> dict[int, PlayerStatLine]: ...


class PlayerIdentityCache:
    """Resolves canonical player identity/current team once per process
    lifetime rather than on every live refresh, per the brief."""

    def __init__(self, afl_client: AflDataSource):
        self._afl_client = afl_client
        self._cache: dict[int, Player] = {}

    def get(self, canonical_player_id: int) -> Player:
        if canonical_player_id not in self._cache:
            self._cache[canonical_player_id] = self._afl_client.get_player(canonical_player_id)
        return self._cache[canonical_player_id]


@dataclass(frozen=True)
class PositionResult:
    position: str
    slot_source: Literal["starting", "interchange", "vacant"]
    canonical_player_id: int | None
    player_name: str | None
    afl_club: str | None
    match_state: PositionState
    calculated_score: float
    override_score: float | None
    override_reason: str | None
    effective_score: float
    recommended_interchange: bool
    # The starting player's own DNP decision, independent of slot_source --
    # e.g. still true even once an interchange has been assigned to cover
    # this position, so the admin UI can render and clear it correctly
    # without first having to remove the interchange assignment.
    starting_dnp: bool


@dataclass(frozen=True)
class InterchangeInfo:
    canonical_player_id: int
    player_name: str
    afl_club: str
    dnp: bool
    target_position: str | None


@dataclass(frozen=True)
class TeamResult:
    team_key: str
    name: str
    positions: list[PositionResult]
    interchange: InterchangeInfo
    total_score: float


@dataclass(frozen=True)
class MatchupResult:
    status: MatchupStatus
    teams: list[TeamResult]
    finalized_at: str | None
    finalized_note: str | None
    leader_team_key: str | None
    margin: float
    counts: dict[PositionState, int]


def _team_name(team_by_player: dict[int, Team], player_id: int | None) -> str | None:
    if player_id is None:
        return None
    team = team_by_player.get(player_id)
    return team.name if team else None


def _match_for_player(
    team_by_player: dict[int, Team],
    matches_by_team_id: dict[int, Match],
    player_id: int | None,
) -> Match | None:
    if player_id is None:
        return None
    team = team_by_player.get(player_id)
    if team is None:
        return None
    return matches_by_team_id.get(team.team_id)


def _stats_to_player_stats(stat_line: PlayerStatLine | None) -> PlayerStats:
    if stat_line is None:
        return PlayerStats()
    return PlayerStats(
        goals=stat_line.goals,
        behinds=stat_line.behinds,
        disposals=stat_line.disposals,
        marks=stat_line.marks,
        hitouts=stat_line.hitouts,
        tackles=stat_line.tackles,
    )


def build_matchup_state(
    afl_client: AflDataSource,
    teams: list[TeamConfig],
    decisions: DecisionsRepository,
    identity_cache: PlayerIdentityCache | None = None,
) -> MatchupResult:
    identity_cache = identity_cache or PlayerIdentityCache(afl_client)

    season = afl_client.get_current_season()
    if season.current_round_number is None:
        raise RuntimeError("afl-api current season has no current_round_number")
    round_ = afl_client.get_round(season.season_id, season.current_round_number)
    matches = afl_client.get_matches(round_.round_id)
    # Matched on team_id, not name -- names are display-only and afl-api
    # does not guarantee they're a stable join key the way team_id is.
    matches_by_team_id: dict[int, Match] = {}
    for match in matches:
        matches_by_team_id[match.home_team.team_id] = match
        matches_by_team_id[match.away_team.team_id] = match

    dnp_map = decisions.get_dnp_map()
    interchange_assignments = decisions.get_interchange_assignments()
    overrides = decisions.get_overrides()

    # Resolve identity for every rostered player once, then work out which
    # unique AFL matches are actually needed before fetching any stats.
    team_by_player: dict[int, Team] = {}
    name_by_player: dict[int, str] = {}
    for team in teams:
        for player_id in team.roster.values():
            player = identity_cache.get(player_id)
            team_by_player[player_id] = player.current_team
            name_by_player[player_id] = player.name

    needed_match_ids: set[int] = set()
    for team in teams:
        assignment = interchange_assignments.get(team.team_key)
        interchange_dnp = dnp_map.get((team.team_key, "Interchange"), False)
        for position in SCORABLE_POSITIONS:
            starting_dnp = dnp_map.get((team.team_key, position), False)
            using_interchange = assignment and assignment.target_position == position
            if using_interchange:
                effective_id = team.roster["Interchange"]
                if interchange_dnp:
                    continue
            elif starting_dnp:
                continue
            else:
                effective_id = team.roster[position]
            match = _match_for_player(team_by_player, matches_by_team_id, effective_id)
            if match:
                needed_match_ids.add(match.match_id)

    stats_by_match: dict[int, dict[int, PlayerStatLine]] = {
        match_id: afl_client.get_match_player_stats(match_id) for match_id in needed_match_ids
    }

    team_results: list[TeamResult] = []
    counts: dict[PositionState, int] = {
        "yet_to_play": 0,
        "live": 0,
        "completed": 0,
        "dnp": 0,
        "vacant": 0,
    }

    for team in teams:
        assignment = interchange_assignments.get(team.team_key)
        interchange_target = assignment.target_position if assignment else None
        interchange_dnp = dnp_map.get((team.team_key, "Interchange"), False)
        interchange_id = team.roster["Interchange"]

        position_results: list[PositionResult] = []
        total_score = 0.0

        for position in SCORABLE_POSITIONS:
            starting_dnp = dnp_map.get((team.team_key, position), False)
            using_interchange = interchange_target == position
            override = overrides.get((team.team_key, position))

            if using_interchange:
                slot_source: Literal["starting", "interchange", "vacant"] = "interchange"
                player_id: int | None = interchange_id
                club = _team_name(team_by_player, interchange_id)
                if interchange_dnp:
                    match_state: PositionState = "dnp"
                    calculated_score = 0.0
                else:
                    match = _match_for_player(team_by_player, matches_by_team_id, interchange_id)
                    match_state = match.state if match else "yet_to_play"
                    stat_line = (
                        stats_by_match.get(match.match_id, {}).get(interchange_id) if match else None
                    )
                    calculated_score = score_position(position, _stats_to_player_stats(stat_line))
                recommended = False
            elif starting_dnp:
                slot_source = "vacant"
                player_id = None
                club = None
                match_state = "vacant"
                calculated_score = 0.0
                recommended = interchange_target is None and not interchange_dnp
            else:
                slot_source = "starting"
                player_id = team.roster[position]
                club = _team_name(team_by_player, player_id)
                match = _match_for_player(team_by_player, matches_by_team_id, player_id)
                match_state = match.state if match else "yet_to_play"
                stat_line = stats_by_match.get(match.match_id, {}).get(player_id) if match else None
                calculated_score = score_position(position, _stats_to_player_stats(stat_line))
                recommended = False

            effective_score = (
                override.override_score
                if override and override.override_score is not None
                else calculated_score
            )
            total_score += effective_score
            counts[match_state] += 1

            position_results.append(
                PositionResult(
                    position=position,
                    slot_source=slot_source,
                    canonical_player_id=player_id,
                    player_name=name_by_player.get(player_id) if player_id else None,
                    afl_club=club,
                    match_state=match_state,
                    calculated_score=calculated_score,
                    override_score=override.override_score if override else None,
                    override_reason=override.reason if override else None,
                    effective_score=effective_score,
                    recommended_interchange=recommended,
                    starting_dnp=starting_dnp,
                )
            )

        interchange_info = InterchangeInfo(
            canonical_player_id=interchange_id,
            player_name=name_by_player.get(interchange_id, ""),
            afl_club=_team_name(team_by_player, interchange_id) or "",
            dnp=interchange_dnp,
            target_position=interchange_target,
        )

        team_results.append(
            TeamResult(
                team_key=team.team_key,
                name=team.name,
                positions=position_results,
                interchange=interchange_info,
                total_score=total_score,
            )
        )

    matchup_state = decisions.get_matchup_state()

    # A match "counts" toward finalisation only if some active (non-DNP,
    # non-vacant) position actually depends on it.
    relevant_match_states: set[MatchState] = set()
    for team_result in team_results:
        for pos in team_result.positions:
            if pos.match_state in ("yet_to_play", "live", "completed"):
                relevant_match_states.add(pos.match_state)

    all_relevant_final = relevant_match_states.issubset({"completed"}) if relevant_match_states else True

    if matchup_state.finalized:
        status: MatchupStatus = "FINAL"
    elif all_relevant_final:
        status = "AWAITING_SCORER_SIGNOFF"
    else:
        status = "LIVE"

    leader_team_key = None
    margin = 0.0
    if len(team_results) == 2:
        a, b = team_results
        if a.total_score != b.total_score:
            leader_team_key = a.team_key if a.total_score > b.total_score else b.team_key
        margin = abs(a.total_score - b.total_score)

    return MatchupResult(
        status=status,
        teams=team_results,
        finalized_at=matchup_state.finalized_at,
        finalized_note=matchup_state.finalized_note,
        leader_team_key=leader_team_key,
        margin=margin,
        counts=counts,
    )


def get_matchup_view(
    afl_client: AflDataSource,
    teams: list[TeamConfig],
    decisions: DecisionsRepository,
    identity_cache: PlayerIdentityCache | None = None,
) -> dict:
    """The dict form of the matchup state that routes should serve.

    Once the Grand Final has been finalised with a stored snapshot (see
    routes/admin.py's finalize endpoint and db.py's finalize()), this is
    served directly from that frozen snapshot and afl-api is never queried
    again -- so a post-signoff stats correction, round/season rollover, or
    afl-api outage cannot change or hide an already-FINAL result. Before
    finalisation, this always reflects the live state.
    """
    matchup_state = decisions.get_matchup_state()
    if matchup_state.finalized and matchup_state.snapshot is not None:
        return matchup_state.snapshot
    result = build_matchup_state(afl_client, teams, decisions, identity_cache)
    return dataclasses.asdict(result)
