"""Issue #237: PostgreSQL serialization of the Season setup commands.
Concurrent duplicate requests must converge on exactly one structure (one
created, the other an explicit no-op), and Opening Round acceptance must
serialize against a concurrently completing preseason pick rather than
deciding "before Pick 1" on a stale snapshot. Skipped unless
`BBBFFL_DATABASE_URL` points at PostgreSQL (the CI postgres job)."""

from datetime import datetime, timezone
from threading import Barrier, Thread

from sqlalchemy import text

from app.db import connect
from app.draft import DraftRepository
from app.opening_round import OpeningRoundRuleRepository
from app.player_pool import PlayerPoolRepository
from app.season import SeasonRepository
from app.season_setup import (
    SeasonSetupError,
    accept_draft_order,
    accept_opening_round_rules,
    configure_squad_limit,
    initialize_ordinary_competition,
    refresh_player_pool,
)
from tests.season_setup_helpers import SCORER, SetupAfl, fresh_season
from tests.test_replay_bootstrap_concurrency import postgres_url  # noqa: F401 -- fixture

REASON = "issue #237 concurrency test"


def _race(postgres_url, action, count=2):  # noqa: F811
    barrier = Barrier(count)
    results, errors = [], []

    def run():
        database = connect(postgres_url)
        try:
            barrier.wait()
            results.append(action(database))
        except Exception as exc:  # noqa: BLE001 -- surfaced by the assertions below
            errors.append(exc)
        finally:
            database.close()

    threads = [Thread(target=run) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, errors


def test_concurrent_ordinary_initialization_creates_exactly_one_structure(postgres_url):  # noqa: F811
    database = connect(postgres_url)
    season, _entries = fresh_season(database)
    results, errors = _race(
        postgres_url,
        lambda db: initialize_ordinary_competition(db, season.season_id, actor=SCORER, reason=REASON),
    )
    assert errors == []
    assert sorted(result["created"] for result in results) == [False, True]
    [competition] = SeasonRepository(database).list_competitions(season.season_id)
    assert len(SeasonRepository(database).list_rounds(competition.competition_id)) == 20
    assert len(SeasonRepository(database).list_rules_versions(season.season_id)) == 1


def test_concurrent_identical_draft_order_acceptance_creates_one_draft(postgres_url):  # noqa: F811
    database = connect(postgres_url)
    season, entries = fresh_season(database)
    refresh_player_pool(database, SetupAfl(), season.season_id, 77, actor=SCORER, reason=REASON)
    initialize_ordinary_competition(database, season.season_id, actor=SCORER, reason=REASON)
    configure_squad_limit(database, season.season_id, 4, actor=SCORER, reason=REASON)
    order = [entry.season_entry_id for entry in entries]
    results, errors = _race(
        postgres_url, lambda db: accept_draft_order(db, season.season_id, order, actor=SCORER, reason=REASON)
    )
    assert errors == []
    assert sorted(result["created"] for result in results) == [False, True]
    assert DraftRepository(database).status(season.season_id).total_picks == 40


def test_opening_round_acceptance_blocks_behind_a_completing_pick_then_refuses(postgres_url):  # noqa: F811
    database = connect(postgres_url)
    season, entries = fresh_season(database)
    afl = SetupAfl()
    refresh_player_pool(database, afl, season.season_id, 77, actor=SCORER, reason=REASON)
    initialize_ordinary_competition(database, season.season_id, actor=SCORER, reason=REASON)
    configure_squad_limit(database, season.season_id, 4, actor=SCORER, reason=REASON)
    accept_draft_order(database, season.season_id, [e.season_entry_id for e in entries], actor=SCORER, reason=REASON)
    player = PlayerPoolRepository(database).list_available(season.season_id)[0]

    # Hold the exact lock DraftRepository._locked_draft takes before a pick.
    lock_conn = connect(postgres_url).engine.connect()
    lock_txn = lock_conn.begin()
    lock_conn.execute(text("SELECT * FROM season_draft WHERE season_id=:sid FOR UPDATE"), {"sid": season.season_id})

    outcome = {}

    def accept():
        db = connect(postgres_url)
        try:
            accept_opening_round_rules(
                db, afl, season.season_id, 77, {1: 2, 2: 2, 3: 3, 4: 4}, actor=SCORER, reason=REASON
            )
            outcome["result"] = "accepted"
        except SeasonSetupError as exc:
            outcome["result"] = str(exc)
        finally:
            db.close()

    thread = Thread(target=accept)
    thread.start()
    thread.join(timeout=1)
    assert thread.is_alive(), "Opening Round acceptance did not wait for the concurrently held draft lock"
    lock_conn.execute(
        text(
            "UPDATE draft_pick SET selected_season_player_id=:player, completed_at=:at "
            "WHERE draft_pick_id=(SELECT p.draft_pick_id FROM draft_pick p JOIN season_draft d "
            "ON d.draft_id=p.draft_id WHERE d.season_id=:sid AND p.completed_at IS NULL "
            "ORDER BY p.overall_number LIMIT 1)"
        ),
        {"player": player.season_player_id, "at": datetime.now(timezone.utc).isoformat(), "sid": season.season_id},
    )
    lock_txn.commit()
    lock_conn.close()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert "before-Pick-1" in outcome["result"]
    assert OpeningRoundRuleRepository(database).list_accepted_for_season(season.season_id) == []


def test_concurrent_finals_then_superscore_initialization_converge(postgres_url):  # noqa: F811
    from app.finals import FinalsBracketRepository
    from app.season_setup import initialize_finals, initialize_superscore
    from app.superscore_round import get_stream
    from tests.finals_seeding_helpers import build_2026_replay_season

    database = connect(postgres_url)
    season_id = build_2026_replay_season(database=database, year=2071)["season"].season_id
    results, errors = _race(postgres_url, lambda db: initialize_finals(db, season_id, actor=SCORER, reason=REASON))
    assert errors == []
    assert sorted(result["created"] for result in results) == [False, True]
    finals_streams = [c for c in SeasonRepository(database).list_competitions(season_id) if c.stream_type == "finals"]
    assert len(finals_streams) == 1
    assert FinalsBracketRepository(database).get_bracket(season_id, finals_streams[0].competition_id) is not None

    results, errors = _race(postgres_url, lambda db: initialize_superscore(db, season_id, actor=SCORER, reason=REASON))
    assert errors == []
    assert sorted(result["created"] for result in results) == [False, True]
    stream = get_stream(database, season_id)
    assert len(SeasonRepository(database).list_rounds(stream.competition_id)) == 4
