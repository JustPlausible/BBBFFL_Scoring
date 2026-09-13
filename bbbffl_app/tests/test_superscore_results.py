"""Issue #193 acceptance coverage for entry scoring and leaderboard history."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text

from app.afl_client import Match, PlayerStatLine, Team
from app.audit import ActorContext
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.db import connect
from app.lineups import POSITIONS
from app.migrations import migrate
from app.season import _now
from app.superscore_results import SuperScoreCalculationService, SuperScoreLeaderboardService
from tests.superscore_helpers import build_superscore_ready_season, open_superscore_round


class Facts:
    def __init__(self, stats):
        self.stats = stats
        self.match = Match(9900, Team(1, "A"), Team(2, "B"), "completed")

    def get_matches(self, _round_id):
        return [self.match]

    def get_match_player_stats(self, _match_id):
        return self.stats


def _ready(year=6200, database=None):
    built = build_superscore_ready_season(year=year, **({"database": database} if database else {}))
    db, round_id = built["database"], built["superscore_rounds"][1]
    now, stats = _now(), {}
    with db.engine.begin() as conn:
        for index, entry in enumerate(built["entries"]):
            lineup_id = f"ss-lineup-{year}-{index}"
            canonical = 9_000_000 + year * 10 + index
            player_id = f"ss-player-{year}-{index}"
            conn.execute(
                text(
                    "INSERT INTO season_player_pool (season_player_id,season_id,canonical_player_id,display_name,afl_team_id,eligible,source_provider,source_fetched_at,created_at,updated_at) VALUES (:p,:s,:c,:n,1,TRUE,'test',:now,:now,:now)"
                ),
                {"p": player_id, "s": built["season"].season_id, "c": canonical, "n": f"SS Player {index}", "now": now},
            )
            conn.execute(
                text(
                    "INSERT INTO weekly_lineup (lineup_id,season_id,competition_id,bbbffl_round_id,season_entry_id,draft_revision,effective_submission_version,created_at,updated_at) VALUES (:l,:s,:c,:r,:e,1,1,:now,:now)"
                ),
                {
                    "l": lineup_id,
                    "s": built["season"].season_id,
                    "c": built["superscore_stream"].competition_id,
                    "r": round_id,
                    "e": entry.season_entry_id,
                    "now": now,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO weekly_lineup_submission (lineup_id,version,based_on_draft_revision,submitted_at,actor_type,actor_role,source_type) VALUES (:l,1,1,:now,'coach','coach','coach')"
                ),
                {"l": lineup_id, "now": now},
            )
            for position in POSITIONS:
                selected = player_id if position == "F1" else None
                conn.execute(
                    text("INSERT INTO weekly_lineup_submission_slot VALUES (:l,1,:pos,:p)"),
                    {"l": lineup_id, "pos": position, "p": selected},
                )
            stats[canonical] = PlayerStatLine(canonical, goals=index + 1)
    open_superscore_round(db, round_id)
    lifecycle = CompetitionLifecycleRepository(db)
    lifecycle.transition(round_id, "live")
    lifecycle.transition(round_id, "review")
    return built, round_id, stats


def test_ten_entries_calculate_without_matchups_and_record_review_version():
    built, round_id, stats = _ready()
    results = SuperScoreCalculationService(built["database"], Facts(stats)).calculate_round(round_id)
    assert len(results) == 10
    assert all(result.computed_as_of_review_version == 0 for result in results)
    assert (
        built["database"]
        .execute("SELECT COUNT(*) AS n FROM bbbffl_matchup WHERE bbbffl_round_id=?", (round_id,))
        .fetchone()["n"]
        == 0
    )


def test_publication_is_one_immutable_ten_entry_revision_with_joint_winners():
    built, round_id, stats = _ready(6201)
    # Give the top two equal evidence: equal highest scores are joint winners.
    top = sorted(stats)[-1]
    stats[sorted(stats)[-2]] = stats[top]
    service = SuperScoreLeaderboardService(built["database"], Facts(stats))
    actor = ActorContext("anonymous_operator", "scorer", "scorer")
    first = service.publish(round_id, actor=actor, reason="sign off SS1")
    assert len(first["entries"]) == 10
    assert sum(entry["is_joint_winner"] for entry in first["entries"]) == 2
    frozen = first["entries"][0]["input_snapshot"]

    stats[min(stats)] = PlayerStatLine(min(stats), goals=99)
    second = service.publish(round_id, actor=actor, reason="correct AFL evidence")
    assert second["version"] == 2
    assert len(second["entries"]) == 10
    assert service.leaderboard(round_id, version=1, include_inputs=True)["entries"][0]["input_snapshot"] == frozen
    assert [item["rank"] for item in second["entries"]] != [item["rank"] for item in first["entries"]]
    assert built["database"].execute("SELECT COUNT(*) AS n FROM bbbffl_official_result").fetchone()["n"] == 0


def test_postgresql_overlapping_entry_calculations_really_block():
    """The second evidence read cannot begin until the first transaction
    releases the entry review-state lock; this is a real row-lock test, not
    an assertion that SQL happens to contain ``FOR UPDATE``."""
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        import pytest

        pytest.skip("PostgreSQL concurrency semantics require BBBFFL_DATABASE_URL")
    migrate(url)
    database = connect(url)
    built, round_id, stats = _ready(6202, database)
    entry_id = built["entries"][0].season_entry_id
    first_read = threading.Event()
    release_first = threading.Event()
    reads = 0
    reads_lock = threading.Lock()

    class PausedFacts(Facts):
        def get_matches(self, round_id):
            nonlocal reads
            with reads_lock:
                reads += 1
                number = reads
            if number == 1:
                first_read.set()
                assert release_first.wait(timeout=5)
            return super().get_matches(round_id)

    service = SuperScoreCalculationService(database, PausedFacts(stats))
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.calculate_entry, round_id, entry_id)
        assert first_read.wait(timeout=5)
        second = executor.submit(service.calculate_entry, round_id, entry_id)
        assert not second.done()
        # It has not even reached AFL evidence: the row lock is acquired first.
        assert reads == 1
        release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert reads == 2
    database.close()
