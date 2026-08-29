"""Shared setup for app.round_review tests: a persisted round with fully
named, submitted lineups (all nine slots, one player each) for every one
of the five matchups' ten entries, and a fake AFL client whose facts cover
every named player by default -- so a happy-path test starts from "nothing
ambiguous, nothing to rule on" and can flip individual slots to DNP/
unknown/zero-stats deliberately, the same way tests/test_calculations.py's
`setup_round`/`Facts` do for the plain calculation engine.
"""

from sqlalchemy import text

from app.afl_client import Match, PlayerStatLine, Team
from app.lineups import POSITIONS
from app.season import _now
from tests.db_helpers import migrated_connection
from tests.test_competition_lifecycle import operational


class Facts:
    """Duck-typed AflDataSource: one match covers every named player
    (all seeded at afl_team_id=1), CONCLUDED by default so calculated
    snapshots are immediately eligible for the review's evidence checks."""

    def __init__(self, stats, status="CONCLUDED"):
        self.stats = stats
        self.match = Match(700, Team(1, "A"), Team(2, "B"), status)

    def get_matches(self, round_id):
        return [self.match]

    def get_match_player_stats(self, match_id):
        return self.stats

    def get_rounds(self, season_id):
        return []


def full_round(db=None, *, year=2200, afl_round=100, stat_line=None):
    """Ten entries, five matchups, every one of the nine roster slots
    named with a distinct player. `stat_line(canonical_player_id)` returns
    the `PlayerStatLine` for each player (default: modest non-zero stats
    for every position's formula, so nothing reads as DNP-ambiguous)."""
    db = db or migrated_connection()
    lifecycle, round_, entries = operational(db, year, afl_round)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    scope = db.execute(
        "SELECT c.season_id,c.competition_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()
    stat_line = stat_line or (
        lambda canonical: PlayerStatLine(canonical, goals=3, behinds=2, disposals=18, marks=6, hitouts=4, tackles=5)
    )
    now = _now()
    stats: dict[int, PlayerStatLine] = {}
    players: dict[tuple[str, str], str] = {}
    canonical_by_slot: dict[tuple[str, str], int] = {}
    canonical = 10_000 * (year % 1000)
    with db.engine.begin() as conn:
        for index, entry in enumerate(entries):
            lineup_id = f"lineup-{year}-{afl_round}-{index}"
            conn.execute(
                text(
                    "INSERT INTO weekly_lineup "
                    "(lineup_id, season_id, competition_id, bbbffl_round_id, "
                    "season_entry_id, draft_revision, effective_submission_version, "
                    "created_at, updated_at) "
                    "VALUES (:lineup_id, :season_id, :competition_id, :round_id, "
                    ":entry_id, 1, 1, :now, :now)"
                ),
                {
                    "lineup_id": lineup_id,
                    "season_id": scope["season_id"],
                    "competition_id": scope["competition_id"],
                    "round_id": round_.bbbffl_round_id,
                    "entry_id": entry.season_entry_id,
                    "now": now,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO weekly_lineup_submission "
                    "(lineup_id, version, based_on_draft_revision, submitted_at, "
                    "actor_type, actor_id, actor_role, source_type, source_detail, reason) "
                    "VALUES (:lineup_id, 1, 1, :now, 'coach', NULL, 'coach', "
                    "'coach', NULL, NULL)"
                ),
                {"lineup_id": lineup_id, "now": now},
            )
            for position in POSITIONS:
                canonical += 1
                player_id = f"player-{year}-{afl_round}-{canonical}"
                players[(entry.season_entry_id, position)] = player_id
                canonical_by_slot[(entry.season_entry_id, position)] = canonical
                conn.execute(
                    text(
                        "INSERT INTO season_player_pool "
                        "(season_player_id, season_id, canonical_player_id, "
                        "display_name, afl_team_id, afl_team_name, eligible, "
                        "source_provider, source_fetched_at, source_updated_at, "
                        "created_at, updated_at) "
                        "VALUES (:player_id, :season_id, :canonical_id, :name, "
                        "1, NULL, TRUE, 'afl-api', :now, NULL, :now, :now)"
                    ),
                    {
                        "player_id": player_id,
                        "season_id": scope["season_id"],
                        "canonical_id": canonical,
                        "name": f"P{canonical}",
                        "now": now,
                    },
                )
                stats[canonical] = stat_line(canonical)
                conn.execute(
                    text(
                        "INSERT INTO weekly_lineup_draft_slot "
                        "(lineup_id, position, season_player_id) "
                        "VALUES (:lineup_id, :position, :player_id)"
                    ),
                    {"lineup_id": lineup_id, "position": position, "player_id": player_id},
                )
                conn.execute(
                    text(
                        "INSERT INTO weekly_lineup_submission_slot "
                        "(lineup_id, version, position, season_player_id) "
                        "VALUES (:lineup_id, 1, :position, :player_id)"
                    ),
                    {"lineup_id": lineup_id, "position": position, "player_id": player_id},
                )
    return db, lifecycle, round_, entries, stats, canonical_by_slot


def progress_to_review(lifecycle, round_id):
    lifecycle.transition(round_id, "live")
    lifecycle.transition(round_id, "review")
