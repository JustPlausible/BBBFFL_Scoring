"""Five-matchup acceptance coverage for persisted season scoring."""

from sqlalchemy import text

from app.afl_client import Match, PlayerStatLine, Team
from app.calculations import MatchupCalculationService
from app.db import transaction
from app.lineups import POSITIONS
from app.season import _now
from tests import afl_evidence
from tests.db_helpers import migrated_connection
from tests.test_competition_lifecycle import operational


class Facts:
    def __init__(self, stats):
        self.stats = stats
        self.match = Match(700, Team(1, "A"), Team(2, "B"), "LIVE")

    def get_matches(self, round_id):
        return [self.match]

    def get_match_player_stats(self, match_id):
        return self.stats


def setup_round(db=None, *, year=2027):
    db = db or migrated_connection()
    lifecycle, round_, entries = operational(db, year, 77)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    scope = db.execute(
        "SELECT c.season_id,c.competition_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()
    stats = {}
    now = _now()
    with db.engine.begin() as conn:
        for index, entry in enumerate(entries):
            lineup_id = f"lineup-{year}-{index}"
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
                player = None
                if position in ("F1", "Interchange"):
                    canonical = 1000 + index * 2 + (position == "Interchange")
                    player = f"player-{year}-{canonical}"
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
                            "player_id": player,
                            "season_id": scope["season_id"],
                            "canonical_id": canonical,
                            "name": f"P{canonical}",
                            "now": now,
                        },
                    )
                    if index != 0 or position != "F1":
                        stats[canonical] = PlayerStatLine(canonical, goals=index + 1)
                conn.execute(
                    text(
                        "INSERT INTO weekly_lineup_draft_slot "
                        "(lineup_id, position, season_player_id) "
                        "VALUES (:lineup_id, :position, :player_id)"
                    ),
                    {
                        "lineup_id": lineup_id,
                        "position": position,
                        "player_id": player,
                    },
                )
                conn.execute(
                    text(
                        "INSERT INTO weekly_lineup_submission_slot "
                        "(lineup_id, version, position, season_player_id) "
                        "VALUES (:lineup_id, 1, :position, :player_id)"
                    ),
                    {
                        "lineup_id": lineup_id,
                        "position": position,
                        "player_id": player,
                    },
                )
    return db, lifecycle, round_, stats


def test_persisted_round_calculates_five_idempotent_matchups_with_slot_evidence():
    db, lifecycle, round_, stats = setup_round()
    service = MatchupCalculationService(db, Facts(stats))
    first = service.calculate_round(
        round_.bbbffl_round_id, upstream_revision="stats-1", observed_at="2027-03-01T00:00:00Z"
    )
    repeated = service.calculate_round(
        round_.bbbffl_round_id, upstream_revision="stats-1", observed_at="2027-03-01T00:01:00Z"
    )
    assert len(first) == len(repeated) == 5
    assert [item.input_fingerprint for item in first] == [item.input_fingerprint for item in repeated]
    assert [item.revision for item in repeated] == [1] * 5
    assert db.execute("SELECT COUNT(*) AS n FROM bbbffl_matchup_calculation").fetchone()["n"] == 5
    slots = first[0].snapshot["home"]["slots"] + first[0].snapshot["away"]["slots"]
    assert any(slot["season_player_id"] and not slot["played"] for slot in slots)
    assert any(slot["interchange_available"] and slot["season_player_id"] for slot in slots)
    assert all(lifecycle.effective_result(item.matchup_id) is None for item in first)

    # A mutable draft edit is not part of the immutable submission input.
    with transaction(db) as conn:
        conn.execute("UPDATE weekly_lineup_draft_slot SET season_player_id=NULL WHERE position='F1'")
    after_draft = service.calculate_round(round_.bbbffl_round_id, upstream_revision="stats-1")
    assert [item.revision for item in after_draft] == [1] * 5

    # Only the matchup containing this canonical player receives a new revision.
    changed_player = next(iter(stats))
    stats[changed_player] = PlayerStatLine(changed_player, goals=99)
    changed = service.calculate_round(round_.bbbffl_round_id, upstream_revision="stats-2")
    assert sum(item.revision == 2 for item in changed) == 1


def test_two_rule_versions_drive_the_shared_scoring_core():
    from app.scoring import PlayerStats, ScoringRules, score_position

    facts = PlayerStats(goals=2, behinds=1)
    assert score_position("Forward1", facts, ScoringRules.from_dict(None)) == 13
    assert score_position("Forward1", facts, ScoringRules.from_dict({"forward_goal": 10})) == 21


def test_partial_upstream_stat_is_retained_and_slot_score_remains_unresolved():
    db, _, round_, stats = setup_round()
    player_id = next(player_id for player_id in stats if player_id % 2 == 0)
    stats[player_id] = PlayerStatLine(player_id, goals=2, behinds=None)

    calculations = MatchupCalculationService(db, Facts(stats)).calculate_round(round_.bbbffl_round_id)
    evidence = [
        slot
        for calculation in calculations
        for side in ("home", "away")
        for slot in calculation.snapshot[side]["slots"]
        if slot["canonical_player_id"] == player_id
    ]
    assert evidence[0]["stats"]["behinds"] is None
    assert evidence[0]["score"] is None


