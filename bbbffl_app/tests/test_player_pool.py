"""Season isolation, cache/authority boundary and ownership invariants."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.audit import AuditEventRepository
from app.identity import IdentityRepository
from app.player_pool import (
    OwnershipRepository,
    PlayerPoolRepository,
    PlayerUnavailableError,
    SquadCapacityError,
)
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection


def setup_domain(limit=2):
    db = migrated_connection()
    seasons = SeasonRepository(db)
    identities = IdentityRepository(db)
    season = seasons.create_season(2027, "2027")
    coaches = [identities.create_coach(name) for name in ("One", "Two")]
    entries = [
        identities.create_entry(season.season_id, f"licence-{i}", coach.coach_id, f"Team {i}")
        for i, coach in enumerate(coaches)
    ]
    ownership = OwnershipRepository(db)
    ownership.configure_squad_limit(season.season_id, limit)
    return db, season, entries, PlayerPoolRepository(db), ownership


def test_concurrent_acquisition_has_exactly_one_winner():
    _db, season, entries, pool, ownership = setup_domain()
    player = pool.refresh_player(season.season_id, 396, "Nick Daicos")

    def attempt(entry):
        try:
            return ownership.acquire(
                player.season_player_id,
                entry.season_entry_id,
                effective_at="2027-03-01T00:00:00+00:00",
            )
        except PlayerUnavailableError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, entries))
    assert sum(result is not None for result in results) == 1
    assert len(ownership.history(player.season_player_id)) == 1


def test_release_transfer_and_reacquisition_preserve_non_overlapping_history():
    _db, season, entries, pool, ownership = setup_domain()
    player = pool.refresh_player(season.season_id, 396, "Nick Daicos")
    ownership.acquire(player.season_player_id, entries[0].season_entry_id, effective_at="2027-01-01")
    ownership.transfer(player.season_player_id, entries[1].season_entry_id, effective_at="2027-02-01")
    ownership.release(player.season_player_id, effective_at="2027-03-01")
    ownership.acquire(player.season_player_id, entries[0].season_entry_id, effective_at="2027-04-01")
    history = ownership.history(player.season_player_id)
    assert [(x.season_entry_id, x.acquired_at, x.released_at) for x in history] == [
        (entries[0].season_entry_id, "2027-01-01", "2027-02-01"),
        (entries[1].season_entry_id, "2027-02-01", "2027-03-01"),
        (entries[0].season_entry_id, "2027-04-01", None),
    ]
    assert ownership.owner_at(player.season_player_id, "2027-02-15").season_entry_id == entries[1].season_entry_id
    assert ownership.owner_at(player.season_player_id, "2027-03-15") is None


def test_canonical_player_is_independent_between_replay_and_live_seasons():
    db, live, _entries, pool, _ownership = setup_domain()
    replay = SeasonRepository(db).create_season(2026, "2026 Replay")
    old = pool.refresh_player(replay.season_id, 396, "Nick Daicos", afl_team_name="Collingwood 2026")
    new = pool.refresh_player(live.season_id, 396, "Nick Daicos", afl_team_name="Collingwood 2027")
    assert old.season_player_id != new.season_player_id
    assert pool.get(replay.season_id, 396).afl_team_name == "Collingwood 2026"
    assert [p.canonical_player_id for p in pool.list_selectable(live.season_id)] == [396]


def test_squad_capacity_rejects_excess_player():
    _db, season, entries, pool, ownership = setup_domain(limit=1)
    first = pool.refresh_player(season.season_id, 396, "First")
    second = pool.refresh_player(season.season_id, 584, "Second")
    ownership.acquire(first.season_player_id, entries[0].season_entry_id, effective_at="2027-01-01")
    with pytest.raises(SquadCapacityError):
        ownership.acquire(
            second.season_player_id,
            entries[0].season_entry_id,
            effective_at="2027-01-02",
        )


def test_backdated_acquisition_cannot_overfill_squad_at_future_boundary():
    _db, season, entries, pool, ownership = setup_domain(limit=1)
    february = pool.refresh_player(season.season_id, 396, "February player")
    january = pool.refresh_player(season.season_id, 584, "January player")
    ownership.acquire(
        february.season_player_id,
        entries[0].season_entry_id,
        effective_at="2027-02-01",
    )

    with pytest.raises(SquadCapacityError):
        ownership.acquire(
            january.season_player_id,
            entries[0].season_entry_id,
            effective_at="2027-01-01",
        )
    assert ownership.squad_at(entries[0].season_entry_id, "2027-02-01") == [
        ownership.history(february.season_player_id)[0]
    ]


def test_backdated_transfer_cannot_overfill_destination_at_future_boundary():
    _db, season, entries, pool, ownership = setup_domain(limit=1)
    future = pool.refresh_player(season.season_id, 396, "Future player")
    transferred = pool.refresh_player(season.season_id, 584, "Transferred player")
    ownership.acquire(
        future.season_player_id,
        entries[1].season_entry_id,
        effective_at="2027-02-01",
    )
    ownership.acquire(
        transferred.season_player_id,
        entries[0].season_entry_id,
        effective_at="2026-12-01",
    )

    with pytest.raises(SquadCapacityError):
        ownership.transfer(
            transferred.season_player_id,
            entries[1].season_entry_id,
            effective_at="2027-01-01",
        )
    assert ownership.owner_at(transferred.season_player_id, "2027-03-01").season_entry_id == entries[0].season_entry_id


def test_squad_limit_reduction_rejects_persisted_oversize_squad():
    db, season, entries, pool, ownership = setup_domain(limit=2)
    players = [
        pool.refresh_player(season.season_id, player_id, name) for player_id, name in ((396, "First"), (584, "Second"))
    ]
    for player in players:
        ownership.acquire(
            player.season_player_id,
            entries[0].season_entry_id,
            effective_at="2027-01-01",
        )
    # Even after one player is released, the season-wide configuration must
    # not retroactively make the persisted January squad invalid.
    ownership.release(players[1].season_player_id, effective_at="2027-02-01")

    with pytest.raises(SquadCapacityError):
        ownership.configure_squad_limit(season.season_id, 1)
    configured = db.execute(
        "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?",
        (season.season_id,),
    ).fetchone()
    assert configured["squad_limit"] == 2


def test_afl_club_refresh_does_not_change_ownership_or_audit_history():
    db, season, entries, pool, ownership = setup_domain()
    player = pool.refresh_player(
        season.season_id,
        396,
        "Player",
        afl_team_id=1,
        afl_team_name="Old Club",
        source_fetched_at="2027-01-01",
    )
    ownership.acquire(player.season_player_id, entries[0].season_entry_id, effective_at="2027-02-01")
    before = ownership.history(player.season_player_id)
    event_count = len(AuditEventRepository(db).list_events(entity_type="ownership.period"))
    refreshed = pool.refresh_player(
        season.season_id,
        396,
        "Player",
        afl_team_id=2,
        afl_team_name="New Club",
        source_fetched_at="2027-02-02",
        source_updated_at="2027-02-02",
    )
    assert refreshed.season_player_id == player.season_player_id
    assert refreshed.afl_team_name == "New Club"
    assert ownership.history(player.season_player_id) == before
    assert len(AuditEventRepository(db).list_events(entity_type="ownership.period")) == event_count


def test_database_rejects_cross_season_ownership():
    db, season, entries, pool, ownership = setup_domain()
    other = SeasonRepository(db).create_season(2026, "2026")
    player = pool.refresh_player(other.season_id, 396, "Player")
    with pytest.raises(ValueError, match="same season"):
        ownership.acquire(player.season_player_id, entries[0].season_entry_id)


def test_ownership_events_are_correlated_and_append_only():
    db, season, entries, pool, ownership = setup_domain()
    player = pool.refresh_player(season.season_id, 396, "Player")
    ownership.acquire(player.season_player_id, entries[0].season_entry_id, effective_at="2027-01-01")
    ownership.transfer(
        player.season_player_id,
        entries[1].season_entry_id,
        effective_at="2027-02-01",
        reason="approved trade",
    )
    events = AuditEventRepository(db).list_events(entity_type="ownership.period")
    assert [event.action for event in events] == [
        "ownership.player.acquired",
        "ownership.player.released",
        "ownership.player.acquired",
    ]
    assert events[-1].correlation_id == events[-2].correlation_id
    assert events[-1].reason == "approved trade"
