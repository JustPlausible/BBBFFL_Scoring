"""PostgreSQL-specific draft invariants enforced by locks and triggers."""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db import connect
from app.draft import DraftRepository
from app.identity import IdentityRepository
from app.migrations import migrate
from app.player_pool import (
    OwnershipRepository,
    PlayerPoolRepository,
    SquadConfigurationFrozenError,
)
from app.season import SeasonRepository


@pytest.fixture(scope="module")
def postgres_draft():
    url = os.getenv("BBBFFL_DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("PostgreSQL draft semantics require BBBFFL_DATABASE_URL")
    migrate(url)
    database = connect(url)
    season = SeasonRepository(database).create_season(2201, "2201 draft trigger")
    identities = IdentityRepository(database)
    entries = []
    for number in range(2):
        coach = identities.create_coach(f"Draft trigger coach {number}")
        entries.append(
            identities.create_entry(
                season.season_id,
                f"draft-trigger-{number}",
                coach.coach_id,
                f"Draft trigger team {number}",
            )
        )
    ownership = OwnershipRepository(database)
    ownership.configure_squad_limit(season.season_id, 1)
    player = PlayerPoolRepository(database).refresh_player(
        season.season_id,
        2201001,
        "Draft trigger player",
    )
    draft = DraftRepository(database)
    draft.accept_order(
        season.season_id,
        [entry.season_entry_id for entry in entries],
    )
    completed = draft.execute_pick(
        season.season_id,
        entries[0].season_entry_id,
        player.season_player_id,
    )
    yield database, season, entries, ownership, draft, completed
    database.close()


def test_postgresql_accepted_draft_freezes_squad_capacity(postgres_draft):
    database, season, entries, ownership, draft, _completed = postgres_draft

    ownership.configure_squad_limit(season.season_id, 1)
    with pytest.raises(SquadConfigurationFrozenError, match="cannot change"):
        ownership.configure_squad_limit(season.season_id, 2)

    assert len(draft.picks(season.season_id)) == len(entries)
    configuration = database.execute(
        "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?",
        (season.season_id,),
    ).fetchone()
    accepted = database.execute(
        "SELECT target_squad_size FROM season_draft WHERE season_id=?",
        (season.season_id,),
    ).fetchone()
    assert configuration["squad_limit"] == accepted["target_squad_size"] == 1


def test_postgresql_rejects_deleting_completed_pick(postgres_draft):
    database, _season, _entries, _ownership, _draft, completed = postgres_draft

    with pytest.raises(IntegrityError, match="accepted/completed draft history is immutable"):
        with database.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM draft_pick WHERE draft_pick_id=:pick_id"),
                {"pick_id": completed.draft_pick_id},
            )
