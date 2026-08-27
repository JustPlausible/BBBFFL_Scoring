"""Durable boundary between private weekly drafts and official selections.

`submit`'s optional `lock_guard` is the sole integration point with
app/lockouts.py's player-level AFL-match lockout decision. If `lock_guard`
has a `materialize(lineup_id)` method, `submit` calls it *before* opening
its own transaction, so app/lockouts.py can durably record any lock it
observes independently of whatever this method goes on to do (see
app.lockouts's module docstring on why that must happen outside this
method's transaction, not inside it). `lock_guard` itself is then invoked
as a plain callable inside this method's own transaction (after the
previous effective submission is read, before the new version is written)
and may raise to reject a submission that would mutate a locked/
indeterminate position. This module has no other lockout awareness and does
not import app/lockouts.py, keeping the two responsibilities -- immutable
submission history here, lock evaluation/evidence there -- decoupled.
"""

import json
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.audit import ActorContext, ENTITY_TYPE_LINEUP, LINEUP_SUBMITTED, append_event
from app.db import _for_update_suffix, transaction
from app.season import _now

POSITIONS = ("F1", "F2", "F3", "M1", "M2", "M3", "Ruck", "Tackler", "Interchange")
SUBMISSION_SOURCES = frozenset({"coach", "scorer_proxy", "carry_forward", "system_derived"})


class LineupConflictError(RuntimeError):
    """The caller based a write on a revision which is no longer current."""


class LineupIntegrityError(ValueError):
    pass


@dataclass(frozen=True)
class LineupDraft:
    lineup_id: str
    season_id: str
    competition_id: str
    bbbffl_round_id: str
    season_entry_id: str
    revision: int
    positions: dict
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SubmittedLineup:
    lineup_id: str
    version: int
    based_on_draft_revision: int
    positions: dict
    submitted_at: str
    actor_type: str
    actor_id: str | None
    actor_role: str | None
    source_type: str
    source_detail: dict | None
    reason: str | None


