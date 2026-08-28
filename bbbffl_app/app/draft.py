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


class DraftPausedError(DraftStateError):
    pass


class DraftFinalizedError(DraftStateError):
    pass


class DraftNotCompleteError(DraftStateError):
    pass


class DraftCorrectionError(DraftStateError):
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
    superseded_by_draft_pick_id: str | None = None


@dataclass(frozen=True)
class DraftStatus:
    draft_id: str
    season_id: str
    target_squad_size: int
    accepted_at: str
    paused_at: str | None
    paused_reason: str | None
    finalized_at: str | None
    finalized_note: str | None
    total_picks: int
    completed_picks: int

    @property
    def is_paused(self) -> bool:
        return self.paused_at is not None

    @property
    def is_finalized(self) -> bool:
        return self.finalized_at is not None

    @property
    def is_complete(self) -> bool:
        return self.total_picks > 0 and self.completed_picks == self.total_picks


@dataclass(frozen=True)
class DraftPickCorrection:
    correction_id: str
    original_draft_pick_id: str
    replacement_draft_pick_id: str
    corrected_at: str
    reason: str | None
    audit_event_id: str


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

    def accept_order(
        self, season_id, ordered_entry_ids, *, actor=ActorContext.anonymous_operator("admin"), reason=None
    ):
        """Freeze a complete order and materialise every stable pick atomically."""
        ordered_entry_ids = list(ordered_entry_ids)
        with transaction(self.database) as conn:
            if self.database.engine.dialect.name == "sqlite":
                conn.execute("UPDATE bbbffl_season SET updated_at=updated_at WHERE season_id=?", (season_id,))
            if conn.execute("SELECT 1 FROM season_draft WHERE season_id=?", (season_id,)).fetchone():
                raise DraftOrderError("the accepted draft order is frozen")
            expected = {
                row["season_entry_id"]
                for row in conn.execute(
                    "SELECT season_entry_id FROM season_entry WHERE season_id=?", (season_id,)
                ).fetchall()
            }
            if not expected or len(ordered_entry_ids) != len(expected) or set(ordered_entry_ids) != expected:
                raise DraftOrderError("draft order must contain every participating season entry exactly once")
            config = conn.execute(
                "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?"
                + _for_update_suffix(self.database),
                (season_id,),
            ).fetchone()
            if not config:
                raise DraftOrderError("season squad limit must be configured before accepting the draft order")
            draft_id, accepted_at = _id(), _now()
            conn.execute(
                "INSERT INTO season_draft VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL)",
                (draft_id, season_id, config["squad_limit"], accepted_at),
            )
            for position, entry_id in enumerate(ordered_entry_ids, 1):
                conn.execute(
                    "INSERT INTO draft_order_position VALUES (?, ?, ?, ?)", (draft_id, season_id, position, entry_id)
                )
            for overall, round_number, round_position, entry_id in snake_allocations(
                ordered_entry_ids, config["squad_limit"]
            ):
                conn.execute(
                    "INSERT INTO draft_pick VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                    (_id(), draft_id, season_id, overall, round_number, round_position, entry_id, entry_id),
                )
            append_event(
                conn,
                actor=actor,
                action="draft.order.accepted",
                entity_type="draft",
                entity_id=draft_id,
                reason=reason,
                after_state={
                    "season_id": season_id,
                    "entry_ids": ordered_entry_ids,
                    "target_squad_size": config["squad_limit"],
                },
            )
        return draft_id

    def picks(self, season_id, *, include_superseded=False):
        """The draft board's picks, one row per slot (overall_number) by
        default -- a corrected pick's original, superseded attempt is
        omitted here (see `corrections`/`pick_history` for that trail), so
        callers building a board never see two rows claiming the same slot."""
        clause = "" if include_superseded else " AND p.superseded_by_draft_pick_id IS NULL"
        rows = self.database.execute(
            "SELECT p.* FROM draft_pick p JOIN season_draft d ON d.draft_id=p.draft_id "
            f"WHERE d.season_id=?{clause} ORDER BY overall_number",
            (season_id,),
        ).fetchall()
        return [DraftPick(**dict(row)) for row in rows]

    def order(self, season_id):
        """The accepted, frozen draft order as `(position, season_entry_id)`
        pairs -- the configured order a board displays, independent of
        which entry currently owns any individual pick (see `picks` for
        that, once trades are applied)."""
        rows = self.database.execute(
            "SELECT o.position, o.season_entry_id FROM draft_order_position o "
            "JOIN season_draft d ON d.draft_id=o.draft_id WHERE d.season_id=? ORDER BY o.position",
            (season_id,),
        ).fetchall()
        return [(row["position"], row["season_entry_id"]) for row in rows]

    def corrections(self, season_id):
        rows = self.database.execute(
            "SELECT c.* FROM draft_pick_correction c JOIN draft_pick p ON p.draft_pick_id=c.original_draft_pick_id "
            "WHERE p.season_id=? ORDER BY c.corrected_at, c.correction_id",
            (season_id,),
        ).fetchall()
        return [DraftPickCorrection(**dict(row)) for row in rows]

    def status(self, season_id):
        row = self.database.execute("SELECT * FROM season_draft WHERE season_id=?", (season_id,)).fetchone()
        if not row:
            return None
        counts = self.database.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) AS completed "
            "FROM draft_pick WHERE draft_id=? AND superseded_by_draft_pick_id IS NULL",
            (row["draft_id"],),
        ).fetchone()
        return DraftStatus(
            draft_id=row["draft_id"],
            season_id=row["season_id"],
            target_squad_size=row["target_squad_size"],
            accepted_at=row["accepted_at"],
            paused_at=row["paused_at"],
            paused_reason=row["paused_reason"],
            finalized_at=row["finalized_at"],
            finalized_note=row["finalized_note"],
            total_picks=counts["total"],
            completed_picks=counts["completed"] or 0,
        )

    def _locked_draft(self, conn, season_id):
        draft = conn.execute(
            "SELECT * FROM season_draft WHERE season_id=?" + _for_update_suffix(self.database), (season_id,)
        ).fetchone()
        if not draft:
            raise DraftOrderError("season has no accepted draft order")
        return draft

    def pause(self, season_id, *, actor=ActorContext.anonymous_operator("admin"), reason=None):
        with transaction(self.database) as conn:
            if self.database.engine.dialect.name == "sqlite":
                conn.execute("UPDATE bbbffl_season SET updated_at=updated_at WHERE season_id=?", (season_id,))
            draft = self._locked_draft(conn, season_id)
            if draft["finalized_at"] is not None:
                raise DraftFinalizedError("draft is already finalized")
            if draft["paused_at"] is not None:
                raise DraftPausedError("draft is already paused")
            at = _now()
            conn.execute(
                "UPDATE season_draft SET paused_at=?, paused_reason=? WHERE draft_id=?",
                (at, reason, draft["draft_id"]),
            )
            append_event(
                conn,
                actor=actor,
                action="draft.paused",
                entity_type="draft",
                entity_id=draft["draft_id"],
                reason=reason,
                before_state={"paused_at": None},
                after_state={"paused_at": at},
            )
        return self.status(season_id)

    def resume(self, season_id, *, actor=ActorContext.anonymous_operator("admin"), reason=None):
        with transaction(self.database) as conn:
            if self.database.engine.dialect.name == "sqlite":
                conn.execute("UPDATE bbbffl_season SET updated_at=updated_at WHERE season_id=?", (season_id,))
            draft = self._locked_draft(conn, season_id)
            if draft["finalized_at"] is not None:
                raise DraftFinalizedError("draft is already finalized")
            if draft["paused_at"] is None:
                raise DraftStateError("draft is not paused")
            paused_at = draft["paused_at"]
            conn.execute(
                "UPDATE season_draft SET paused_at=NULL, paused_reason=NULL WHERE draft_id=?", (draft["draft_id"],)
            )
            append_event(
                conn,
                actor=actor,
                action="draft.resumed",
                entity_type="draft",
                entity_id=draft["draft_id"],
                reason=reason,
                before_state={"paused_at": paused_at},
                after_state={"paused_at": None},
            )
        return self.status(season_id)

    def correct_pick(
        self,
        season_id,
        draft_pick_id,
        *,
        actor=ActorContext.anonymous_operator("admin"),
        reason=None,
        corrected_at=None,
    ):
        """Undo the most recently completed pick: releases the erroneous
        acquisition (preserving its history via `OwnershipRepository.release`)
        and reopens the same draft slot as a fresh, uncompleted pick so the
        scorer re-selects through the ordinary `execute_pick` path -- no
        separate "corrected selection" code path exists. The original,
        erroneous `draft_pick` row is never updated or deleted: 0015's
        immutability triggers still protect it, and only its
        `superseded_by_draft_pick_id` pointer (added by 0016, deliberately
        outside those triggers' protected columns) changes.

        Deliberately narrow: only the single most-recently-completed active
        pick in the whole draft is correctable, so a correction never has to
        reconcile sequencing against picks completed after it.
        """
        at = corrected_at or _now()
        correlation = new_correlation_id()
        with transaction(self.database) as conn:
            if self.database.engine.dialect.name == "sqlite":
                conn.execute("UPDATE bbbffl_season SET updated_at=updated_at WHERE season_id=?", (season_id,))
                # The replacement row below is inserted only after the
                # original is marked superseded (see the FK's docstring in
                # migrations/versions/0016_draft_operations.py) -- deferred
                # for the whole transaction, reset automatically at commit.
                conn.execute("PRAGMA defer_foreign_keys = ON")
            draft = self._locked_draft(conn, season_id)
            if draft["finalized_at"] is not None:
                raise DraftFinalizedError("draft is already finalized")
            original = conn.execute(
                "SELECT * FROM draft_pick WHERE draft_pick_id=?" + _for_update_suffix(self.database),
                (draft_pick_id,),
            ).fetchone()
            if not original or original["draft_id"] != draft["draft_id"]:
                raise KeyError(draft_pick_id)
            if original["superseded_by_draft_pick_id"] is not None:
                raise DraftCorrectionError("pick has already been corrected")
            if original["completed_at"] is None:
                raise DraftCorrectionError("only a completed pick can be corrected")
            later = conn.execute(
                "SELECT 1 FROM draft_pick WHERE draft_id=? AND superseded_by_draft_pick_id IS NULL "
                "AND completed_at IS NOT NULL AND overall_number>?" + _for_update_suffix(self.database),
                (draft["draft_id"], original["overall_number"]),
            ).fetchone()
            if later:
                raise DraftCorrectionError(
                    "only the most recently completed pick can be corrected -- correct that one first"
                )
            self.ownership.release_in_transaction(
                conn,
                original["selected_season_player_id"],
                effective_at=at,
                actor=actor,
                reason=reason or "draft pick corrected",
                correlation_id=correlation,
            )
            # The original row must be marked superseded (deactivating it
            # for the partial-unique "one active row per slot" index)
            # *before* the replacement row exists to take its place, or the
            # two rows would momentarily both be active for the same slot.
            # That ordering means this UPDATE's FK reference to
            # `replacement_id` is written before that row exists --
            # deferred to commit, see the FK's own docstring in
            # migrations/versions/0016_draft_operations.py.
            replacement_id = _id()
            conn.execute(
                "UPDATE draft_pick SET superseded_by_draft_pick_id=? WHERE draft_pick_id=?",
                (replacement_id, draft_pick_id),
            )
            conn.execute(
                "INSERT INTO draft_pick VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                (
                    replacement_id,
                    original["draft_id"],
                    original["season_id"],
                    original["overall_number"],
                    original["draft_round"],
                    original["round_position"],
                    original["original_season_entry_id"],
                    original["current_season_entry_id"],
                ),
            )
            correction_event = append_event(
                conn,
                actor=actor,
                action="draft.pick.corrected",
                entity_type="draft.pick",
                entity_id=draft_pick_id,
                correlation_id=correlation,
                reason=reason,
                before_state={
                    "season_player_id": original["selected_season_player_id"],
                    "season_entry_id": original["current_season_entry_id"],
                },
                after_state={"replacement_draft_pick_id": replacement_id},
            )
            conn.execute(
                "INSERT INTO draft_pick_correction VALUES (?, ?, ?, ?, ?, ?)",
                (_id(), draft_pick_id, replacement_id, at, reason, correction_event.event_id),
            )
        return self.picks(season_id)[original["overall_number"] - 1]

    def finalize(self, season_id, *, actor=ActorContext.anonymous_operator("admin"), note=None):
        with transaction(self.database) as conn:
            if self.database.engine.dialect.name == "sqlite":
                conn.execute("UPDATE bbbffl_season SET updated_at=updated_at WHERE season_id=?", (season_id,))
            draft = self._locked_draft(conn, season_id)
            if draft["finalized_at"] is not None:
                raise DraftFinalizedError("draft is already finalized")
            counts = conn.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) AS completed "
                "FROM draft_pick WHERE draft_id=? AND superseded_by_draft_pick_id IS NULL",
                (draft["draft_id"],),
            ).fetchone()
            if not counts["total"] or (counts["completed"] or 0) != counts["total"]:
                raise DraftNotCompleteError(
                    f"{(counts['completed'] or 0)}/{counts['total'] or 0} picks completed -- "
                    "every configured pick must be completed before finalisation"
                )
            # Defence in depth: execute_pick's squad-capacity check already
            # guarantees this once every pick is completed (see
            # docs/draft-ledger.md), but finalisation is the one place that
            # must refuse outright if that invariant is ever violated rather
            # than silently freezing an inconsistent squad.
            uneven = conn.execute(
                "SELECT current_season_entry_id, COUNT(*) AS n FROM draft_pick "
                "WHERE draft_id=? AND superseded_by_draft_pick_id IS NULL "
                "GROUP BY current_season_entry_id HAVING COUNT(*) <> ?",
                (draft["draft_id"], draft["target_squad_size"]),
            ).fetchall()
            if uneven:
                raise DraftNotCompleteError("resulting squads do not match the configured squad size")
            at = _now()
            conn.execute(
                "UPDATE season_draft SET finalized_at=?, finalized_note=? WHERE draft_id=?",
                (at, note, draft["draft_id"]),
            )
            append_event(
                conn,
                actor=actor,
                action="draft.finalized",
                entity_type="draft",
                entity_id=draft["draft_id"],
                reason=note,
                before_state={"finalized_at": None},
                after_state={"finalized_at": at, "finalized_note": note},
            )
        return self.status(season_id)

    def reopen(self, season_id, *, actor=ActorContext.anonymous_operator("admin"), reason):
        """Deliberately separate from `finalize`/ordinary controls -- see
        the module docstring. Callers (routes/scripts) must gate this behind
        their own explicit, hard-to-mistake confirmation step; this method
        itself only enforces that a reason is always given."""
        if not reason or not reason.strip():
            raise ValueError("reopening a finalized draft requires an explicit reason")
        with transaction(self.database) as conn:
            if self.database.engine.dialect.name == "sqlite":
                conn.execute("UPDATE bbbffl_season SET updated_at=updated_at WHERE season_id=?", (season_id,))
            draft = self._locked_draft(conn, season_id)
            if draft["finalized_at"] is None:
                raise DraftStateError("draft is not finalized")
            finalized_at = draft["finalized_at"]
            conn.execute(
                "UPDATE season_draft SET finalized_at=NULL, finalized_note=NULL WHERE draft_id=?",
                (draft["draft_id"],),
            )
            append_event(
                conn,
                actor=actor,
                action="draft.reopened",
                entity_type="draft",
                entity_id=draft["draft_id"],
                reason=reason,
                before_state={"finalized_at": finalized_at},
                after_state={"finalized_at": None},
            )
        return self.status(season_id)

    def next_pick(self, season_id):
        row = self.database.execute(
            "SELECT p.* FROM draft_pick p JOIN season_draft d ON d.draft_id=p.draft_id "
            "WHERE d.season_id=? AND p.completed_at IS NULL ORDER BY p.overall_number LIMIT 1",
            (season_id,),
        ).fetchone()
        return DraftPick(**dict(row)) if row else None

    def transfer_pick(
        self,
        draft_pick_id,
        to_entry_id,
        *,
        actor=ActorContext.anonymous_operator("admin"),
        reason=None,
        transferred_at=None,
    ):
        at = transferred_at or _now()
        with transaction(self.database) as conn:
            if self.database.engine.dialect.name == "sqlite":
                conn.execute(
                    "UPDATE draft_pick SET current_season_entry_id=current_season_entry_id WHERE draft_pick_id=?",
                    (draft_pick_id,),
                )
            pick = conn.execute(
                "SELECT * FROM draft_pick WHERE draft_pick_id=?" + _for_update_suffix(self.database), (draft_pick_id,)
            ).fetchone()
            entry = conn.execute(
                "SELECT season_id FROM season_entry WHERE season_entry_id=?", (to_entry_id,)
            ).fetchone()
            if not pick or not entry or entry["season_id"] != pick["season_id"]:
                raise DraftStateError("pick and destination must exist in the same season")
            if pick["completed_at"] is not None:
                raise DraftPickCompletedError("completed picks cannot be transferred")
            if pick["current_season_entry_id"] == to_entry_id:
                raise DraftStateError("destination already owns the pick")
            correlation = new_correlation_id()
            event = append_event(
                conn,
                actor=actor,
                action="draft.pick.transferred",
                entity_type="draft.pick",
                entity_id=draft_pick_id,
                correlation_id=correlation,
                reason=reason,
                before_state={"owner": pick["current_season_entry_id"]},
                after_state={"owner": to_entry_id},
            )
            transfer = PickTransfer(
                _id(), draft_pick_id, pick["current_season_entry_id"], to_entry_id, at, reason, event.event_id
            )
            conn.execute(
                "INSERT INTO draft_pick_transfer VALUES (?, ?, ?, ?, ?, ?, ?)", tuple(transfer.__dict__.values())
            )
            conn.execute(
                "UPDATE draft_pick SET current_season_entry_id=? WHERE draft_pick_id=?", (to_entry_id, draft_pick_id)
            )
        return transfer

    def transfer_history(self, draft_pick_id):
        rows = self.database.execute(
            "SELECT * FROM draft_pick_transfer WHERE draft_pick_id=? ORDER BY transferred_at, transfer_id",
            (draft_pick_id,),
        ).fetchall()
        return [PickTransfer(**dict(row)) for row in rows]

    def execute_pick(
        self,
        season_id,
        selecting_entry_id,
        season_player_id,
        *,
        pick_id=None,
        actor=ActorContext.anonymous_operator("admin"),
        reason="draft selection",
        completed_at=None,
    ):
        at = completed_at or _now()
        correlation = new_correlation_id()
        with transaction(self.database) as conn:
            if self.database.engine.dialect.name == "sqlite":
                conn.execute("UPDATE bbbffl_season SET updated_at=updated_at WHERE season_id=?", (season_id,))
            draft = self._locked_draft(conn, season_id)
            if draft["finalized_at"] is not None:
                raise DraftFinalizedError("draft is already finalized")
            if draft["paused_at"] is not None:
                raise DraftPausedError("draft is currently paused")
            next_row = conn.execute(
                "SELECT p.* FROM draft_pick p JOIN season_draft d ON d.draft_id=p.draft_id "
                "WHERE d.season_id=? AND p.completed_at IS NULL ORDER BY p.overall_number LIMIT 1"
                + _for_update_suffix(self.database),
                (season_id,),
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
                conn,
                season_player_id,
                selecting_entry_id,
                effective_at=at,
                actor=actor,
                reason=reason,
                correlation_id=correlation,
            )
            result = conn.execute(
                "UPDATE draft_pick SET selected_season_player_id=?, completed_at=? "
                "WHERE draft_pick_id=? AND completed_at IS NULL",
                (season_player_id, at, next_row["draft_pick_id"]),
            )
            if result.rowcount != 1:
                raise DraftPickCompletedError("pick was completed concurrently")
            append_event(
                conn,
                actor=actor,
                action="draft.pick.completed",
                entity_type="draft.pick",
                entity_id=next_row["draft_pick_id"],
                correlation_id=correlation,
                reason=reason,
                after_state={
                    "season_player_id": season_player_id,
                    "season_entry_id": selecting_entry_id,
                    "overall_number": next_row["overall_number"],
                },
            )
        return self.picks(season_id)[next_row["overall_number"] - 1]
