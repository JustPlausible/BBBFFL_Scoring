"""Preseason transaction/finalisation window: audited trades and the frozen
opening-ownership boundary Round 1 relies on (roadmap package 15, issue #54).

Builds on, rather than duplicating:

- the authoritative ownership ledger (`app.player_pool.OwnershipRepository`)
  -- every trade leg is applied through `acquire_in_transaction`/
  `release_in_transaction`, so every existing invariant (no overlapping
  ownership, season match, eligibility, squad capacity) is enforced exactly
  once, in one place, for drafts, trades and any future caller alike;
- the finalised preseason draft (`app.draft.DraftRepository`) -- a window
  can only open once its season's draft is finalized;
- the append-only audit boundary (`app.audit`) for actor/reason/before-after
  history on every trade and lifecycle transition.

## Lifecycle

Persisted, season-scoped, never inferred from dates or UI state::

    draft finalized -> window OPEN -> [preseason trades] -> window CLOSED
        (+ opening snapshot frozen) -> Round 1

`season_preseason_window` holds exactly one row per season (mirroring
`season_draft`'s one-row-per-season shape); `closed_at IS NULL` means open.
Both `open_window` and `close_window` run inside one transaction with a row
lock on that row (see `_locked_window`), so two concurrent attempts to open
or close the same season's window can never both succeed, and a closing
transaction that fails validation changes nothing.

## Trade atomicity

`submit_trade` validates every leg of a proposed trade -- two-club,
multi-club, or multiple players within one trade -- against the
authoritative ownership ledger *before* writing anything. Only once every
leg passes does it apply every release/acquisition, in one database
transaction: if any leg is invalid, or any later ownership-ledger invariant
(squad capacity, availability) is violated while applying it, the entire
transaction rolls back and no partial trade is ever visible.

## Opening snapshot

`close_window` freezes a versioned `preseason_opening_snapshot`: one row per
player recording which entry owned it and a reference to the exact
`player_ownership_period` row, not a copy of player/ownership facts. This is
the stable boundary Round 1 selection validation must use -- never a live
re-query of `player_ownership_period`, which keeps changing as later
history (mid-season roadmap 30/31 work, not this issue) is added. A
correction after closure (`correct_opening_snapshot`) appends a new
snapshot version rather than rewriting the frozen one; draft/trade
provenance remains fully queryable through `app.draft`/this module
regardless of which snapshot version is "current".
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from app.audit import ActorContext, append_event, new_correlation_id
from app.db import _for_update_suffix, transaction
from app.player_pool import OwnershipRepository, PreseasonWindowClosedError

__all__ = [
    "PreseasonStateError",
    "PreseasonDraftNotFinalizedError",
    "PreseasonWindowExistsError",
    "PreseasonWindowClosedError",
    "PreseasonSquadValidationError",
    "PreseasonTradeValidationError",
    "PreseasonSnapshotError",
    "PreseasonWindow",
    "PreseasonTrade",
    "TradeLeg",
    "OpeningSquadSnapshot",
    "OpeningSquadEntry",
    "PreseasonRepository",
]


def _id():
    return str(uuid4())


def _now():
    return datetime.now(timezone.utc).isoformat()


class PreseasonStateError(ValueError):
    pass


class PreseasonDraftNotFinalizedError(PreseasonStateError):
    pass


class PreseasonWindowExistsError(PreseasonStateError):
    pass


class PreseasonSquadValidationError(PreseasonStateError):
    """Raised by `close_window` when one or more opening squads are invalid.
    `.issues` is a list of `{"season_entry_id", "expected_squad_size",
    "actual_squad_size", "problem"}` dicts identifying exactly which
    entry/squad and which rule blocked closure."""

    def __init__(self, message, issues):
        super().__init__(message)
        self.issues = issues


class PreseasonTradeValidationError(PreseasonStateError):
    """Raised by `submit_trade` when one or more legs of a proposed trade
    are invalid. `.issues` is a list of small dicts identifying the
    offending leg and why; nothing is written when this is raised."""

    def __init__(self, message, issues):
        super().__init__(message)
        self.issues = issues


class PreseasonSnapshotError(PreseasonStateError):
    pass


@dataclass(frozen=True)
class PreseasonWindow:
    window_id: str
    season_id: str
    draft_id: str
    opened_at: str
    closed_at: str | None
    closed_note: str | None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


@dataclass(frozen=True)
class PreseasonTrade:
    trade_id: str
    season_id: str
    window_id: str
    applied_at: str
    reason: str | None
    correlation_id: str
    audit_event_id: str


@dataclass(frozen=True)
class TradeLeg:
    leg_id: str
    trade_id: str
    season_player_id: str
    season_id: str
    from_season_entry_id: str
    to_season_entry_id: str
    released_ownership_period_id: str
    acquired_ownership_period_id: str


@dataclass(frozen=True)
class OpeningSquadSnapshot:
    snapshot_id: str
    season_id: str
    window_id: str
    version: int
    created_at: str
    created_by: str | None
    reason: str | None
    supersedes_snapshot_id: str | None


@dataclass(frozen=True)
class OpeningSquadEntry:
    entry_row_id: str
    snapshot_id: str
    season_id: str
    season_entry_id: str
    season_player_id: str
    ownership_period_id: str


class PreseasonRepository:
    def __init__(self, database):
        self.database = database
        self.ownership = OwnershipRepository(database)

    # -- Window lifecycle --------------------------------------------------

    def open_window(self, season_id, *, actor=ActorContext.anonymous_operator("admin"), reason=None):
        """Open the preseason trade/finalisation window for a season. Only
        legal once, and only once that season's draft has been finalized
        (`app.draft.DraftRepository.finalize`) -- see the module docstring's
        lifecycle diagram."""
        with transaction(self.database) as conn:
            if self.database.engine.dialect.name == "sqlite":
                conn.execute("UPDATE bbbffl_season SET updated_at=updated_at WHERE season_id=?", (season_id,))
            if conn.execute(
                "SELECT 1 FROM season_preseason_window WHERE season_id=?" + _for_update_suffix(self.database),
                (season_id,),
            ).fetchone():
                raise PreseasonWindowExistsError("a preseason window already exists for this season")
            draft = conn.execute(
                "SELECT draft_id, finalized_at FROM season_draft WHERE season_id=?" + _for_update_suffix(self.database),
                (season_id,),
            ).fetchone()
            if not draft or draft["finalized_at"] is None:
                raise PreseasonDraftNotFinalizedError(
                    "the season draft must be finalized before the preseason window can open"
                )
            window_id, opened_at = _id(), _now()
            conn.execute(
                "INSERT INTO season_preseason_window VALUES (?, ?, ?, ?, NULL, NULL)",
                (window_id, season_id, draft["draft_id"], opened_at),
            )
            append_event(
                conn,
                actor=actor,
                action="preseason.window.opened",
                entity_type="preseason.window",
                entity_id=window_id,
                reason=reason,
                after_state={"season_id": season_id, "draft_id": draft["draft_id"]},
            )
        return self.get_window(season_id)

    def get_window(self, season_id):
        row = self.database.execute("SELECT * FROM season_preseason_window WHERE season_id=?", (season_id,)).fetchone()
        return PreseasonWindow(**dict(row)) if row else None

    def _locked_window(self, conn, season_id):
        row = conn.execute(
            "SELECT * FROM season_preseason_window WHERE season_id=?" + _for_update_suffix(self.database),
            (season_id,),
        ).fetchone()
        if not row:
            raise KeyError(season_id)
        return row

    def validate_squads(self, season_id, *, connection=None):
        """Every season entry's active ownership count against the season's
        configured squad limit (`season_squad_configuration` -- the one
        configured season rule this repository knows about; a future
        rules-engine package would extend this, not this call site). Returns
        a list of issues (empty when every squad is valid); never raises --
        callers (`close_window`, an admin read endpoint) decide what to do
        with the result."""
        conn = connection or self.database
        config = conn.execute(
            "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?", (season_id,)
        ).fetchone()
        if not config:
            return [{"season_entry_id": None, "problem": "season squad limit is not configured"}]
        rows = conn.execute(
            "SELECT e.season_entry_id, COUNT(p.ownership_period_id) AS n FROM season_entry e "
            "LEFT JOIN player_ownership_period p "
            "  ON p.season_entry_id=e.season_entry_id AND p.released_at IS NULL "
            "WHERE e.season_id=? GROUP BY e.season_entry_id",
            (season_id,),
        ).fetchall()
        issues = []
        for row in rows:
            if row["n"] != config["squad_limit"]:
                issues.append(
                    {
                        "season_entry_id": row["season_entry_id"],
                        "expected_squad_size": config["squad_limit"],
                        "actual_squad_size": row["n"],
                        "problem": "squad size does not match the configured squad limit",
                    }
                )
        return issues

    def close_window(self, season_id, *, actor=ActorContext.anonymous_operator("admin"), reason=None):
        """Validate every opening squad and, only if every one is valid,
        atomically freeze the opening-ownership snapshot and close the
        window. Raises `PreseasonSquadValidationError` (with `.issues`)
        without changing any state if even one squad is invalid. Calling
        this again once closed raises `PreseasonWindowClosedError` rather
        than silently doing nothing -- repeated calls are safe (nothing is
        double-applied), just not silently idempotent, matching this
        repository's `DraftFinalizedError` convention."""
        with transaction(self.database) as conn:
            if self.database.engine.dialect.name == "sqlite":
                conn.execute("UPDATE bbbffl_season SET updated_at=updated_at WHERE season_id=?", (season_id,))
            window = self._locked_window(conn, season_id)
            if window["closed_at"] is not None:
                raise PreseasonWindowClosedError("preseason window is already closed")
            issues = self.validate_squads(season_id, connection=conn)
            if issues:
                raise PreseasonSquadValidationError(
                    "preseason window cannot be closed: one or more opening squads are invalid",
                    issues=issues,
                )
            entries = conn.execute(
                "SELECT e.season_entry_id, p.ownership_period_id, p.season_player_id FROM season_entry e "
                "JOIN player_ownership_period p "
                "  ON p.season_entry_id=e.season_entry_id AND p.released_at IS NULL "
                "WHERE e.season_id=? ORDER BY e.season_entry_id, p.season_player_id",
                (season_id,),
            ).fetchall()
            snapshot_id, now = _id(), _now()
            conn.execute(
                "INSERT INTO preseason_opening_snapshot VALUES (?, ?, ?, 1, ?, ?, ?, NULL)",
                (snapshot_id, season_id, window["window_id"], now, actor.actor_id, reason),
            )
            for row in entries:
                conn.execute(
                    "INSERT INTO preseason_opening_snapshot_entry VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _id(),
                        snapshot_id,
                        season_id,
                        row["season_entry_id"],
                        row["season_player_id"],
                        row["ownership_period_id"],
                    ),
                )
            correlation = new_correlation_id()
            append_event(
                conn,
                actor=actor,
                action="preseason.squad.frozen",
                entity_type="preseason.opening_snapshot",
                entity_id=snapshot_id,
                correlation_id=correlation,
                reason=reason,
                after_state={"season_id": season_id, "version": 1, "entry_count": len(entries)},
            )
            conn.execute(
                "UPDATE season_preseason_window SET closed_at=?, closed_note=? WHERE window_id=?",
                (now, reason, window["window_id"]),
            )
            append_event(
                conn,
                actor=actor,
                action="preseason.window.closed",
                entity_type="preseason.window",
                entity_id=window["window_id"],
                correlation_id=correlation,
                reason=reason,
                before_state={"closed_at": None},
                after_state={"closed_at": now, "opening_snapshot_id": snapshot_id},
            )
        return self.get_window(season_id)

    # -- Trades --------------------------------------------------------------

    def submit_trade(
        self,
        season_id,
        legs,
        *,
        actor=ActorContext.anonymous_operator("admin"),
        reason=None,
        effective_at=None,
    ):
        """Atomically apply a preseason trade of one or more players moving
        between two or more entries. `legs` is a sequence of mappings with
        `season_player_id`, `from_season_entry_id` and `to_season_entry_id`
        -- the caller's claimed current owner is validated against the
        authoritative ownership ledger, never trusted. Every leg is
        validated (season/eligibility/current-owner/destination) before any
        ownership write happens; if any leg is invalid, nothing is written
        (`PreseasonTradeValidationError`, `.issues` lists every problem
        found). Once every leg passes, every release is applied before any
        acquisition, so a multi-club or multi-player trade's squad-capacity
        checks see each entry's net position for the whole trade rather than
        a stale mid-trade count; a squad-capacity or availability failure
        discovered only at that point still rolls back the entire
        transaction, so no partial trade is ever visible."""
        legs = [dict(leg) for leg in legs]
        if not legs:
            raise PreseasonTradeValidationError("a trade requires at least one player leg", issues=[])
        at = effective_at or _now()
        correlation = new_correlation_id()
        with transaction(self.database) as conn:
            window = self._locked_window(conn, season_id)
            if window["closed_at"] is not None:
                raise PreseasonWindowClosedError("preseason window is closed; trades can no longer be submitted")

            issues = []
            seen_players = set()
            resolved = []
            for index, leg in enumerate(legs):
                player_id = leg.get("season_player_id")
                from_entry = leg.get("from_season_entry_id")
                to_entry = leg.get("to_season_entry_id")
                if not player_id or not from_entry or not to_entry:
                    issues.append(
                        {
                            "leg": index,
                            "problem": "season_player_id, from_season_entry_id and to_season_entry_id are all required",
                        }
                    )
                    continue
                if from_entry == to_entry:
                    issues.append(
                        {"leg": index, "season_player_id": player_id, "problem": "from and to entry must differ"}
                    )
                    continue
                if player_id in seen_players:
                    issues.append(
                        {
                            "leg": index,
                            "season_player_id": player_id,
                            "problem": "player appears in more than one leg of this trade",
                        }
                    )
                    continue
                seen_players.add(player_id)
                player = conn.execute(
                    "SELECT * FROM season_player_pool WHERE season_player_id=?" + _for_update_suffix(self.database),
                    (player_id,),
                ).fetchone()
                if not player or player["season_id"] != season_id:
                    issues.append(
                        {
                            "leg": index,
                            "season_player_id": player_id,
                            "problem": "player is not in this season's player pool",
                        }
                    )
                    continue
                if not player["eligible"]:
                    issues.append(
                        {"leg": index, "season_player_id": player_id, "problem": "player is not eligible/selectable"}
                    )
                    continue
                from_row = conn.execute(
                    "SELECT season_id FROM season_entry WHERE season_entry_id=?" + _for_update_suffix(self.database),
                    (from_entry,),
                ).fetchone()
                to_row = conn.execute(
                    "SELECT season_id FROM season_entry WHERE season_entry_id=?" + _for_update_suffix(self.database),
                    (to_entry,),
                ).fetchone()
                if not from_row or from_row["season_id"] != season_id or not to_row or to_row["season_id"] != season_id:
                    issues.append(
                        {"leg": index, "season_player_id": player_id, "problem": "entries must belong to this season"}
                    )
                    continue
                current = conn.execute(
                    "SELECT * FROM player_ownership_period WHERE season_player_id=? AND released_at IS NULL"
                    + _for_update_suffix(self.database),
                    (player_id,),
                ).fetchone()
                if not current or current["season_entry_id"] != from_entry:
                    issues.append(
                        {
                            "leg": index,
                            "season_player_id": player_id,
                            "problem": f"player is not currently owned by entry {from_entry}",
                        }
                    )
                    continue
                resolved.append({"season_player_id": player_id, "from_entry": from_entry, "to_entry": to_entry})
            if issues:
                raise PreseasonTradeValidationError(
                    "preseason trade rejected: every leg of a trade must be valid", issues=issues
                )

            trade_id = _id()
            trade_event = append_event(
                conn,
                actor=actor,
                action="preseason.trade.applied",
                entity_type="preseason.trade",
                entity_id=trade_id,
                correlation_id=correlation,
                reason=reason,
                after_state={"season_id": season_id, "legs": resolved},
            )
            conn.execute(
                "INSERT INTO preseason_trade VALUES (?, ?, ?, ?, ?, ?, ?)",
                (trade_id, season_id, window["window_id"], at, reason, correlation, trade_event.event_id),
            )
            released = {}
            for leg in resolved:
                released[leg["season_player_id"]] = self.ownership.release_in_transaction(
                    conn,
                    leg["season_player_id"],
                    effective_at=at,
                    actor=actor,
                    reason=reason or "preseason trade",
                    correlation_id=correlation,
                )
            for leg in resolved:
                acquired = self.ownership.acquire_in_transaction(
                    conn,
                    leg["season_player_id"],
                    leg["to_entry"],
                    effective_at=at,
                    actor=actor,
                    reason=reason or "preseason trade",
                    correlation_id=correlation,
                )
                released_period = released[leg["season_player_id"]]
                conn.execute(
                    "INSERT INTO preseason_trade_leg VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _id(),
                        trade_id,
                        leg["season_player_id"],
                        season_id,
                        leg["from_entry"],
                        leg["to_entry"],
                        released_period.ownership_period_id,
                        acquired.ownership_period_id,
                    ),
                )
        return self.get_trade(trade_id)

    def get_trade(self, trade_id):
        row = self.database.execute("SELECT * FROM preseason_trade WHERE trade_id=?", (trade_id,)).fetchone()
        return PreseasonTrade(**dict(row)) if row else None

    def list_trades(self, season_id):
        rows = self.database.execute(
            "SELECT * FROM preseason_trade WHERE season_id=? ORDER BY applied_at, trade_id", (season_id,)
        ).fetchall()
        return [PreseasonTrade(**dict(row)) for row in rows]

    def trade_legs(self, trade_id):
        rows = self.database.execute(
            "SELECT * FROM preseason_trade_leg WHERE trade_id=? ORDER BY leg_id", (trade_id,)
        ).fetchall()
        return [TradeLeg(**dict(row)) for row in rows]

    # -- Opening snapshot ------------------------------------------------

    def current_snapshot(self, season_id):
        row = self.database.execute(
            "SELECT * FROM preseason_opening_snapshot WHERE season_id=? ORDER BY version DESC LIMIT 1",
            (season_id,),
        ).fetchone()
        return OpeningSquadSnapshot(**dict(row)) if row else None

    def snapshot_versions(self, season_id):
        rows = self.database.execute(
            "SELECT * FROM preseason_opening_snapshot WHERE season_id=? ORDER BY version", (season_id,)
        ).fetchall()
        return [OpeningSquadSnapshot(**dict(row)) for row in rows]

    def opening_squad(self, season_id, season_entry_id=None):
        """The frozen opening-ownership boundary Round 1 selection
        validation relies on -- the *current* snapshot version's entries,
        never a live re-query of `player_ownership_period` (see the module
        docstring). Empty before the window has been closed."""
        snapshot = self.current_snapshot(season_id)
        if not snapshot:
            return []
        query = "SELECT * FROM preseason_opening_snapshot_entry WHERE snapshot_id=?"
        params = [snapshot.snapshot_id]
        if season_entry_id is not None:
            query += " AND season_entry_id=?"
            params.append(season_entry_id)
        rows = self.database.execute(query, tuple(params)).fetchall()
        return [OpeningSquadEntry(**dict(row)) for row in rows]

    def correct_opening_snapshot(
        self,
        season_id,
        season_entry_id,
        *,
        remove_season_player_id,
        add_season_player_id,
        actor=ActorContext.anonymous_operator("admin"),
        reason,
        effective_at=None,
    ):
        """An explicitly authorised, attributable correction to one entry's
        already-frozen opening squad -- e.g. a data-entry error discovered
        after closure. Deliberately narrow and distinguishable from an
        ordinary preseason trade:

        - only permitted once the window is *closed* (a trade only runs
          while it is open);
        - a single entry's single-player swap, never a general multi-party
          trade;
        - recorded under its own audit action (`preseason.correction.applied`),
          never `preseason.trade.applied`;
        - appends a brand-new opening-snapshot version; the prior version's
          rows are never updated or deleted (0017's triggers enforce this at
          the database level too).

        The underlying swap still goes through
        `OwnershipRepository.release_in_transaction`/`acquire_in_transaction`
        -- the same authoritative ledger a trade uses, so eligibility,
        season match, no-overlap and squad-capacity invariants all still
        apply; an administrator cannot use this to manufacture an otherwise
        invalid ownership state. It uses those methods' narrow
        `allow_closed_window` escape hatch, which is not reachable from the
        ordinary `acquire`/`release`/`transfer` entry points.
        """
        if not reason or not reason.strip():
            raise ValueError("an opening-squad correction requires an explicit reason")
        at = effective_at or _now()
        with transaction(self.database) as conn:
            window = self._locked_window(conn, season_id)
            if window["closed_at"] is None:
                raise PreseasonStateError("the opening squad can only be corrected after the window is closed")
            current = conn.execute(
                "SELECT * FROM preseason_opening_snapshot WHERE season_id=? ORDER BY version DESC LIMIT 1"
                + _for_update_suffix(self.database),
                (season_id,),
            ).fetchone()
            if not current:
                raise PreseasonSnapshotError("no opening snapshot exists to correct")
            rows = conn.execute(
                "SELECT * FROM preseason_opening_snapshot_entry WHERE snapshot_id=?", (current["snapshot_id"],)
            ).fetchall()
            removed = next(
                (
                    row
                    for row in rows
                    if row["season_entry_id"] == season_entry_id and row["season_player_id"] == remove_season_player_id
                ),
                None,
            )
            if not removed:
                raise PreseasonSnapshotError("player is not part of that entry's frozen opening squad")

            correlation = new_correlation_id()
            self.ownership.release_in_transaction(
                conn,
                remove_season_player_id,
                effective_at=at,
                actor=actor,
                reason=reason,
                correlation_id=correlation,
                allow_closed_window=True,
            )
            acquired = self.ownership.acquire_in_transaction(
                conn,
                add_season_player_id,
                season_entry_id,
                effective_at=at,
                actor=actor,
                reason=reason,
                correlation_id=correlation,
                allow_closed_window=True,
            )

            new_version = current["version"] + 1
            new_snapshot_id, now = _id(), _now()
            conn.execute(
                "INSERT INTO preseason_opening_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_snapshot_id,
                    season_id,
                    window["window_id"],
                    new_version,
                    now,
                    actor.actor_id,
                    reason,
                    current["snapshot_id"],
                ),
            )
            for row in rows:
                if row["entry_row_id"] == removed["entry_row_id"]:
                    continue
                conn.execute(
                    "INSERT INTO preseason_opening_snapshot_entry VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        _id(),
                        new_snapshot_id,
                        season_id,
                        row["season_entry_id"],
                        row["season_player_id"],
                        row["ownership_period_id"],
                    ),
                )
            conn.execute(
                "INSERT INTO preseason_opening_snapshot_entry VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _id(),
                    new_snapshot_id,
                    season_id,
                    season_entry_id,
                    add_season_player_id,
                    acquired.ownership_period_id,
                ),
            )
            append_event(
                conn,
                actor=actor,
                action="preseason.correction.applied",
                entity_type="preseason.opening_snapshot",
                entity_id=new_snapshot_id,
                correlation_id=correlation,
                reason=reason,
                before_state={
                    "snapshot_id": current["snapshot_id"],
                    "version": current["version"],
                    "season_entry_id": season_entry_id,
                    "season_player_id": remove_season_player_id,
                },
                after_state={
                    "snapshot_id": new_snapshot_id,
                    "version": new_version,
                    "season_entry_id": season_entry_id,
                    "season_player_id": add_season_player_id,
                },
            )
        return self.current_snapshot(season_id)