class WeeklyLineupRepository:
    def __init__(self, database):
        self.database = database

    def save_draft(self, season_id, competition_id, round_id, entry_id, positions, *, expected_revision):
        selected = self._normalise(positions)
        now = _now()
        try:
            with transaction(self.database) as conn:
                self._validate_scope(conn, season_id, competition_id, round_id, entry_id)
                self._validate_players(conn, season_id, selected)
                row = conn.execute(
                    "SELECT * FROM weekly_lineup WHERE season_id=? AND competition_id=? AND bbbffl_round_id=? AND season_entry_id=?" + _for_update_suffix(self.database),
                    (season_id, competition_id, round_id, entry_id),
                ).fetchone()
                if row is None:
                    if expected_revision != 0:
                        raise LineupConflictError("draft does not exist at expected revision")
                    lineup_id, revision, created = str(uuid4()), 1, now
                    conn.execute(
                        "INSERT INTO weekly_lineup VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                        (lineup_id, season_id, competition_id, round_id, entry_id, revision, created, now),
                    )
                else:
                    if row["draft_revision"] != expected_revision:
                        raise LineupConflictError("stale draft revision")
                    lineup_id, revision, created = row["lineup_id"], expected_revision + 1, row["created_at"]
                    result = conn.execute(
                        "UPDATE weekly_lineup SET draft_revision=?, updated_at=? WHERE lineup_id=? AND draft_revision=?",
                        (revision, now, lineup_id, expected_revision),
                    )
                    # SELECT FOR UPDATE serializes PostgreSQL writers; this CAS
                    # also documents and protects the authoritative transition.
                    if not result.rowcount:
                        raise LineupConflictError("stale draft revision")
                    conn.execute("DELETE FROM weekly_lineup_draft_slot WHERE lineup_id=?", (lineup_id,))
                for position in POSITIONS:
                    conn.execute("INSERT INTO weekly_lineup_draft_slot VALUES (?, ?, ?)", (lineup_id, position, selected[position]))
        except IntegrityError as exc:
            raise LineupConflictError("concurrent draft creation or edit") from exc
        return LineupDraft(lineup_id, season_id, competition_id, round_id, entry_id, revision, selected, created, now)

    def get_draft(self, season_id, competition_id, round_id, entry_id):
        # Materialise the header and slots in one database statement. Under
        # PostgreSQL READ COMMITTED, merely placing two SELECTs in the same
        # transaction would not provide a single snapshot: a draft save could
        # commit between them. The join makes it impossible to pair revision N
        # metadata with revision N+1 slots.
        rows = self.database.execute(
            "SELECT w.*, s.position, s.season_player_id "
            "FROM weekly_lineup w "
            "LEFT JOIN weekly_lineup_draft_slot s ON s.lineup_id=w.lineup_id "
            "WHERE w.season_id=? AND w.competition_id=? "
            "AND w.bbbffl_round_id=? AND w.season_entry_id=? "
            "ORDER BY s.position",
            (season_id, competition_id, round_id, entry_id),
        ).fetchall()
        if not rows:
            return None
        row = rows[0]
        positions = {slot["position"]: slot["season_player_id"] for slot in rows}
        if set(positions) != set(POSITIONS):
            raise LineupIntegrityError("persisted draft does not contain all scoring positions")
        return LineupDraft(
            row["lineup_id"],
            row["season_id"],
            row["competition_id"],
            row["bbbffl_round_id"],
            row["season_entry_id"],
            row["draft_revision"],
            positions,
            row["created_at"],
            row["updated_at"],
        )

    def submit(self, lineup_id, *, expected_draft_revision, expected_submission_version, actor=ActorContext.anonymous_operator("coach"), source_type="coach", source_detail=None, reason=None, lock_guard=None):
        if source_type not in SUBMISSION_SOURCES:
            raise LineupIntegrityError("unknown submission source")
        if lock_guard is not None and hasattr(lock_guard, "materialize"):
            # Runs in its own standalone transaction, deliberately *before*
            # this method opens its own below -- see the module docstring
            # and app.lockouts's docstring for why a lock observed here
            # must survive even if this submission attempt is rejected.
            lock_guard.materialize(lineup_id)
        with transaction(self.database) as conn:
            # SQLite obtains its single writer lock before reading; PostgreSQL
            # takes row locks below. Both then perform a compare-and-swap.
            if self.database.engine.dialect.name == "sqlite":
                conn.execute("UPDATE weekly_lineup SET updated_at=updated_at WHERE lineup_id=?", (lineup_id,))
            lineup = conn.execute("SELECT * FROM weekly_lineup WHERE lineup_id=?" + _for_update_suffix(self.database), (lineup_id,)).fetchone()
            if not lineup:
                raise KeyError(lineup_id)
            if lineup["draft_revision"] != expected_draft_revision:
                raise LineupConflictError("stale draft revision at submission")
            current = lineup["effective_submission_version"] or 0
            if current != expected_submission_version:
                raise LineupConflictError("stale submission version")
            lifecycle = conn.execute("SELECT state FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?" + _for_update_suffix(self.database), (lineup["bbbffl_round_id"],)).fetchone()
            if not lifecycle or lifecycle["state"] != "open":
                raise LineupIntegrityError("BBBFFL round does not currently permit submission")
            slots = conn.execute("SELECT position, season_player_id FROM weekly_lineup_draft_slot WHERE lineup_id=?", (lineup_id,)).fetchall()
            positions = {row["position"]: row["season_player_id"] for row in slots}
            positions = self._normalise(positions)
            self._validate_players(conn, lineup["season_id"], positions, lock=True)
            self._validate_ownership(conn, lineup["season_entry_id"], positions)
            if lock_guard is not None:
                # `guard_transition`'s caller-owned invariant: this must run
                # under the `weekly_lineup` row lock already taken above, so
                # two concurrent submissions for the same lineup serialize
                # against each other and against the lock evidence each one
                # observes/materializes (see app/lockouts.py).
                if current:
                    previous_rows = conn.execute(
                        "SELECT position, season_player_id FROM weekly_lineup_submission_slot WHERE lineup_id=? AND version=?",
                        (lineup_id, current),
                    ).fetchall()
                    previous_positions = {row["position"]: row["season_player_id"] for row in previous_rows}
                else:
                    previous_positions = {position: None for position in POSITIONS}
                lock_guard(conn, lineup, previous_positions, positions)
            version, now = current + 1, _now()
            conn.execute(
                "INSERT INTO weekly_lineup_submission VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (lineup_id, version, expected_draft_revision, now, actor.actor_type, actor.actor_id, actor.actor_role, source_type, json.dumps(source_detail, sort_keys=True) if source_detail is not None else None, reason),
            )
            for position in POSITIONS:
                conn.execute("INSERT INTO weekly_lineup_submission_slot VALUES (?, ?, ?, ?)", (lineup_id, version, position, positions[position]))
            result = conn.execute(
                "UPDATE weekly_lineup SET effective_submission_version=?, updated_at=? WHERE lineup_id=? AND "
                "((effective_submission_version IS NULL AND ?=0) OR effective_submission_version=?)",
                (version, now, lineup_id, expected_submission_version, expected_submission_version),
            )
            if not result.rowcount:
                raise LineupConflictError("concurrent submission")
            append_event(conn, actor=actor, action=LINEUP_SUBMITTED, entity_type=ENTITY_TYPE_LINEUP, entity_id=lineup_id, entity_version=str(version), reason=reason, after_state={"effective_submission_version": version}, payload={"source_type": source_type, "based_on_draft_revision": expected_draft_revision})
        return self.get_submission(lineup_id, version)

    def get_effective_submission(self, lineup_id):
        row = self.database.execute("SELECT effective_submission_version FROM weekly_lineup WHERE lineup_id=?", (lineup_id,)).fetchone()
        if not row or row["effective_submission_version"] is None:
            return None
        return self.get_submission(lineup_id, row["effective_submission_version"])

    def get_submission(self, lineup_id, version):
        row = self.database.execute("SELECT * FROM weekly_lineup_submission WHERE lineup_id=? AND version=?", (lineup_id, version)).fetchone()
        if not row:
            return None
        slots = self.database.execute("SELECT position, season_player_id FROM weekly_lineup_submission_slot WHERE lineup_id=? AND version=?", (lineup_id, version)).fetchall()
        return SubmittedLineup(row["lineup_id"], row["version"], row["based_on_draft_revision"], {s["position"]: s["season_player_id"] for s in slots}, row["submitted_at"], row["actor_type"], row["actor_id"], row["actor_role"], row["source_type"], json.loads(row["source_detail"]) if row["source_detail"] else None, row["reason"])

    @staticmethod
    def _normalise(positions):
        unknown = set(positions) - set(POSITIONS)
        if unknown:
            raise LineupIntegrityError(f"unknown scoring positions: {sorted(unknown)}")
        result = {position: positions.get(position) for position in POSITIONS}
        chosen = [player for player in result.values() if player is not None]
        if len(chosen) != len(set(chosen)):
            raise LineupIntegrityError("a player cannot occupy multiple scoring positions")
        return result

    def _validate_scope(self, conn, season_id, competition_id, round_id, entry_id):
        row = conn.execute("SELECT c.season_id AS competition_season, r.competition_id, e.season_id AS entry_season FROM competition_stream c JOIN bbbffl_round r ON r.competition_id=c.competition_id JOIN season_entry e ON e.season_entry_id=? WHERE c.competition_id=? AND r.bbbffl_round_id=?", (entry_id, competition_id, round_id)).fetchone()
        if not row:
            raise LineupIntegrityError("unknown lineup season/competition/round/entry scope")
        if row["competition_season"] != season_id or row["entry_season"] != season_id:
            raise LineupIntegrityError("lineup scope crosses season identities")

    def _validate_players(self, conn, season_id, positions, lock=False):
        players = sorted({p for p in positions.values() if p is not None})
        for player_id in players:
            row = conn.execute("SELECT season_id FROM season_player_pool WHERE season_player_id=?" + (_for_update_suffix(self.database) if lock else ""), (player_id,)).fetchone()
            if not row or row["season_id"] != season_id:
                raise LineupIntegrityError("selection must reference a season-player in the lineup season")

    @staticmethod
    def _validate_ownership(conn, entry_id, positions):
        for player_id in {p for p in positions.values() if p is not None}:
            owner = conn.execute("SELECT season_entry_id FROM player_ownership_period WHERE season_player_id=? AND released_at IS NULL", (player_id,)).fetchone()
            if not owner or owner["season_entry_id"] != entry_id:
                raise LineupIntegrityError("selected player is not currently owned by the submitting entry")
