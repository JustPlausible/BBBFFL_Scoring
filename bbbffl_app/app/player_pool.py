"""Season-scoped cached AFL player facts and authoritative BBBFFL ownership.

Canonical IDs and AFL club details are cache data obtained through afl-api's
public contract.  Ownership periods are BBBFFL state and are changed only in
transactions that append an audit event.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from app.audit import (
    ENTITY_TYPE_OWNERSHIP_PERIOD,
    PLAYER_ACQUIRED,
    PLAYER_RELEASED,
    ActorContext,
    append_event,
    new_correlation_id,
)
from app.db import _for_update_suffix, transaction


def _id():
    return str(uuid4())


def _now():
    return datetime.now(timezone.utc).isoformat()


class PlayerUnavailableError(ValueError):
    pass


class SquadCapacityError(ValueError):
    pass


@dataclass(frozen=True)
class SeasonPlayer:
    season_player_id: str
    season_id: str
    canonical_player_id: int
    display_name: str
    afl_team_id: int | None
    afl_team_name: str | None
    eligible: bool
    source_provider: str
    source_fetched_at: str
    source_updated_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class OwnershipPeriod:
    ownership_period_id: str
    season_player_id: str
    season_id: str
    season_entry_id: str
    acquired_at: str
    released_at: str | None
    reason: str | None
    created_at: str


def _player(row):
    values = dict(row)
    values["eligible"] = bool(values["eligible"])
    return SeasonPlayer(**values)


class PlayerPoolRepository:
    def __init__(self, database):
        self.database = database

    def refresh_player(
        self,
        season_id,
        canonical_player_id,
        display_name,
        *,
        afl_team_id=None,
        afl_team_name=None,
        eligible=True,
        source_provider="afl-api-v1",
        source_fetched_at=None,
        source_updated_at=None,
    ):
        """Upsert public afl-api facts without touching ownership history."""
        fetched = source_fetched_at or _now()
        with transaction(self.database) as conn:
            existing = conn.execute(
                "SELECT * FROM season_player_pool WHERE season_id=? AND canonical_player_id=?"
                + _for_update_suffix(self.database),
                (season_id, canonical_player_id),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE season_player_pool SET display_name=?, afl_team_id=?, afl_team_name=?, eligible=?, source_provider=?, source_fetched_at=?, source_updated_at=?, updated_at=? WHERE season_player_id=?",
                    (
                        display_name,
                        afl_team_id,
                        afl_team_name,
                        bool(eligible),
                        source_provider,
                        fetched,
                        source_updated_at,
                        fetched,
                        existing["season_player_id"],
                    ),
                )
                player_id, created = (
                    existing["season_player_id"],
                    existing["created_at"],
                )
            else:
                player_id = _id()
                created = fetched
                conn.execute(
                    "INSERT INTO season_player_pool VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        player_id,
                        season_id,
                        canonical_player_id,
                        display_name,
                        afl_team_id,
                        afl_team_name,
                        bool(eligible),
                        source_provider,
                        fetched,
                        source_updated_at,
                        created,
                        fetched,
                    ),
                )
        return SeasonPlayer(
            player_id,
            season_id,
            canonical_player_id,
            display_name,
            afl_team_id,
            afl_team_name,
            eligible,
            source_provider,
            fetched,
            source_updated_at,
            created,
            fetched,
        )

    def list_selectable(self, season_id):
        rows = self.database.execute(
            "SELECT * FROM season_player_pool WHERE season_id=? AND eligible=TRUE ORDER BY display_name, canonical_player_id",
            (season_id,),
        ).fetchall()
        return [_player(row) for row in rows]

    def get(self, season_id, canonical_player_id):
        row = self.database.execute(
            "SELECT * FROM season_player_pool WHERE season_id=? AND canonical_player_id=?",
            (season_id, canonical_player_id),
        ).fetchone()
        return _player(row) if row else None


class OwnershipRepository:
    def __init__(self, database):
        self.database = database

    def configure_squad_limit(
        self,
        season_id,
        squad_limit,
        *,
        actor=ActorContext.anonymous_operator("admin"),
        reason=None,
    ):
        if squad_limit <= 0:
            raise ValueError("squad limit must be positive")
        with transaction(self.database) as conn:
            # Serialize configuration changes with acquisitions, which lock a
            # season entry before checking capacity. SQLite's no-op write takes
            # its writer lock before any validation snapshot.
            if self.database.engine.dialect.name == "sqlite":
                conn.execute(
                    "UPDATE bbbffl_season SET updated_at=updated_at WHERE season_id=?",
                    (season_id,),
                )
            existing = conn.execute(
                "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?"
                + _for_update_suffix(self.database),
                (season_id,),
            ).fetchone()
            entries = conn.execute(
                "SELECT season_entry_id FROM season_entry WHERE season_id=?" + _for_update_suffix(self.database),
                (season_id,),
            ).fetchall()
            for entry in entries:
                maximum = self._maximum_squad_size(entry["season_entry_id"], connection=conn)
                if maximum > squad_limit:
                    raise SquadCapacityError(
                        f"squad limit of {squad_limit} is below persisted squad "
                        f"size {maximum} for entry {entry['season_entry_id']}"
                    )
            conn.execute(
                "INSERT INTO season_squad_configuration VALUES (?, ?, ?) ON CONFLICT(season_id) DO UPDATE SET squad_limit=excluded.squad_limit, updated_at=excluded.updated_at",
                (season_id, squad_limit, _now()),
            )
            append_event(
                conn,
                actor=actor,
                action="ownership.squad_limit.configured",
                entity_type="season.squad_configuration",
                entity_id=season_id,
                reason=reason,
                before_state={"squad_limit": existing["squad_limit"]} if existing else None,
                after_state={"squad_limit": squad_limit},
            )

    def _maximum_squad_size(self, season_entry_id, *, from_at=None, additional=0, connection=None):
        """Maximum effective squad size at all acquisition boundaries.

        Counts use half-open ownership periods. When ``additional`` is set,
        it represents a proposed open-ended period beginning at ``from_at``.
        Checking every later acquisition boundary is sufficient because squad
        size can only increase at those boundaries (releases only decrease it).
        """
        conn = connection or self.database
        boundaries = [from_at] if from_at is not None else []
        if from_at is None:
            rows = conn.execute(
                "SELECT DISTINCT acquired_at FROM player_ownership_period WHERE season_entry_id=? ORDER BY acquired_at",
                (season_entry_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT acquired_at FROM player_ownership_period "
                "WHERE season_entry_id=? AND acquired_at>=? ORDER BY acquired_at",
                (season_entry_id, from_at),
            ).fetchall()
        boundaries.extend(row["acquired_at"] for row in rows)
        maximum = 0
        for boundary in dict.fromkeys(boundaries):
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM player_ownership_period "
                "WHERE season_entry_id=? AND acquired_at<=? "
                "AND (released_at IS NULL OR released_at>?)",
                (season_entry_id, boundary, boundary),
            ).fetchone()["n"]
            maximum = max(maximum, count + additional)
        return maximum

    def validate_squad_capacity(self, season_entry_id, *, effective_at, additional=1, connection=None):
        conn = connection or self.database
        entry = conn.execute(
            "SELECT season_id FROM season_entry WHERE season_entry_id=?",
            (season_entry_id,),
        ).fetchone()
        if not entry:
            raise KeyError(season_entry_id)
        config = conn.execute(
            "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?",
            (entry["season_id"],),
        ).fetchone()
        if not config:
            raise ValueError("season squad limit is not configured")
        maximum = self._maximum_squad_size(
            season_entry_id,
            from_at=effective_at,
            additional=additional,
            connection=conn,
        )
        if maximum > config["squad_limit"]:
            raise SquadCapacityError(f"squad limit of {config['squad_limit']} would be exceeded")
        return config["squad_limit"] - maximum

    def acquire(
        self,
        season_player_id,
        season_entry_id,
        *,
        effective_at=None,
        actor=ActorContext.anonymous_operator("admin"),
        reason=None,
        correlation_id=None,
    ):
        with transaction(self.database) as conn:
            item = self.acquire_in_transaction(
                conn, season_player_id, season_entry_id, effective_at=effective_at,
                actor=actor, reason=reason, correlation_id=correlation_id,
            )
        return item

    def acquire_in_transaction(
        self, conn, season_player_id, season_entry_id, *, effective_at=None,
        actor=ActorContext.anonymous_operator("admin"), reason=None, correlation_id=None,
    ):
        """Acquire using an existing transaction (for compound domain commands)."""
        at = effective_at or _now()
        correlation_id = correlation_id or new_correlation_id()
        if self.database.engine.dialect.name == "sqlite":
            conn.execute("UPDATE season_player_pool SET updated_at=updated_at WHERE season_player_id=?", (season_player_id,))
            conn.execute("UPDATE season_entry SET created_at=created_at WHERE season_entry_id=?", (season_entry_id,))
        player = conn.execute(
            "SELECT season_id, eligible FROM season_player_pool WHERE season_player_id=?" + _for_update_suffix(self.database),
            (season_player_id,),
        ).fetchone()
        entry = conn.execute(
            "SELECT season_id FROM season_entry WHERE season_entry_id=?" + _for_update_suffix(self.database),
            (season_entry_id,),
        ).fetchone()
        if not player or not entry:
            raise KeyError(season_player_id if not player else season_entry_id)
        if entry["season_id"] != player["season_id"]:
            raise ValueError("player and entry must belong to the same season")
        if not player["eligible"]:
            raise PlayerUnavailableError("player is not selectable")
        overlap = conn.execute(
            "SELECT 1 FROM player_ownership_period WHERE season_player_id=? AND acquired_at<=? AND (released_at IS NULL OR released_at>?)",
            (season_player_id, at, at),
        ).fetchone()
        if overlap:
            raise PlayerUnavailableError("player is already owned at the effective time")
        self.validate_squad_capacity(season_entry_id, effective_at=at, connection=conn)
        item = OwnershipPeriod(_id(), season_player_id, player["season_id"], season_entry_id, at, None, reason, _now())
        conn.execute("INSERT INTO player_ownership_period VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(item.__dict__.values()))
        append_event(
            conn, actor=actor, action=PLAYER_ACQUIRED, entity_type=ENTITY_TYPE_OWNERSHIP_PERIOD,
            entity_id=item.ownership_period_id, correlation_id=correlation_id, reason=reason,
            after_state={"season_player_id": season_player_id, "season_entry_id": season_entry_id, "acquired_at": at},
        )
        return item

    def release(
        self,
        season_player_id,
        *,
        effective_at=None,
        actor=ActorContext.anonymous_operator("admin"),
        reason=None,
        correlation_id=None,
    ):
        at = effective_at or _now()
        correlation_id = correlation_id or new_correlation_id()
        with transaction(self.database) as conn:
            if self.database.engine.dialect.name == "sqlite":
                conn.execute(
                    "UPDATE season_player_pool SET updated_at=updated_at WHERE season_player_id=?",
                    (season_player_id,),
                )
            conn.execute(
                "SELECT season_player_id FROM season_player_pool WHERE season_player_id=?"
                + _for_update_suffix(self.database),
                (season_player_id,),
            ).fetchone()
            current = conn.execute(
                "SELECT * FROM player_ownership_period WHERE season_player_id=? AND released_at IS NULL",
                (season_player_id,),
            ).fetchone()
            if not current:
                raise PlayerUnavailableError("player is not currently owned")
            if at <= current["acquired_at"]:
                raise ValueError("release must be after acquisition")
            conn.execute(
                "UPDATE player_ownership_period SET released_at=? WHERE ownership_period_id=?",
                (at, current["ownership_period_id"]),
            )
            append_event(
                conn,
                actor=actor,
                action=PLAYER_RELEASED,
                entity_type=ENTITY_TYPE_OWNERSHIP_PERIOD,
                entity_id=current["ownership_period_id"],
                correlation_id=correlation_id,
                reason=reason,
                before_state={"released_at": None},
                after_state={"released_at": at},
                payload={
                    "season_player_id": season_player_id,
                    "season_entry_id": current["season_entry_id"],
                },
            )
        values = dict(current)
        values["released_at"] = at
        return OwnershipPeriod(**values)

    def transfer(
        self,
        season_player_id,
        to_entry_id,
        *,
        effective_at=None,
        actor=ActorContext.anonymous_operator("admin"),
        reason=None,
    ):
        """A correlated release/acquisition. Both writes succeed or neither does."""
        at = effective_at or _now()
        correlation = new_correlation_id()
        # A single outer transaction is needed, so implement the pair directly.
        with transaction(self.database) as conn:
            if self.database.engine.dialect.name == "sqlite":
                conn.execute(
                    "UPDATE season_player_pool SET updated_at=updated_at WHERE season_player_id=?",
                    (season_player_id,),
                )
                conn.execute(
                    "UPDATE season_entry SET created_at=created_at WHERE season_entry_id=?",
                    (to_entry_id,),
                )
            player = conn.execute(
                "SELECT season_id, eligible FROM season_player_pool WHERE season_player_id=?"
                + _for_update_suffix(self.database),
                (season_player_id,),
            ).fetchone()
            current = conn.execute(
                "SELECT * FROM player_ownership_period WHERE season_player_id=? AND released_at IS NULL",
                (season_player_id,),
            ).fetchone()
            entry = conn.execute(
                "SELECT season_id FROM season_entry WHERE season_entry_id=?" + _for_update_suffix(self.database),
                (to_entry_id,),
            ).fetchone()
            if not player or not current or not entry:
                raise PlayerUnavailableError("player, current owner, or destination is unavailable")
            if entry["season_id"] != player["season_id"]:
                raise ValueError("player and entry must belong to the same season")
            if not player["eligible"]:
                raise PlayerUnavailableError("player is not selectable")
            if at <= current["acquired_at"]:
                raise ValueError("transfer must be after acquisition")
            self.validate_squad_capacity(to_entry_id, effective_at=at, connection=conn)
            conn.execute(
                "UPDATE player_ownership_period SET released_at=? WHERE ownership_period_id=?",
                (at, current["ownership_period_id"]),
            )
            item = OwnershipPeriod(
                _id(),
                season_player_id,
                player["season_id"],
                to_entry_id,
                at,
                None,
                reason,
                _now(),
            )
            conn.execute(
                "INSERT INTO player_ownership_period VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(item.__dict__.values()),
            )
            append_event(
                conn,
                actor=actor,
                action=PLAYER_RELEASED,
                entity_type=ENTITY_TYPE_OWNERSHIP_PERIOD,
                entity_id=current["ownership_period_id"],
                correlation_id=correlation,
                reason=reason,
                before_state={"released_at": None},
                after_state={"released_at": at},
            )
            append_event(
                conn,
                actor=actor,
                action=PLAYER_ACQUIRED,
                entity_type=ENTITY_TYPE_OWNERSHIP_PERIOD,
                entity_id=item.ownership_period_id,
                correlation_id=correlation,
                reason=reason,
                after_state={
                    "season_player_id": season_player_id,
                    "season_entry_id": to_entry_id,
                    "acquired_at": at,
                },
            )
        return item

    def owner_at(self, season_player_id, effective_at):
        row = self.database.execute(
            "SELECT * FROM player_ownership_period WHERE season_player_id=? AND acquired_at<=? AND (released_at IS NULL OR released_at>?) ORDER BY acquired_at DESC",
            (season_player_id, effective_at, effective_at),
        ).fetchone()
        return OwnershipPeriod(**dict(row)) if row else None

    def history(self, season_player_id):
        rows = self.database.execute(
            "SELECT * FROM player_ownership_period WHERE season_player_id=? ORDER BY acquired_at",
            (season_player_id,),
        ).fetchall()
        return [OwnershipPeriod(**dict(row)) for row in rows]

    def squad_at(self, season_entry_id, effective_at):
        rows = self.database.execute(
            "SELECT * FROM player_ownership_period WHERE season_entry_id=? AND acquired_at<=? AND (released_at IS NULL OR released_at>?) ORDER BY acquired_at",
            (season_entry_id, effective_at, effective_at),
        ).fetchall()
        return [OwnershipPeriod(**dict(row)) for row in rows]
