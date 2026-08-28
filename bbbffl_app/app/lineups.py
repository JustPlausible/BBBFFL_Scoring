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

`submit` and `submit_positions` share one core (`_finalize_submission`): the
same `weekly_lineup` row lock, the same `expected_submission_version`
compare-and-swap, and the same `lock_guard` integration point, so every
submission source enforces identical lock-integrity/concurrency rules.
`submit` is the coach path -- it reads live content from
`weekly_lineup_draft_slot`. `submit_positions` accepts an explicit
`positions` mapping instead, for any non-coach source whose content
originates elsewhere: `app.carry_forward` (`source_type="carry_forward"`,
an exact copy of a prior round's submitted lineup) and `app.lineup_proxy`
submissions that go through the ordinary draft (`source_type=
"scorer_proxy"`) may use either, but a source that must not read/displace
the entry's own private draft always uses `submit_positions`.
"""

import json
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.audit import ENTITY_TYPE_LINEUP, LINEUP_SUBMITTED, ActorContext, append_event
from app.db import _for_update_suffix, transaction
from app.season import _now

POSITIONS = ("F1", "F2", "F3", "M1", "M2", "M3", "Ruck", "Tackler", "Interchange")
SUBMISSION_SOURCES = frozenset({"coach", "scorer_proxy", "carry_forward", "system_derived"})
# Whole-draft (not per-position) origin of the *current* draft revision --
# see migrations/versions/0018_proxy_draft_source.py's docstring and
# `submit`'s use of it below.
DRAFT_SOURCES = frozenset({"coach", "scorer_proxy"})


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
    draft_source: str


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

    def save_draft(
        self, season_id, competition_id, round_id, entry_id, positions, *, expected_revision, draft_source="coach"
    ):
        """`draft_source` records the whole-draft origin of the resulting
        revision -- `"coach"` (default, ordinary editing) or
        `"scorer_proxy"` (only `app.lineup_proxy.LineupProxyService.
        create_or_amend` passes this). See `DRAFT_SOURCES` and `submit`'s
        use of it, and migrations/versions/0018_proxy_draft_source.py's
        docstring for why this exists. A coach's own subsequent edit
        (leaving `draft_source` at its default) resets it back to
        `"coach"` -- this tracks only the *current* revision's origin, not
        a history of every edit."""
        if draft_source not in DRAFT_SOURCES:
            raise LineupIntegrityError(f"unknown draft source: {draft_source!r}")
        selected = self._normalise(positions)
        now = _now()
        try:
            with transaction(self.database) as conn:
                self._validate_scope(conn, season_id, competition_id, round_id, entry_id)
                self._validate_players(conn, season_id, selected)
                row = conn.execute(
                    "SELECT * FROM weekly_lineup WHERE season_id=? AND competition_id=? AND bbbffl_round_id=? AND season_entry_id=?"
                    + _for_update_suffix(self.database),
                    (season_id, competition_id, round_id, entry_id),
                ).fetchone()
                if row is None:
                    if expected_revision != 0:
                        raise LineupConflictError("draft does not exist at expected revision")
                    lineup_id, revision, created = str(uuid4()), 1, now
                    conn.execute(
                        "INSERT INTO weekly_lineup VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                        (
                            lineup_id,
                            season_id,
                            competition_id,
                            round_id,
                            entry_id,
                            revision,
                            created,
                            now,
                            draft_source,
                        ),
                    )
                else:
                    if row["draft_revision"] != expected_revision:
                        raise LineupConflictError("stale draft revision")
                    lineup_id, revision, created = row["lineup_id"], expected_revision + 1, row["created_at"]
                    result = conn.execute(
                        "UPDATE weekly_lineup SET draft_revision=?, updated_at=?, draft_source=? WHERE lineup_id=? AND draft_revision=?",
                        (revision, now, draft_source, lineup_id, expected_revision),
                    )
                    # SELECT FOR UPDATE serializes PostgreSQL writers; this CAS
                    # also documents and protects the authoritative transition.
                    if not result.rowcount:
                        raise LineupConflictError("stale draft revision")
                    conn.execute("DELETE FROM weekly_lineup_draft_slot WHERE lineup_id=?", (lineup_id,))
                for position in POSITIONS:
                    conn.execute(
                        "INSERT INTO weekly_lineup_draft_slot VALUES (?, ?, ?)",
                        (lineup_id, position, selected[position]),
                    )
        except IntegrityError as exc:
            raise LineupConflictError("concurrent draft creation or edit") from exc
        return LineupDraft(
            lineup_id, season_id, competition_id, round_id, entry_id, revision, selected, created, now, draft_source
        )

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
            row["draft_source"],
        )

    def submit(
        self,
        lineup_id,
        *,
        expected_draft_revision,
        expected_submission_version,
        actor=ActorContext.anonymous_operator("coach"),
        source_type="coach",
        source_detail=None,
        reason=None,
        lock_guard=None,
    ):
        """Submit the lineup's own current private draft content
        (`weekly_lineup_draft_slot`) as a new immutable version.

        `source_type="coach"` (the default) is the only source whose
        submitted content is read live from the draft -- every other
        declared source (`scorer_proxy`, `carry_forward`, `system_derived`)
        supplies its content explicitly via `submit_positions` instead, so
        it can never silently diverge from, or be confused with, whatever
        the coach currently has open in their own private draft (see
        `submit_positions`, app/carry_forward.py, app/lineup_proxy.py).

        If the draft's current revision was last written by a scorer/admin
        proxy operation (`weekly_lineup.draft_source == "scorer_proxy"`,
        set only by `app.lineup_proxy.LineupProxyService.create_or_amend`),
        this refuses unless `source_type="scorer_proxy"` too -- a proxy-
        authored draft can never silently become a `source_type="coach"`
        submission with no trace of the intervention (see
        migrations/versions/0018_proxy_draft_source.py's docstring). A
        coach's own subsequent `save_draft` call resets `draft_source` back
        to `"coach"`, lifting this again.
        """
        if source_type not in SUBMISSION_SOURCES:
            raise LineupIntegrityError("unknown submission source")
        if lock_guard is not None and hasattr(lock_guard, "materialize"):
            # Runs in its own standalone transaction, deliberately *before*
            # this method opens its own below -- see the module docstring
            # and app.lockouts's docstring for why a lock observed here
            # must survive even if this submission attempt is rejected.
            lock_guard.materialize(lineup_id)
        with transaction(self.database) as conn:
            lineup = self._lock_lineup_row(conn, lineup_id)
            if lineup["draft_revision"] != expected_draft_revision:
                raise LineupConflictError("stale draft revision at submission")
            if lineup["draft_source"] == "scorer_proxy" and source_type != "scorer_proxy":
                raise LineupIntegrityError(
                    "this draft's current content was last saved by a scorer/admin proxy operation; "
                    "submit it via LineupProxyService.submit (source_type='scorer_proxy'), not the "
                    "ordinary coach path -- or have the coach save their own draft edit first"
                )
            slots = conn.execute(
                "SELECT position, season_player_id FROM weekly_lineup_draft_slot WHERE lineup_id=?", (lineup_id,)
            ).fetchall()
            positions = self._normalise({row["position"]: row["season_player_id"] for row in slots})
            version = self._finalize_submission(
                conn,
                lineup,
                positions,
                based_on_draft_revision=expected_draft_revision,
                expected_submission_version=expected_submission_version,
                actor=actor,
                source_type=source_type,
                source_detail=source_detail,
                reason=reason,
                lock_guard=lock_guard,
            )
        # Read back outside the transaction: `self.database.execute` (used by
        # `get_submission`) is a separate connection/session that cannot see
        # this transaction's writes until it has committed above.
        return self.get_submission(lineup_id, version)

    def submit_positions(
        self,
        lineup_id,
        positions,
        *,
        expected_submission_version,
        actor,
        source_type,
        source_detail=None,
        reason=None,
        lock_guard=None,
        require_unchanged=None,
    ):
        """Submit an explicit `positions` mapping -- e.g. an exact copy of a
        prior round's submitted lineup (see `app.carry_forward`) -- as a new
        immutable version, *without* reading or displacing whatever the
        entry currently has saved in its own private draft
        (`weekly_lineup_draft_slot` is never touched).

        `source_type` must not be `"coach"`: a coach's own submission always
        goes through `submit()`, which reads live draft content, so its
        history can never silently diverge from what they see on screen.

        Shares every concurrency/lock-integrity rule with `submit()` --
        the same `weekly_lineup` row lock, the same
        `expected_submission_version` compare-and-swap, the same
        `lock_guard` integration point -- via `_finalize_submission`, so
        lock evaluation is never duplicated for this submission source (see
        app/lockouts.py's module docstring).

        `require_unchanged`, if given, is a `(lineup_id, expected_version)`
        pair for a *second* lineup this submission's `positions` were
        derived from (e.g. carry-forward's source round) -- typically
        resolved by the caller via a separate, unlocked read before calling
        this method. That second row is locked (`FOR UPDATE`) and its
        `effective_submission_version` re-checked *inside this same
        transaction*, atomically with the target write: if the source was
        resubmitted after the caller resolved it but before this commits,
        this raises `LineupConflictError` instead of silently persisting a
        now-stale copy. Carry-forward's target round always has a strictly
        later `bbbffl_round.sequence` than any legitimate source round, so
        this always locks the (later) target first and the (earlier)
        source second, in the same order across every caller -- no
        cross-operation lock-order deadlock is possible.
        """
        if source_type == "coach":
            raise LineupIntegrityError("coach submissions must go through submit(), which reads live draft content")
        if source_type not in SUBMISSION_SOURCES:
            raise LineupIntegrityError("unknown submission source")
        positions = self._normalise(positions)
        if lock_guard is not None and hasattr(lock_guard, "materialize"):
            lock_guard.materialize(lineup_id)
        with transaction(self.database) as conn:
            lineup = self._lock_lineup_row(conn, lineup_id)
            if require_unchanged is not None:
                source_lineup_id, expected_source_version = require_unchanged
                source_row = self._lock_lineup_row(conn, source_lineup_id)
                source_current = source_row["effective_submission_version"] or 0
                if source_current != expected_source_version:
                    raise LineupConflictError(
                        "source lineup was resubmitted after being resolved; re-resolve and retry"
                    )
            version = self._finalize_submission(
                conn,
                lineup,
                positions,
                based_on_draft_revision=lineup["draft_revision"],
                expected_submission_version=expected_submission_version,
                actor=actor,
                source_type=source_type,
                source_detail=source_detail,
                reason=reason,
                lock_guard=lock_guard,
            )
        # See submit()'s matching comment: read back only after commit.
        return self.get_submission(lineup_id, version)

    def get_or_create_header(self, season_id, competition_id, round_id, entry_id):
        """Return `(lineup_id, effective_submission_version)` for this
        season/competition/round/entry, creating an empty (all-`None`)
        private draft header via `save_draft` if this entry has never
        touched this round at all -- so a non-coach submission source
        (carry-forward, proxy) always has a `weekly_lineup` row to submit
        into without inventing or pre-populating draft content. A
        concurrent first-touch race is resolved the same way any other
        concurrent draft creation is (`LineupConflictError`; see
        `save_draft`)."""
        row = self.database.execute(
            "SELECT lineup_id, effective_submission_version FROM weekly_lineup "
            "WHERE season_id=? AND competition_id=? AND bbbffl_round_id=? AND season_entry_id=?",
            (season_id, competition_id, round_id, entry_id),
        ).fetchone()
        if row is not None:
            return row["lineup_id"], row["effective_submission_version"] or 0
        draft = self.save_draft(season_id, competition_id, round_id, entry_id, {}, expected_revision=0)
        return draft.lineup_id, 0

    def _lock_lineup_row(self, conn, lineup_id):
        # SQLite obtains its single writer lock before reading; PostgreSQL
        # takes row locks below. Both then perform a compare-and-swap.
        if self.database.engine.dialect.name == "sqlite":
            conn.execute("UPDATE weekly_lineup SET updated_at=updated_at WHERE lineup_id=?", (lineup_id,))
        lineup = conn.execute(
            "SELECT * FROM weekly_lineup WHERE lineup_id=?" + _for_update_suffix(self.database), (lineup_id,)
        ).fetchone()
        if not lineup:
            raise KeyError(lineup_id)
        return lineup

    def _finalize_submission(
        self,
        conn,
        lineup,
        positions,
        *,
        based_on_draft_revision,
        expected_submission_version,
        actor,
        source_type,
        source_detail,
        reason,
        lock_guard,
    ):
        lineup_id = lineup["lineup_id"]
        current = lineup["effective_submission_version"] or 0
        if current != expected_submission_version:
            raise LineupConflictError("stale submission version")
        lifecycle = conn.execute(
            "SELECT state FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?" + _for_update_suffix(self.database),
            (lineup["bbbffl_round_id"],),
        ).fetchone()
        if not lifecycle or lifecycle["state"] != "open":
            raise LineupIntegrityError("BBBFFL round does not currently permit submission")
        self._validate_players(conn, lineup["season_id"], positions, lock=True)
        self._validate_ownership(conn, lineup["season_entry_id"], positions)
        if lock_guard is not None:
            # `guard_transition`'s caller-owned invariant: this must run
            # under the `weekly_lineup` row lock already taken above, so
            # two concurrent submissions for the same lineup serialize
            # against each other and against the lock evidence each one
            # observes/materializes (see app/lockouts.py). Identical for
            # every submission source -- carry-forward and proxy submissions
            # get no special exemption from a locked/indeterminate position.
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
            (
                lineup_id,
                version,
                based_on_draft_revision,
                now,
                actor.actor_type,
                actor.actor_id,
                actor.actor_role,
                source_type,
                json.dumps(source_detail, sort_keys=True) if source_detail is not None else None,
                reason,
            ),
        )
        for position in POSITIONS:
            conn.execute(
                "INSERT INTO weekly_lineup_submission_slot VALUES (?, ?, ?, ?)",
                (lineup_id, version, position, positions[position]),
            )
        result = conn.execute(
            "UPDATE weekly_lineup SET effective_submission_version=?, updated_at=? WHERE lineup_id=? AND "
            "((effective_submission_version IS NULL AND ?=0) OR effective_submission_version=?)",
            (version, now, lineup_id, expected_submission_version, expected_submission_version),
        )
        if not result.rowcount:
            raise LineupConflictError("concurrent submission")
        append_event(
            conn,
            actor=actor,
            action=LINEUP_SUBMITTED,
            entity_type=ENTITY_TYPE_LINEUP,
            entity_id=lineup_id,
            entity_version=str(version),
            reason=reason,
            after_state={"effective_submission_version": version},
            payload={"source_type": source_type, "based_on_draft_revision": based_on_draft_revision},
        )
        return version

    def get_effective_submission(self, lineup_id):
        row = self.database.execute(
            "SELECT effective_submission_version FROM weekly_lineup WHERE lineup_id=?", (lineup_id,)
        ).fetchone()
        if not row or row["effective_submission_version"] is None:
            return None
        return self.get_submission(lineup_id, row["effective_submission_version"])

    def get_submission(self, lineup_id, version):
        row = self.database.execute(
            "SELECT * FROM weekly_lineup_submission WHERE lineup_id=? AND version=?", (lineup_id, version)
        ).fetchone()
        if not row:
            return None
        slots = self.database.execute(
            "SELECT position, season_player_id FROM weekly_lineup_submission_slot WHERE lineup_id=? AND version=?",
            (lineup_id, version),
        ).fetchall()
        return SubmittedLineup(
            row["lineup_id"],
            row["version"],
            row["based_on_draft_revision"],
            {s["position"]: s["season_player_id"] for s in slots},
            row["submitted_at"],
            row["actor_type"],
            row["actor_id"],
            row["actor_role"],
            row["source_type"],
            json.loads(row["source_detail"]) if row["source_detail"] else None,
            row["reason"],
        )

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
        row = conn.execute(
            "SELECT c.season_id AS competition_season, r.competition_id, e.season_id AS entry_season FROM competition_stream c JOIN bbbffl_round r ON r.competition_id=c.competition_id JOIN season_entry e ON e.season_entry_id=? WHERE c.competition_id=? AND r.bbbffl_round_id=?",
            (entry_id, competition_id, round_id),
        ).fetchone()
        if not row:
            raise LineupIntegrityError("unknown lineup season/competition/round/entry scope")
        if row["competition_season"] != season_id or row["entry_season"] != season_id:
            raise LineupIntegrityError("lineup scope crosses season identities")

    def _validate_players(self, conn, season_id, positions, lock=False):
        players = sorted({p for p in positions.values() if p is not None})
        for player_id in players:
            row = conn.execute(
                "SELECT season_id FROM season_player_pool WHERE season_player_id=?"
                + (_for_update_suffix(self.database) if lock else ""),
                (player_id,),
            ).fetchone()
            if not row or row["season_id"] != season_id:
                raise LineupIntegrityError("selection must reference a season-player in the lineup season")

    @staticmethod
    def _validate_ownership(conn, entry_id, positions):
        for player_id in {p for p in positions.values() if p is not None}:
            owner = conn.execute(
                "SELECT season_entry_id FROM player_ownership_period WHERE season_player_id=? AND released_at IS NULL",
                (player_id,),
            ).fetchone()
            if not owner or owner["season_entry_id"] != entry_id:
                raise LineupIntegrityError("selected player is not currently owned by the submitting entry")
