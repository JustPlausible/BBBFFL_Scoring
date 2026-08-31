"""Season-scoped cached AFL player facts and authoritative BBBFFL ownership.

Canonical IDs and AFL club details are cache data obtained through afl-api's
public contract.  Ownership periods are BBBFFL state and are changed only in
transactions that append an audit event.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

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


class SquadConfigurationFrozenError(ValueError):
    pass


class PreseasonWindowClosedError(ValueError):
    """Raised by the ordinary ownership mutation paths below once roadmap
    package 15 (issue #54)'s preseason window has been explicitly closed for
    a player/entry's season.

    Defined here, not in `app.preseason`, so `acquire_in_transaction`/
    `release_in_transaction`/`transfer` can enforce it directly without a
    circular import (`app.preseason` already imports `OwnershipRepository`
    from this module) -- this is deliberately the *one* place that decides
    whether ordinary ownership mutation is still allowed for a season, so a
    future caller cannot bypass the lifecycle by going around
    `app.preseason.PreseasonRepository` straight to this repository.
    """


def _assert_ownership_mutation_allowed(conn, database, season_id, *, allow_closed_window=False):
    """No row for `season_id` means no preseason window has ever opened for
    it (e.g. a season still mid-draft, or one that predates this package) --
    that must not block the draft's own ownership acquisitions. Once a
    window exists and has been closed, only the frozen opening snapshot
    (`app.preseason`) remains authoritative; ordinary acquire/release/
    transfer calls -- including any future caller that has not gone through
    `app.preseason.PreseasonRepository` -- are refused here, not only in a
    route or UI layer.

    The read is taken under `_for_update_suffix`'s row lock (a no-op on
    SQLite, which serializes via its single writer lock instead, same as
    everywhere else in this module) so an ordinary mutation cannot read
    `closed_at` while still open, race a concurrent `close_window`, and
    commit its change *after* closure -- which would both defeat the
    closed-window guarantee and leave the just-frozen opening snapshot
    silently stale. Locking here serializes with `close_window`'s own
    `SELECT ... FOR UPDATE` on the same row.

    `allow_closed_window` is a narrow, explicit escape hatch for exactly one
    caller: `app.preseason.PreseasonRepository.correct_opening_snapshot`'s
    authorised post-closure correction, which deliberately still needs
    every other ownership invariant (eligibility, season match, squad
    capacity, no overlap) enforced. It is not exposed by `acquire`/
    `release`/`transfer` -- only their `_in_transaction` counterparts -- so
    an ordinary caller cannot opt into bypassing a closed window."""
    if allow_closed_window:
        return
    window = conn.execute(
        "SELECT closed_at FROM season_preseason_window WHERE season_id=?" + _for_update_suffix(database),
        (season_id,),
    ).fetchone()
    if window is not None and window["closed_at"] is not None:
        raise PreseasonWindowClosedError(
            "preseason window is closed for this season; ownership can no longer be changed directly"
        )


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


@dataclass(frozen=True)
class SeasonPlayerPoolItem:
    """Read model for the operational browser; never an ownership authority."""

    season_player_id: str
    season_id: str
    canonical_player_id: int
    display_name: str
    afl_team_id: int | None
    afl_team_name: str | None
    eligible: bool
    availability: str
    owner_season_entry_id: str | None
    owner_team_name: str | None
    diagnostic: str | None


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

    def get_by_id(self, season_player_id):
        row = self.database.execute(
            "SELECT * FROM season_player_pool WHERE season_player_id=?",
            (season_player_id,),
        ).fetchone()
        return _player(row) if row else None

    def list_available(self, season_id):
        """Eligible season players with no currently-open ownership period --
        i.e. what a draft selection screen must offer (see app.draft), not
        merely `list_selectable`'s eligibility-only view."""
        rows = self.database.execute(
            "SELECT p.* FROM season_player_pool p WHERE p.season_id=? AND p.eligible=TRUE "
            "AND NOT EXISTS (SELECT 1 FROM player_ownership_period o "
            "WHERE o.season_player_id=p.season_player_id AND o.released_at IS NULL) "
            "ORDER BY p.display_name, p.canonical_player_id",
            (season_id,),
        ).fetchall()
        return [_player(row) for row in rows]

    def search_available(self, season_id, query=None, limit=50):
        """`list_available` narrowed by a case-insensitive substring of
        `display_name`. Filtered in Python rather than SQL `LIKE`/`ILIKE` so
        matching behaves identically on SQLite and PostgreSQL (see
        app.db._translate/`_for_update_suffix` for the same cross-dialect
        philosophy elsewhere in this codebase)."""
        available = self.list_available(season_id)
        if query:
            needle = query.strip().lower()
            available = [player for player in available if needle in player.display_name.lower()]
        return available[: max(limit, 0)]

    def browse(self, season_id, query=None, availability=None, limit=200):
        """Return the season pool plus current authoritative ownership.

        This is intentionally a joined, request-time read model.  It does not
        cache availability and cannot be used to acquire a player; draft
        writes still go through :class:`DraftRepository` and its locked
        ownership transaction.
        """
        rows = self.database.execute(
            "SELECT p.*, o.season_entry_id AS owner_season_entry_id, "
            "n.team_name AS owner_team_name FROM season_player_pool p "
            "LEFT JOIN player_ownership_period o ON o.season_player_id=p.season_player_id "
            "AND o.released_at IS NULL "
            "LEFT JOIN season_entry_team_name_history n ON n.season_entry_id=o.season_entry_id "
            "AND n.ended_at IS NULL "
            "WHERE p.season_id=? ORDER BY p.display_name, p.canonical_player_id",
            (season_id,),
        ).fetchall()
        needles = [part.casefold() for part in (query or "").split() if part]
        items = []
        for row in rows:
            state = "owned" if row["owner_season_entry_id"] else ("available" if row["eligible"] else "unresolved")
            if availability and state != availability:
                continue
            searchable = " ".join(
                str(value or "") for value in (row["display_name"], row["afl_team_name"], row["canonical_player_id"])
            ).casefold()
            if needles and not all(needle in searchable for needle in needles):
                continue
            diagnostic = None
            if not row["eligible"]:
                diagnostic = "Not selectable: season player identity or eligibility requires investigation"
            elif not row["display_name"].strip():
                diagnostic = "Missing AFL player display name"
            elif row["afl_team_id"] is None or not row["afl_team_name"]:
                diagnostic = "AFL club data unavailable"
            items.append(
                SeasonPlayerPoolItem(
                    season_player_id=row["season_player_id"],
                    season_id=row["season_id"],
                    canonical_player_id=row["canonical_player_id"],
                    display_name=row["display_name"],
                    afl_team_id=row["afl_team_id"],
                    afl_team_name=row["afl_team_name"],
                    eligible=bool(row["eligible"]),
                    availability=state,
                    owner_season_entry_id=row["owner_season_entry_id"],
                    owner_team_name=row["owner_team_name"],
                    diagnostic=diagnostic,
                )
            )
        return items[: max(limit, 0)]


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
            accepted_draft = conn.execute(
                "SELECT target_squad_size FROM season_draft WHERE season_id=?" + _for_update_suffix(self.database),
                (season_id,),
            ).fetchone()
            if accepted_draft and accepted_draft["target_squad_size"] != squad_limit:
                raise SquadConfigurationFrozenError("squad limit cannot change after the season draft is accepted")
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
                conn,
                season_player_id,
                season_entry_id,
                effective_at=effective_at,
                actor=actor,
                reason=reason,
                correlation_id=correlation_id,
            )
        return item

    def acquire_in_transaction(
        self,
        conn,
        season_player_id,
        season_entry_id,
        *,
        effective_at=None,
        actor=ActorContext.anonymous_operator("admin"),
        reason=None,
        correlation_id=None,
        allow_closed_window=False,
    ):
        """Acquire using an existing transaction (for compound domain commands).
        `allow_closed_window` -- see `_assert_ownership_mutation_allowed` --
        must only be set by an explicitly authorised correction call site."""
        at = effective_at or _now()
        correlation_id = correlation_id or new_correlation_id()
        if self.database.engine.dialect.name == "sqlite":
            conn.execute(
                "UPDATE season_player_pool SET updated_at=updated_at WHERE season_player_id=?", (season_player_id,)
            )
            conn.execute("UPDATE season_entry SET created_at=created_at WHERE season_entry_id=?", (season_entry_id,))
        player = conn.execute(
            "SELECT season_id, eligible FROM season_player_pool WHERE season_player_id=?"
            + _for_update_suffix(self.database),
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
        _assert_ownership_mutation_allowed(
            conn, self.database, player["season_id"], allow_closed_window=allow_closed_window
        )
        if not player["eligible"]:
            raise PlayerUnavailableError("player is not selectable")
        overlap = conn.execute(
            "SELECT 1 FROM player_ownership_period WHERE season_player_id=? AND (released_at IS NULL OR released_at>?)",
            (season_player_id, at),
        ).fetchone()
        if overlap:
            raise PlayerUnavailableError("player ownership would overlap an existing period")
        self.validate_squad_capacity(season_entry_id, effective_at=at, connection=conn)
        item = OwnershipPeriod(_id(), season_player_id, player["season_id"], season_entry_id, at, None, reason, _now())
        try:
            conn.execute(
                "INSERT INTO player_ownership_period VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(item.__dict__.values()),
            )
        except IntegrityError as error:
            message = str(error.orig).lower()
            if "overlapping player ownership period" in message or (
                "unique" in message and "player_ownership_period.season_player_id" in message
            ):
                raise PlayerUnavailableError("player ownership changed concurrently") from error
            raise
        append_event(
            conn,
            actor=actor,
            action=PLAYER_ACQUIRED,
            entity_type=ENTITY_TYPE_OWNERSHIP_PERIOD,
            entity_id=item.ownership_period_id,
            correlation_id=correlation_id,
            reason=reason,
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
        with transaction(self.database) as conn:
            item = self.release_in_transaction(
                conn,
                season_player_id,
                effective_at=effective_at,
                actor=actor,
                reason=reason,
                correlation_id=correlation_id,
            )
        return item

    def release_in_transaction(
        self,
        conn,
        season_player_id,
        *,
        effective_at=None,
        actor=ActorContext.anonymous_operator("admin"),
        reason=None,
        correlation_id=None,
        allow_closed_window=False,
    ):
        """Release using an existing transaction (for compound domain commands,
        e.g. app.draft's correction workflow releasing an erroneous pick's
        acquisition in the same transaction as reopening the pick).
        `allow_closed_window` -- see `_assert_ownership_mutation_allowed` --
        must only be set by an explicitly authorised correction call site."""
        at = effective_at or _now()
        correlation_id = correlation_id or new_correlation_id()
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
        _assert_ownership_mutation_allowed(
            conn, self.database, current["season_id"], allow_closed_window=allow_closed_window
        )
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
            _assert_ownership_mutation_allowed(conn, self.database, player["season_id"])
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

    def current_squad(self, season_entry_id):
        """Return the entry's authoritative current ownership periods.

        Unlike ``squad_at``, this is intentionally not a historical view. It
        shares submission validation's authority: an ownership period is
        current exactly while it has no release timestamp.
        """
        rows = self.database.execute(
            "SELECT * FROM player_ownership_period "
            "WHERE season_entry_id=? AND released_at IS NULL ORDER BY acquired_at",
            (season_entry_id,),
        ).fetchall()
        return [OwnershipPeriod(**dict(row)) for row in rows]
