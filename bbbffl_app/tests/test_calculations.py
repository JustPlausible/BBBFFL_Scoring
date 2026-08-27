"""Five-matchup acceptance coverage for persisted season scoring."""

from app.afl_client import Match, PlayerStatLine, Team
from app.calculations import MatchupCalculationService
from app.db import transaction
from app.lineups import POSITIONS
from app.season import _now
from tests.test_competition_lifecycle import operational
from tests.db_helpers import migrated_connection


class Facts:
    def __init__(self, stats):
        self.stats = stats
        self.match = Match(700, Team(1, "A"), Team(2, "B"), "LIVE")

    def get_matches(self, round_id):
        return [self.match]

    def get_match_player_stats(self, match_id):
        return self.stats


def setup_round():
    db = migrated_connection()
    lifecycle, round_, entries = operational(db, 2027, 77)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    scope = db.execute("SELECT c.season_id,c.competition_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?", (round_.bbbffl_round_id,)).fetchone()
    stats = {}
    now = _now()
    with db.engine.begin() as conn:
        for index, entry in enumerate(entries):
            lineup_id = f"lineup-{index}"
            conn.exec_driver_sql("INSERT INTO weekly_lineup VALUES (?,?,?,?,?,?,1,?,?)", (lineup_id, scope["season_id"], scope["competition_id"], round_.bbbffl_round_id, entry.season_entry_id, 1, now, now))
            conn.exec_driver_sql("INSERT INTO weekly_lineup_submission VALUES (?,1,1,?,'coach',NULL,'coach','coach',NULL,NULL)", (lineup_id, now))
            for position in POSITIONS:
                player = None
                if position in ("F1", "Interchange"):
                    canonical = 1000 + index * 2 + (position == "Interchange")
                    player = f"player-{canonical}"
                    conn.exec_driver_sql("INSERT INTO season_player_pool VALUES (?,?,?,?,?,NULL,1,'afl-api',?,NULL,?,?)", (player, scope["season_id"], canonical, f"P{canonical}", 1, now, now, now))
                    if index != 0 or position != "F1":
                        stats[canonical] = PlayerStatLine(canonical, goals=index + 1)
                conn.exec_driver_sql("INSERT INTO weekly_lineup_draft_slot VALUES (?,?,?)", (lineup_id, position, player))
                conn.exec_driver_sql("INSERT INTO weekly_lineup_submission_slot VALUES (?,1,?,?)", (lineup_id, position, player))
    return db, lifecycle, round_, stats


def test_persisted_round_calculates_five_idempotent_matchups_with_slot_evidence():
    db, lifecycle, round_, stats = setup_round()
    service = MatchupCalculationService(db, Facts(stats))
    first = service.calculate_round(round_.bbbffl_round_id, upstream_revision="stats-1", observed_at="2027-03-01T00:00:00Z")
    repeated = service.calculate_round(round_.bbbffl_round_id, upstream_revision="stats-1", observed_at="2027-03-01T00:01:00Z")
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
