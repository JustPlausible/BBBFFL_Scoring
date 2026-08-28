"""Shared complete-lineup fixtures for post-Issue-56 submission tests."""

from app.lineups import POSITIONS
from app.player_pool import OwnershipRepository, PlayerPoolRepository


def complete_lineup(database, scope, entry, overrides=None, *, neutral_team=None):
    """Fill all nine slots with owned season players, preserving overrides.

    Neutral players are stable per season entry and deliberately separate
    from scenario-specific players so changing F1/M1 cannot accidentally move
    that player into an unrelated filler slot.
    """
    positions = dict(overrides or {})
    missing = [position for position in POSITIONS if position not in positions]
    rows = database.execute(
        "SELECT p.* FROM season_player_pool p JOIN player_ownership_period o "
        "ON o.season_player_id=p.season_player_id WHERE o.season_entry_id=? "
        "AND o.released_at IS NULL AND p.display_name LIKE 'Validation Neutral %' "
        "ORDER BY p.display_name",
        (entry.season_entry_id,),
    ).fetchall()
    pool, ownership = PlayerPoolRepository(database), OwnershipRepository(database)
    neutrals = [pool.get_by_id(row["season_player_id"]) for row in rows]
    while len(neutrals) < len(missing):
        number = len(neutrals) + 1
        # Entry-derived numeric space avoids collisions between the two teams
        # in one season without relying on transient AFL player identifiers.
        entry_number = int(entry.season_entry_id.replace("-", "")[:8], 16) % 10_000
        canonical_id = 8_000_000 + entry_number * 100 + number
        player = pool.refresh_player(
            scope["season_id"],
            canonical_id,
            f"Validation Neutral {entry_number:04d}-{number:02d}",
            afl_team_id=neutral_team.team_id if neutral_team else None,
            afl_team_name=neutral_team.name if neutral_team else None,
        )
        ownership.acquire(player.season_player_id, entry.season_entry_id)
        neutrals.append(player)
    positions.update({position: neutrals[index].season_player_id for index, position in enumerate(missing)})
    return positions
