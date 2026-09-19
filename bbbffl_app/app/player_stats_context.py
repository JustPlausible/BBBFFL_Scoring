"""Read-only player scoring context for the shared draft player browser
(issue #181).

Neither the preseason nor the mid-season draft engine has ever needed a
denormalised per-player season aggregate -- scoring is computed and
persisted per matchup/round in `bbbffl_matchup_calculation.snapshot`
(`app.calculations`), keyed by `canonical_player_id` inside each snapshot's
JSON slots, not by a dedicated indexed column. This module is a thin,
best-effort presentation aggregate over that existing data: it never
writes anything, is never consulted by any ownership/eligibility/
availability decision, and returns an empty result rather than raising
when no calculated rounds exist yet (a brand-new season, or one with no
prior completed season) -- informational context only, exactly like
`app.player_pool.SeasonPlayerPoolItem.diagnostic`.

Two contexts, matching the issue's phase-appropriate requirement:

- `current_season_points(season_id)` -- every round already calculated
  *this* season, i.e. genuinely "to date" for a mid-season draft.
- `previous_completed_season_points(season_id)` -- every round calculated
  for the most recent *other* season with `lifecycle_state='completed'`
  and an earlier `year`, for a preseason draft's "how did this player
  perform last year" context.

Aggregation is by `canonical_player_id` (stable across seasons -- see
`app.player_pool`'s own cross-season identity tests), so a previous
season's `season_player_id` (a different row entirely) never needs to be
resolved.
"""

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class PlayerSeasonStats:
    canonical_player_id: int
    games: int
    total_points: float
    average_points: float


def _aggregate(rows) -> dict[int, PlayerSeasonStats]:
    totals: dict[int, list[float]] = {}
    for row in rows:
        try:
            snapshot = json.loads(row["snapshot"])
        except (TypeError, ValueError):
            continue
        for side in ("home", "away"):
            entry = snapshot.get(side) or {}
            for slot in entry.get("slots") or []:
                canonical_player_id = slot.get("canonical_player_id")
                score = slot.get("score")
                if canonical_player_id is None or not slot.get("played") or score is None:
                    continue
                totals.setdefault(canonical_player_id, []).append(float(score))
    return {
        canonical_player_id: PlayerSeasonStats(
            canonical_player_id=canonical_player_id,
            games=len(scores),
            total_points=round(sum(scores), 2),
            average_points=round(sum(scores) / len(scores), 2),
        )
        for canonical_player_id, scores in totals.items()
    }


class PlayerStatsContext:
    def __init__(self, database):
        self.database = database

    def current_season_points(self, season_id) -> dict[int, PlayerSeasonStats]:
        rows = self.database.execute(
            "SELECT snapshot FROM bbbffl_matchup_calculation WHERE season_id=?", (season_id,)
        ).fetchall()
        return _aggregate(rows)

    def previous_completed_season_points(self, season_id) -> tuple[int | None, dict[int, PlayerSeasonStats]]:
        """Returns `(previous_season_year, stats)`; `(None, {})` when this
        season has no earlier completed season on record."""
        season = self.database.execute("SELECT year FROM bbbffl_season WHERE season_id=?", (season_id,)).fetchone()
        if not season:
            return None, {}
        previous = self.database.execute(
            "SELECT season_id, year FROM bbbffl_season WHERE lifecycle_state='completed' AND year<? "
            "ORDER BY year DESC LIMIT 1",
            (season["year"],),
        ).fetchone()
        if not previous:
            return None, {}
        rows = self.database.execute(
            "SELECT snapshot FROM bbbffl_matchup_calculation WHERE season_id=?", (previous["season_id"],)
        ).fetchall()
        return previous["year"], _aggregate(rows)
