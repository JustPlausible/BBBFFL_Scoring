"""Deterministic BBBFFL ladder read model.

The ladder is deliberately rebuilt from effective, versioned official results;
it is not a mutable competition counter.  Sporting order has exactly three
criteria: competition points, percentage, and points for.
"""

from dataclasses import dataclass
from decimal import Decimal, localcontext


@dataclass(frozen=True)
class OfficialMatchupInput:
    round_number: int
    matchup_id: str
    official_version: int
    home_entry_id: str
    away_entry_id: str
    home_score: Decimal
    away_score: Decimal


@dataclass(frozen=True)
class ResultReference:
    matchup_id: str
    official_version: int


@dataclass(frozen=True)
class LadderRow:
    season_entry_id: str
    played: int
    wins: int
    draws: int
    losses: int
    points_for: Decimal
    points_against: Decimal
    points_per_game: Decimal
    percentage: Decimal
    competition_points: int
    rank: int
    tied: bool
    tie_group: tuple[str, ...]


@dataclass(frozen=True)
class LadderSnapshot:
    season_id: str
    competition_id: str
    through_round: int
    rows: tuple[LadderRow, ...]
    result_references: tuple[ResultReference, ...]


def _ratio(numerator: Decimal, denominator: Decimal, multiplier: Decimal = Decimal(1)) -> Decimal:
    # The historical workbook explicitly renders zero when PA is zero.
    if denominator == 0:
        return Decimal(0)
    with localcontext() as context:
        context.prec = 28
        return numerator / denominator * multiplier


def calculate_ladder(
    season_id: str,
    competition_id: str,
    through_round: int,
    entry_ids: list[str] | tuple[str, ...],
    results: list[OfficialMatchupInput] | tuple[OfficialMatchupInput, ...],
) -> LadderSnapshot:
    """Rebuild a snapshot from effective official inputs only.

    Entry-id ordering is used solely for repeatable serialization inside an
    unresolved tie.  All tied rows share their sporting ``rank`` and expose
    the complete ``tie_group``; entry id is not a fourth sporting criterion.
    """
    stats = {
        entry_id: {"p": 0, "w": 0, "d": 0, "l": 0, "pf": Decimal(0), "pa": Decimal(0), "cp": 0}
        for entry_id in entry_ids
    }
    selected = sorted(
        (result for result in results if result.round_number <= through_round),
        key=lambda result: (result.round_number, result.matchup_id, result.official_version),
    )
    for result in selected:
        home, away = stats[result.home_entry_id], stats[result.away_entry_id]
        home["p"] += 1
        away["p"] += 1
        home["pf"] += result.home_score
        home["pa"] += result.away_score
        away["pf"] += result.away_score
        away["pa"] += result.home_score
        if result.home_score > result.away_score:
            home["w"] += 1
            away["l"] += 1
            home["cp"] += 4
        elif result.home_score < result.away_score:
            away["w"] += 1
            home["l"] += 1
            away["cp"] += 4
        else:
            home["d"] += 1
            away["d"] += 1
            home["cp"] += 2
            away["cp"] += 2

    values = []
    for entry_id, value in stats.items():
        percentage = _ratio(value["pf"], value["pa"], Decimal(100))
        values.append((entry_id, value, percentage))
    values.sort(key=lambda item: (-item[1]["cp"], -item[2], -item[1]["pf"], item[0]))

    groups: dict[tuple, tuple[str, ...]] = {}
    for entry_id, value, percentage in values:
        key = (value["cp"], percentage, value["pf"])
        groups.setdefault(key, tuple())
        groups[key] += (entry_id,)

    rows = []
    prior_key = None
    rank = 0
    for index, (entry_id, value, percentage) in enumerate(values, 1):
        key = (value["cp"], percentage, value["pf"])
        if key != prior_key:
            rank = index
        group = groups[key]
        rows.append(
            LadderRow(
                entry_id,
                value["p"],
                value["w"],
                value["d"],
                value["l"],
                value["pf"],
                value["pa"],
                _ratio(value["pf"], Decimal(value["p"])),
                percentage,
                value["cp"],
                rank,
                len(group) > 1,
                group,
            )
        )
        prior_key = key
    references = tuple(ResultReference(result.matchup_id, result.official_version) for result in selected)
    return LadderSnapshot(season_id, competition_id, through_round, tuple(rows), references)


class LadderRepository:
    """Season-scoped query adapter for the existing official-result lifecycle."""

    def __init__(self, database):
        self.database = database

    def snapshot(self, competition_id: str, through_round: int) -> LadderSnapshot:
        competition = self.database.execute(
            "SELECT season_id, stream_type FROM competition_stream WHERE competition_id=?",
            (competition_id,),
        ).fetchone()
        if competition is None or competition["stream_type"] != "ordinary":
            raise KeyError(competition_id)
        season_id = competition["season_id"]
        entries = self.database.execute(
            "SELECT season_entry_id FROM season_entry WHERE season_id=? ORDER BY season_entry_id", (season_id,)
        ).fetchall()
        if not entries:
            raise KeyError(season_id)
        rows = self.database.execute(
            """
            SELECT l.fixture_round_number, m.matchup_id, m.effective_official_version,
                   m.home_season_entry_id, m.away_season_entry_id, r.home_score, r.away_score
            FROM bbbffl_round_lifecycle l
            JOIN bbbffl_matchup m ON m.bbbffl_round_id=l.bbbffl_round_id
            JOIN bbbffl_official_result r ON r.matchup_id=m.matchup_id
                 AND r.version=m.effective_official_version
            WHERE l.season_id=? AND l.competition_id=? AND l.state='final'
                  AND l.fixture_round_number<=?
            ORDER BY l.fixture_round_number, m.matchup_id
            """,
            (season_id, competition_id, through_round),
        ).fetchall()
        results = [
            OfficialMatchupInput(
                row["fixture_round_number"],
                row["matchup_id"],
                row["effective_official_version"],
                row["home_season_entry_id"],
                row["away_season_entry_id"],
                Decimal(str(row["home_score"])),
                Decimal(str(row["away_score"])),
            )
            for row in rows
        ]
        return calculate_ladder(
            season_id,
            competition_id,
            through_round,
            [row["season_entry_id"] for row in entries],
            results,
        )