# ---------------------------------------------------------------------------
# Replay-oriented: round/per-match scoring driven by curated AFL evidence
# fixtures (issue #40 / roadmap package 08), not a hand-built Facts stub.
# ---------------------------------------------------------------------------


def _seed_evidence_lineup(conn, scope, round_id, entry_id, side, position_players, now):
    """Raw-SQL lineup seeding for one matchup side, mirroring setup_round()
    above but scoped to only the scorable positions a curated-evidence test
    cares about (see tests/afl_evidence.py's provenance for canonical_player_id
    / afl_team_id values, which must match the fixture's own team_ids)."""
    lineup_id = f"lineup-evidence-{side}"
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
            "round_id": round_id,
            "entry_id": entry_id,
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
    for position, (canonical_id, team_id) in position_players.items():
        player_id = f"player-evidence-{side}-{canonical_id}"
        conn.execute(
            text(
                "INSERT INTO season_player_pool "
                "(season_player_id, season_id, canonical_player_id, "
                "display_name, afl_team_id, afl_team_name, eligible, "
                "source_provider, source_fetched_at, source_updated_at, "
                "created_at, updated_at) "
                "VALUES (:player_id, :season_id, :canonical_id, :name, "
                ":team_id, NULL, TRUE, 'afl-api', :now, NULL, :now, :now)"
            ),
            {
                "player_id": player_id,
                "season_id": scope["season_id"],
                "canonical_id": canonical_id,
                "name": f"Evidence Player {canonical_id}",
                "team_id": team_id,
                "now": now,
            },
        )
        conn.execute(
            text(
                "INSERT INTO weekly_lineup_draft_slot (lineup_id, position, season_player_id) "
                "VALUES (:lineup_id, :position, :player_id)"
            ),
            {"lineup_id": lineup_id, "position": position, "player_id": player_id},
        )
        conn.execute(
            text(
                "INSERT INTO weekly_lineup_submission_slot (lineup_id, version, position, season_player_id) "
                "VALUES (:lineup_id, 1, :position, :player_id)"
            ),
            {"lineup_id": lineup_id, "position": position, "player_id": player_id},
        )


def test_calculate_matchup_scores_from_curated_afl_evidence_fixtures():
    """Proves roadmap package 08's evidence corpus is useful to #35's round/
    per-match scoring, not just to `tests/test_afl_evidence.py`'s loader
    tests. `client` is a real `AflApiClient` wired to
    `tests/fixtures/afl_evidence/v1/synthetic/season_85/round_1500/` via
    `httpx.MockTransport` (see `tests.afl_evidence`) -- `MatchupCalculationService`
    receives it exactly as it would the production client, with no network
    call possible.

    Round 1500's fixture match 9503 (Hawthorn v Adelaide, CONCLUDED, final
    stats) carries one player per BBBFFL scorable position type: a home
    Forward (goals=4, behinds=2 -> 26) and Midfield (disposals=29 -> 29),
    and an away Ruck (marks=6, hitouts=34 -> 40) and Tackler (tackles=9 ->
    54) -- see that fixture's own provenance notes for the full stat lines.
    """
    db = migrated_connection()
    lifecycle, round_, entries = operational(db, 2027, 1500)
    matchup = lifecycle.list_matchups(round_.bbbffl_round_id)[0]
    scope = db.execute(
        "SELECT c.season_id,c.competition_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()

    now = _now()
    with db.engine.begin() as conn:
        _seed_evidence_lineup(
            conn,
            scope,
            round_.bbbffl_round_id,
            matchup.home_season_entry_id,
            "home",
            {"F1": (9701, 6005), "M1": (9702, 6005)},
            now,
        )
        _seed_evidence_lineup(
            conn,
            scope,
            round_.bbbffl_round_id,
            matchup.away_season_entry_id,
            "away",
            {"Ruck": (9703, 6006), "Tackler": (9704, 6006)},
            now,
        )

    client = afl_evidence.build_client(
        {
            "/api/v1/rounds/1500/matches": "v1/synthetic/season_85/round_1500/matches.json",
            "/api/v1/matches/9501/player-stats": "v1/synthetic/season_85/round_1500/match_9501/player_stats.json",
            "/api/v1/matches/9502/player-stats": "v1/synthetic/season_85/round_1500/match_9502/player_stats.json",
            "/api/v1/matches/9503/player-stats": "v1/synthetic/season_85/round_1500/match_9503/player_stats.json",
        }
    )
    try:
        result = MatchupCalculationService(db, client).calculate_matchup(
            matchup.matchup_id, upstream_revision="evidence-fixture", observed_at="2026-07-06T00:00:00Z"
        )
    finally:
        client.close()

    assert result.snapshot["home"]["score"] == 4 * 6 + 2 + 29
    assert result.snapshot["away"]["score"] == 6 + 34 + 9 * 6
