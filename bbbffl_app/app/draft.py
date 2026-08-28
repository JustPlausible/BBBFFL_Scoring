"""Authoritative preseason draft ledger integrated with player ownership."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from app.audit import ActorContext, append_event, new_correlation_id
from app.db import _for_update_suffix, transaction
from app.player_pool import OwnershipRepository


def _id():
    return str(uuid4())


def _now():
    return datetime.now(timezone.utc).isoformat()


class DraftStateError(ValueError):
    pass


class DraftOrderError(DraftStateError):
    pass


class DraftTurnError(DraftStateError):
    pass


class DraftPickCompletedError(DraftStateError):
    pass


@dataclass(frozen=True)
class DraftPick:
    draft_pick_id: str
    draft_id: str
    season_id: str
    overall_number: int
    draft_round: int
    round_position: int
    original_season_entry_id: str
    current_season_entry_id: str
    selected_season_player_id: str | None
    completed_at: str | None


@dataclass(frozen=True)
class PickTransfer:
    transfer_id: str
    draft_pick_id: str
    from_season_entry_id: str
    to_season_entry_id: str
    transferred_at: str
    reason: str | None
    audit_event_id: str


def snake_allocations(entry_ids, target_squad_size):
    """Yield (overall, round, position, entry) for a deterministic snake."""
    if not entry_ids or target_squad_size <= 0 or len(set(entry_ids)) != len(entry_ids):
        raise DraftOrderError("draft order must contain unique entries and a positive squad target")
    overall = 0
    for round_number in range(1, target_squad_size + 1):
        allocation = entry_ids if round_number % 2 else list(reversed(entry_ids))
        for round_position, entry_id in enumerate(allocation, 1):
            overall += 1
            yield overall, round_number, round_position, entry_id


class DraftRepository:
    def __init__(self, database):
        self.database = database
        self.ownership = OwnershipRepository(database)

    def accept_order(self, season_id, ordered_entry_ids, *, actor=ActorContext.anonymous_operator("admin"), reason=None):
        """Freeze a complete order and materialise every stable pick atomically."""
        ordered_entry_ids = list(ordered_entry_ids)
        with transaction(self.database) as conn:
            if self.database.engine.dialect.name == "sqlite":
                conn.execute("UPDATE bbbffl_season SET updated_at=updated_at WHERE season_id=?", (season_id,))
            if conn.execute("SELECT 1 FROM season_draft WHERE season_id=?", (season_id,)).fetchone():
                raise DraftOrderError("the accepted draft order is frozen")
            expected = {row["season_entry_id"] for row in conn.execute(
                "SELECT season_entry_id FROM season_entry WHERE season_id=?", (season_id,)
            ).fetchall()}
            if not expected or len(ordered_entry_ids) != len(expected) or set(ordered_entry_ids) != expected:
                raise DraftOrderError("draft order must contain every participating season entry exactly once")
            config = conn.execute(
                "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?", (season_id,)
            ).fetchone()
            if not config:
                raise DraftOrderError("season squad limit must be configured before accepting the draft order")
            draft_id, accepted_at = _id(), _now()
            conn.execute("INSERT INTO season_draft VALUES (?, ?, ?, ?)", (draft_id, season_id, config["squad_limit"], accepted_at))
            for position, entry_id in enumerate(ordered_entry_ids, 1):
                conn.execute("INSERT INTO draft_order_position VALUES (?, ?, ?, ?)", (draft_id, season_id, position, entry_id))
            for overall, round_number, round_position, entry_id in snake_allocations(ordered_entry_ids, config["squad_limit"]):
                conn.execute(
                    "INSERT INTO draft_pick VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                    (_id(), draft_id, season_id, overall, round_number, round_position, entry_id, entry_id),
                )
            append_event(conn, actor=actor, action="draft.order.accepted", entity_type="draft", entity_id=draft_id,
                         reason=reason, after_state={"season_id": season_id, "entry_ids": ordered_entry_ids,
                                                    "target_squad_size": config["squad_limit"]})
        return draft_id

    def picks(self, season_id):
        rows = self.database.execute(
            "SELECT p.* FROM draft_pick p JOIN season_draft d ON d.draft_id=p.draft_id WHERE d.season_id=? ORDER BY overall_number",
            (season_id,),
        ).fetchall()
        return [DraftPick(**dict(row)) for row in rows]

    def next_pick(self, season_id):
        row = self.database.execute(
            "SELECT p.* FROM draft_pick p JOIN season_draft d ON d.draft_id=p.draft_id "
            "WHERE d.season_id=? AND p.completed_at IS NULL ORDER BY p.overall_number LIMIT 1", (season_id,),
        ).fetchone()
        return DraftPick(**dict(row)) if row else None

    def transfer_pick(self, draft_pick_id, to_entry_id, *, actor=ActorContext.anonymous_operator("admin"), reason=None, transferred_at=None):
        at = transferred_at or _now()
        with transaction(self.database) as conn:
            if self.database.engine.dialect.name == "sqlite":
                conn.execute("UPDATE draft_pick SET current_season_entry_id=current_season_entry_id WHERE draft_pick_id=?", (draft_pick_id,))
            pick = conn.execute("SELECT * FROM draft_pick WHERE draft_pick_id=?" + _for_update_suffix(self.database), (draft_pick_id,)).fetchone()
            entry = conn.execute("SELECT season_id FROM season_entry WHERE season_entry_id=?", (to_entry_id,)).fetchone()
            if not pick or not entry or entry["season_id"] != pick["season_id"]:
                raise DraftStateError("pick and destination must exist in the same season")
            if pick["completed_at"] is not None:
                raise DraftPickCompletedError("completed picks cannot be transferred")
            if pick["current_season_entry_id"] == to_entry_id:
                raise DraftStateError("destination already owns the pick")
            correlation = new_correlation_id()
            event = append_event(
                conn, actor=actor, action="draft.pick.transferred", entity_type="draft.pick",
                entity_id=draft_pick_id, correlation_id=correlation, reason=reason,
                before_state={"owner": pick["current_season_entry_id"]}, after_state={"owner": to_entry_id},
            )
            transfer = PickTransfer(_id(), draft_pick_id, pick["current_season_entry_id"], to_entry_id, at, reason, event.event_id)
            conn.execute("INSERT INTO draft_pick_transfer VALUES (?, ?, ?, ?, ?, ?, ?)", tuple(transfer.__dict__.values()))
            conn.execute("UPDATE draft_pick SET current_season_entry_id=? WHERE draft_pick_id=?", (to_entry_id, draft_pick_id))
        return transfer

    def transfer_history(self, draft_pick_id):
        rows = self.database.execute(
            "SELECT * FROM draft_pick_transfer WHERE draft_pick_id=? ORDER BY transferred_at, transfer_id", (draft_pick_id,)
        ).fetchall()
        return [PickTransfer(**dict(row)) for row in rows]

    def execute_pick(self, season_id, selecting_entry_id, season_player_id, *, pick_id=None,
                     actor=ActorContext.anonymous_operator("admin"), reason="draft selection", completed_at=None):
        at = completed_at or _now()
        correlation = new_correlation_id()
        with transaction(self.database) as conn:
            if self.database.engine.dialect.name == "sqlite":
                conn.execute("UPDATE bbbffl_season SET updated_at=updated_at WHERE season_id=?", (season_id,))
            next_row = conn.execute(
                "SELECT p.* FROM draft_pick p JOIN season_draft d ON d.draft_id=p.draft_id "
                "WHERE d.season_id=? AND p.completed_at IS NULL ORDER BY p.overall_number LIMIT 1"
                + _for_update_suffix(self.database), (season_id,),
            ).fetchone()
            if not next_row:
                raise DraftStateError("draft has no executable pick")
            if pick_id is not None and pick_id != next_row["draft_pick_id"]:
                prior = conn.execute("SELECT completed_at FROM draft_pick WHERE draft_pick_id=?", (pick_id,)).fetchone()
                if prior and prior["completed_at"] is not None:
                    raise DraftPickCompletedError("pick is already completed")
                raise DraftTurnError("requested pick is not the next executable pick")
            if next_row["current_season_entry_id"] != selecting_entry_id:
                raise DraftTurnError("selecting entry does not own the current pick")
            self.ownership.acquire_in_transaction(
                conn, season_player_id, selecting_entry_id, effective_at=at, actor=actor,
                reason=reason, correlation_id=correlation,
            )
            result = conn.execute(
                "UPDATE draft_pick SET selected_season_player_id=?, completed_at=? "
                "WHERE draft_pick_id=? AND completed_at IS NULL",
                (season_player_id, at, next_row["draft_pick_id"]),
            )
            if result.rowcount != 1:
                raise DraftPickCompletedError("pick was completed concurrently")
            append_event(
                conn, actor=actor, action="draft.pick.completed", entity_type="draft.pick",
                entity_id=next_row["draft_pick_id"], correlation_id=correlation, reason=reason,
                after_state={"season_player_id": season_player_id, "season_entry_id": selecting_entry_id,
                             "overall_number": next_row["overall_number"]},
            )
        return self.picks(season_id)[next_row["overall_number"] - 1]
