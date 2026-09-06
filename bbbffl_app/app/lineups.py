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

## Round lifecycle vs. position-level lock state (issue #144)

`ORDINARY_SUBMISSION_ALLOWED_STATES` is the round-lifecycle gate ("is
ordinary submission even in scope for this round at all"), and it is
deliberately coarser than the position-level lock decision `lock_guard`
makes ("is *this particular* change legal right now"). A BBBFFL round
becomes `live` the instant its first AFL match begins, but staged lockout
(see app/lockouts.py) means most positions typically remain individually
editable for a while after that -- `live` is a fact about play having
started somewhere, never a global submission freeze. `_finalize_submission`
therefore accepts an ordinary submission for either `"open"` or `"live"`,
and always delegates the actual per-position accept/reject decision to
`lock_guard`, unchanged. Because that delegation is the *only* thing
standing between a `live` round and an unrestricted rewrite of every
position, `_finalize_submission` also refuses outright (fails closed) if a
round is `live` and no `lock_guard` was supplied -- an ordinary submission
source can never silently skip position-level enforcement just because it
forgot to pass one. `"review"` and `"final"` remain outside
`ORDINARY_SUBMISSION_ALLOWED_STATES` entirely: ordinary submission stays
closed there regardless of any lock_guard.
"""

import json
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.audit import ENTITY_TYPE_LINEUP, LINEUP_CORRECTED, LINEUP_SUBMITTED, ActorContext, append_event
from app.db import _for_update_suffix, transaction
from app.season import _now

POSITIONS = ("F1", "F2", "F3", "M1", "M2", "M3", "Ruck", "Tackler", "Interchange")
# "scorer_correction" (issue #137) is deliberately distinct from
# "scorer_proxy": a proxy submission still goes through the ordinary
# lock_guard rejection like a coach's own submission, while a correction is
# the one path an authorised operator uses specifically to override an
# already-locked position -- see `submit_correction` below.
SUBMISSION_SOURCES = frozenset({"coach", "scorer_proxy", "carry_forward", "system_derived", "scorer_correction"})
CORRECTION_SOURCE_TYPE = "scorer_correction"
# Ordinary submission (coach, scorer/admin proxy, carry-forward) is
# permitted while the round is "open" (before any AFL match has started) or
# "live" (issue #144: at least one AFL match has started, but staged
# position-level lockout -- app/lockouts.py -- may still leave most
# positions individually editable). Every "live" submission still passes
# through the caller-supplied `lock_guard` exactly as it always has; this
# frozenset only ever widens *which rounds* an ordinary submission attempt
# is in scope for, never which positions within one are legal to change.
# "review" and "final" are deliberately excluded: ordinary submission stays
# closed once a round moves past ordinary play, regardless of lock_guard.
ORDINARY_SUBMISSION_ALLOWED_STATES = frozenset({"open", "live"})
# A correction is permitted for any round state an ordinary submission is
# not yet frozen out of publication for -- "open", "live" (main/selective
# lockout already active) and "review" (calculated but not yet signed off).
# "final" is deliberately excluded: post-publication correction is the
# separate app.round_review.attempt_correction workflow (issue #137).
CORRECTION_ALLOWED_STATES = frozenset({"open", "live", "review"})
# Whole-draft (not per-position) origin of the *current* draft revision --
# see migrations/versions/0018_proxy_draft_source.py's docstring and
# `submit`'s use of it below.
DRAFT_SOURCES = frozenset({"coach", "scorer_proxy"})


class LineupConflictError(RuntimeError):
    """The caller based a write on a revision which is no longer current."""


class LineupIntegrityError(ValueError):
    pass


class RoundPublishedError(LineupIntegrityError):
    """A locked-lineup correction was attempted against a round that has
    already reached final publication (issue #137). This workflow never
    edits published official history -- see
    `app.round_review.attempt_correction`, the separate official-result
    correction boundary, instead."""


class NoEffectiveSubmissionError(LineupIntegrityError):
    """A correction was attempted against a lineup with no effective
    submitted version yet -- there is nothing to correct."""


class NoOpCorrectionError(LineupIntegrityError):
    """A correction's proposed positions are identical to the current
    effective submission -- there is nothing to record."""


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


@dataclass(frozen=True)
class CorrectionSlotChange:
    """One position's before/after and, if it was locked at the time of
    correction, the original lock evidence copied read-only from
    `weekly_lineup_lock` -- never a recomputation, and never written back
    to that immutable table (issue #137's "corrected lock provenance")."""

    position: str
    previous_season_player_id: str | None
    corrected_season_player_id: str | None
    was_locked: bool
    lock_reason: str | None
    afl_match_id: int | None
    effective_lock_at: str | None
    observed_status: str | None
    locked_at: str | None


@dataclass(frozen=True)
class LineupCorrection:
    """The full audited record of one authorised locked-lineup correction
    (issue #137): which submission version it superseded and which new one
    became effective, who did it under which role and why, and the
    per-position provenance of exactly what changed."""

    correction_id: str
    lineup_id: str
    bbbffl_round_id: str
    season_entry_id: str
    from_version: int
    to_version: int
    actor_type: str
    actor_id: str | None
    actor_role: str | None
    reason: str
    created_at: str
    slots: tuple[CorrectionSlotChange, ...]


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
        if source_type == CORRECTION_SOURCE_TYPE:
            raise LineupIntegrityError(
                "'scorer_correction' is reserved for submit_correction(), which alone records the "
                "required correction provenance -- it cannot be used with this ordinary submission path"
            )
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
        if source_type == CORRECTION_SOURCE_TYPE:
            raise LineupIntegrityError(
                "'scorer_correction' is reserved for submit_correction(), which alone records the "
                "required correction provenance -- it cannot be used with this ordinary submission path"
            )
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
        allowed_states=ORDINARY_SUBMISSION_ALLOWED_STATES,
        require_lock_guard_when_live=True,
        correlation_id=None,
    ):
        lineup_id = lineup["lineup_id"]
        current = lineup["effective_submission_version"] or 0
        if current != expected_submission_version:
            raise LineupConflictError("stale submission version")
        lifecycle = conn.execute(
            "SELECT state FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?" + _for_update_suffix(self.database),
            (lineup["bbbffl_round_id"],),
        ).fetchone()
        if not lifecycle or lifecycle["state"] not in allowed_states:
            if lifecycle and lifecycle["state"] == "final" and "final" not in allowed_states:
                # issue #137: a published round is never edited through this
                # ordinary/correction submission path -- see
                # app.round_review.attempt_correction, the separate
                # official-result correction boundary.
                raise RoundPublishedError(
                    "BBBFFL round has already reached final publication; use the official-result "
                    "correction workflow (app.round_review.attempt_correction) instead"
                )
            raise LineupIntegrityError("BBBFFL round does not currently permit submission")
        if lifecycle["state"] == "live" and require_lock_guard_when_live and lock_guard is None:
            # issue #144: "live" is never itself a global submission lock,
            # but the only thing that keeps it from becoming one in practice
            # is `lock_guard` actually running on every submission attempt.
            # An ordinary submission source that omitted one while the round
            # is live would otherwise be able to rewrite every position --
            # including ones an activated trigger has already locked -- with
            # no enforcement at all. Fail closed rather than trust the
            # caller silently opted out of position-level enforcement.
            raise LineupIntegrityError(
                "a submission while the BBBFFL round is live requires an active position-level lock "
                "guard (see app.lockouts.LockoutRepository.guard); none was supplied"
            )
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
            correlation_id=correlation_id,
            reason=reason,
            after_state={"effective_submission_version": version},
            payload={"source_type": source_type, "based_on_draft_revision": based_on_draft_revision},
        )
        return version

    def submit_correction(
        self,
        lineup_id,
        positions,
        *,
        expected_submission_version,
        actor,
        reason,
        lock_guard=None,
    ) -> LineupCorrection:
        """Authorised Scorer/Admin/Replay-Operator correction of an
        authoritative weekly lineup after a selective or main lockout has
        already activated (issue #137).

        `positions` is the complete, atomic corrected position map -- the
        same full nine-position contract `submit`/`submit_positions` use, so
        a direct swap (e.g. Tackler <-> Interchange) is validated against
        the single final proposed state, never an invalid intermediate
        duplicate-player state. Callers that only want to change one or two
        slots (the common case) merge their change onto the lineup's
        current effective positions before calling this -- see
        `app.lineup_correction.LineupCorrectionService.correct`, which is
        the authorised, reason-checked entry point most callers should use
        instead of this method directly.

        `lock_guard`, if given and exposing a `.materialize(lineup_id)`
        method, is called *before* this opens its own transaction -- exactly
        the same pre-transaction step `submit`/`submit_positions` already
        take (see this module's docstring and app.lockouts's docstring for
        why materialization must happen outside, and before, the write
        transaction). This is the only use this method ever makes of
        `lock_guard`: its rejecting `__call__` behaviour is never invoked
        (see below). Without this, a correction that is the very first
        lineup operation since a trigger activated -- lock evidence is
        materialized lazily, by `lock_state`/an ordinary submission attempt,
        never eagerly -- could read `weekly_lineup_lock` before any row for
        the affected position exists yet, and wrongly record `was_locked=
        False` with no trigger/match/instant provenance even though the
        position is, in fact, already governed by an activated trigger.
        `app.lineup_correction.LineupCorrectionService.correct` always
        supplies one.

        Unlike `submit`/`submit_positions`, this:

        - is permitted for any round state in `CORRECTION_ALLOWED_STATES`
          ("open", "live", "review"), not just "open" -- the entire point is
          correcting a lineup *after* a lockout has activated, which by
          definition means the round has moved past "open". A round that
          has already reached "final" publication raises
          `RoundPublishedError` instead (see `_finalize_submission`); this
          workflow never edits published official history.
        - never invokes `lock_guard` as a rejecting callable -- an
          authorised correction is exactly the one path permitted to
          override an already-locked position. Ordinary
          `app.lockouts.LockGuard` rejection remains fully intact for every
          other submission source (`submit`/`submit_positions` with any
          `source_type` other than `"scorer_correction"`, which this method
          is the only caller of).
        - never touches `weekly_lineup_lock` (immutable, untouched) but
          copies each corrected position's existing lock evidence, if any,
          read-only into a new `weekly_lineup_correction`/
          `weekly_lineup_correction_slot` audit record in the same
          transaction as the new submission version -- see this module's
          docstring in migrations/versions/0025_lineup_correction.py.

        Raises `NoEffectiveSubmissionError` if the lineup has never been
        submitted (nothing to correct), and `NoOpCorrectionError` if
        `positions` is identical to the current effective submission (a
        correction, unlike an ordinary resubmission, must change something
        -- issue #137's atomic position map exists specifically to record a
        deliberate slot change, not a no-op replay of the same content).
        """
        if not reason or not reason.strip():
            raise LineupIntegrityError("a locked-lineup correction requires a substantive reason")
        positions = self._normalise(positions)
        if lock_guard is not None and hasattr(lock_guard, "materialize"):
            # Runs in its own standalone transaction, deliberately *before*
            # this method opens its own below -- see the module docstring
            # and app.lockouts's docstring for why a lock observed here must
            # be durably recorded independently of whatever this correction
            # goes on to do, and so that the read of `weekly_lineup_lock`
            # below always reflects the lineup's *current* effective lock
            # state rather than whatever a caller last happened to trigger.
            lock_guard.materialize(lineup_id)
        correlation_id = str(uuid4())
        with transaction(self.database) as conn:
            lineup = self._lock_lineup_row(conn, lineup_id)
            lineup_id = lineup["lineup_id"]
            from_version = lineup["effective_submission_version"] or 0
            if from_version == 0:
                raise NoEffectiveSubmissionError(
                    f"lineup {lineup_id} has no effective submitted version yet; there is nothing to correct"
                )
            previous_rows = conn.execute(
                "SELECT position, season_player_id FROM weekly_lineup_submission_slot WHERE lineup_id=? AND version=?",
                (lineup_id, from_version),
            ).fetchall()
            previous_positions = {row["position"]: row["season_player_id"] for row in previous_rows}
            changed_positions = [p for p in POSITIONS if previous_positions.get(p) != positions.get(p)]
            if not changed_positions:
                raise NoOpCorrectionError("a correction must change at least one position")
            # Opening Round deferred slots retain their own separate audited
            # correction workflow (app.opening_round.OpeningRoundNominationRepository.
            # correct) and must never be reachable through this one -- the
            # same query `OpeningRoundSelectionGuard` uses inside
            # `submit`/`submit_positions`, applied here explicitly since this
            # method deliberately bypasses `lock_guard` (which is how that
            # guard is ordinarily plugged in).
            deferred_positions = {
                row["position"]: row["season_player_id"]
                for row in conn.execute(
                    "SELECT position, season_player_id FROM opening_round_nomination "
                    "WHERE bbbffl_round_id=? AND season_entry_id=?",
                    (lineup["bbbffl_round_id"], lineup["season_entry_id"]),
                ).fetchall()
            }
            deferred_changed = [p for p in changed_positions if p in deferred_positions]
            if deferred_changed:
                raise LineupIntegrityError(
                    f"positions {deferred_changed} are Opening Round deferred slots and cannot be changed "
                    "through this correction workflow; use the Opening Round nomination correction workflow instead"
                )
            existing_locks = {
                row["position"]: row
                for row in conn.execute(
                    "SELECT position, season_player_id, afl_match_id, observed_status, effective_lock_at, "
                    "lock_reason, locked_at FROM weekly_lineup_lock WHERE lineup_id=?",
                    (lineup_id,),
                ).fetchall()
            }
            to_version = self._finalize_submission(
                conn,
                lineup,
                positions,
                based_on_draft_revision=lineup["draft_revision"],
                expected_submission_version=expected_submission_version,
                actor=actor,
                source_type=CORRECTION_SOURCE_TYPE,
                source_detail=None,
                reason=reason,
                lock_guard=None,
                allowed_states=CORRECTION_ALLOWED_STATES,
                require_lock_guard_when_live=False,
                correlation_id=correlation_id,
            )
            correction_id, now = str(uuid4()), _now()
            conn.execute(
                "INSERT INTO weekly_lineup_correction VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    correction_id,
                    lineup_id,
                    lineup["bbbffl_round_id"],
                    lineup["season_entry_id"],
                    from_version,
                    to_version,
                    actor.actor_type,
                    actor.actor_id,
                    actor.actor_role,
                    reason,
                    now,
                ),
            )
            slots = []
            # Alphabetical, matching `_to_correction`'s `ORDER BY position`
            # read-back -- so a freshly-returned `LineupCorrection` compares
            # equal to one re-read via `get_correction`/`list_corrections`.
            for position in sorted(changed_positions):
                lock_row = existing_locks.get(position)
                # `weekly_lineup_lock`'s PK is `(lineup_id, position)`: at
                # most one lock instance can ever be materialized for a
                # given position, durably marking that position as having
                # been governed by an activated trigger. A *second*
                # correction of the same position must still carry that
                # provenance forward even though the position's occupant at
                # correction time (a prior correction's own result) no
                # longer matches the row's original `season_player_id` --
                # comparing occupancy here would silently lose the lock
                # link on any correction after the first.
                was_locked = lock_row is not None
                conn.execute(
                    "INSERT INTO weekly_lineup_correction_slot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        correction_id,
                        position,
                        previous_positions.get(position),
                        positions.get(position),
                        int(was_locked),
                        lock_row["lock_reason"] if was_locked else None,
                        lock_row["afl_match_id"] if was_locked else None,
                        lock_row["effective_lock_at"] if was_locked else None,
                        lock_row["observed_status"] if was_locked else None,
                        lock_row["locked_at"] if was_locked else None,
                    ),
                )
                slots.append(
                    CorrectionSlotChange(
                        position,
                        previous_positions.get(position),
                        positions.get(position),
                        was_locked,
                        lock_row["lock_reason"] if was_locked else None,
                        lock_row["afl_match_id"] if was_locked else None,
                        lock_row["effective_lock_at"] if was_locked else None,
                        lock_row["observed_status"] if was_locked else None,
                        lock_row["locked_at"] if was_locked else None,
                    )
                )
            invalidated_positions = self._invalidate_stale_review_state(
                conn, lineup["bbbffl_round_id"], lineup["season_entry_id"], changed_positions
            )
            append_event(
                conn,
                actor=actor,
                action=LINEUP_CORRECTED,
                entity_type=ENTITY_TYPE_LINEUP,
                entity_id=lineup_id,
                entity_version=str(to_version),
                correlation_id=correlation_id,
                reason=reason,
                before_state={"version": from_version, "positions": previous_positions},
                after_state={"version": to_version, "positions": positions},
                payload={
                    "correction_id": correction_id,
                    "locked_positions_overridden": [slot.position for slot in slots if slot.was_locked],
                    "invalidated_review_state_for_positions": invalidated_positions,
                },
            )
        return LineupCorrection(
            correction_id,
            lineup_id,
            lineup["bbbffl_round_id"],
            lineup["season_entry_id"],
            from_version,
            to_version,
            actor.actor_type,
            actor.actor_id,
            actor.actor_role,
            reason,
            now,
            tuple(slots),
        )

    @staticmethod
    def _invalidate_stale_review_state(conn, bbbffl_round_id: str, season_entry_id: str, changed_positions) -> list:
        """Two correctness gaps closed together, both found by review of
        this correction feature:

        1. `app.round_review`'s per-matchup `review_version` is the CAS
           `CompetitionLifecycleRepository.publish_results`/`attempt_signoff`
           actually re-check inside their own locked transaction (see
           `app.round_review`'s "Atomicity and concurrency"). A correction
           changes what a matchup's calculated score is derived from just as
           much as a DNP ruling/override does, but previously never bumped
           it -- so a correction landing between `build_round_review` and
           `publish_results`'s row lock could still let a stale,
           pre-correction score be published, even though this module's own
           lineup-version staleness check (see `app.round_review.
           build_matchup_review`) would have blocked a *fresh* review. Every
           matchup this entry participates in for this round has its
           `review_version` bumped here, in the same transaction as the
           correction itself, so any in-flight sign-off/correction attempt
           built from the pre-correction revision fails closed
           (`StaleRoundVersionError`) instead of racing it.
        2. `bbbffl_matchup_slot_ruling`/`bbbffl_matchup_interchange_ruling`/
           `bbbffl_matchup_override` are keyed by `(matchup_id,
           season_entry_id, slot/position)`, never by player -- a DNP ruling
           or override recorded against the *pre-correction* occupant of a
           position would otherwise silently keep applying to whichever
           player the correction just installed there instead, with nothing
           in `app.round_review`'s lineup-version staleness check catching
           it (that check compares lineup *versions*, not per-slot rulings).
           Any slot ruling/override for a position this correction actually
           changed is deleted here -- the scorer must re-decide DNP/
           interchange/override for the corrected occupant, exactly as if
           reviewing this position for the first time. Every prior ruling/
           override's own history remains fully inspectable via
           `app.audit.AuditEventRepository` regardless (`SLOT_RULING_
           RECORDED`/`INTERCHANGE_RULING_RECORDED`/`OVERRIDE_RECORDED`); only
           the current-decision pointer row is cleared, exactly like
           clearing an override via `RoundReviewRepository.record_override(
           override_score=None)` already does.

        Raw SQL against `app.round_review`'s tables, not an import of that
        module -- `app.round_review` sits *above* the season model
        (app.lineups), so the reverse dependency this method would need if
        it called into `app.round_review` directly is architecturally
        disallowed (see tests/test_architecture.py); this mirrors how this
        method already reaches into `weekly_lineup_lock`/
        `opening_round_nomination` by table name rather than by import. A
        round with no persisted matchups yet (or no rulings/overrides at
        all -- the overwhelming common case) is a safe no-op.

        Returns the list of positions any ruling/override/interchange
        ruling was actually invalidated for, for the correction's own audit
        payload.
        """
        matchups = conn.execute(
            "SELECT matchup_id FROM bbbffl_matchup WHERE bbbffl_round_id=? "
            "AND (home_season_entry_id=? OR away_season_entry_id=?)",
            (bbbffl_round_id, season_entry_id, season_entry_id),
        ).fetchall()
        invalidated: set = set()
        for matchup in matchups:
            matchup_id = matchup["matchup_id"]
            conn.execute(
                "UPDATE bbbffl_matchup SET review_version = review_version + 1 WHERE matchup_id=?", (matchup_id,)
            )
            for position in changed_positions:
                ruling_result = conn.execute(
                    "DELETE FROM bbbffl_matchup_slot_ruling WHERE matchup_id=? AND season_entry_id=? AND slot=?",
                    (matchup_id, season_entry_id, position),
                )
                override_result = conn.execute(
                    "DELETE FROM bbbffl_matchup_override WHERE matchup_id=? AND season_entry_id=? AND position=?",
                    (matchup_id, season_entry_id, position),
                )
                if ruling_result.rowcount or override_result.rowcount:
                    invalidated.add(position)
            interchange_row = conn.execute(
                "SELECT target_position FROM bbbffl_matchup_interchange_ruling WHERE matchup_id=? AND season_entry_id=?",
                (matchup_id, season_entry_id),
            ).fetchone()
            if interchange_row is not None and (
                "Interchange" in changed_positions or interchange_row["target_position"] in changed_positions
            ):
                conn.execute(
                    "DELETE FROM bbbffl_matchup_interchange_ruling WHERE matchup_id=? AND season_entry_id=?",
                    (matchup_id, season_entry_id),
                )
                invalidated.add("Interchange")
        return sorted(invalidated)

    def get_correction(self, correction_id: str) -> LineupCorrection | None:
        row = self.database.execute(
            "SELECT * FROM weekly_lineup_correction WHERE correction_id=?", (correction_id,)
        ).fetchone()
        if not row:
            return None
        return self._to_correction(row)

    def list_corrections(self, lineup_id: str) -> list[LineupCorrection]:
        """Every correction ever recorded against this lineup, oldest
        first -- the full audited history issue #137 requires scorer
        review/lineup inspection surfaces to display."""
        rows = self.database.execute(
            "SELECT * FROM weekly_lineup_correction WHERE lineup_id=? ORDER BY to_version", (lineup_id,)
        ).fetchall()
        return [self._to_correction(row) for row in rows]

    def _to_correction(self, row) -> LineupCorrection:
        slot_rows = self.database.execute(
            "SELECT * FROM weekly_lineup_correction_slot WHERE correction_id=? ORDER BY position",
            (row["correction_id"],),
        ).fetchall()
        slots = tuple(
            CorrectionSlotChange(
                s["position"],
                s["previous_season_player_id"],
                s["corrected_season_player_id"],
                bool(s["was_locked"]),
                s["lock_reason"],
                s["afl_match_id"],
                s["effective_lock_at"],
                s["observed_status"],
                s["locked_at"],
            )
            for s in slot_rows
        )
        return LineupCorrection(
            row["correction_id"],
            row["lineup_id"],
            row["bbbffl_round_id"],
            row["season_entry_id"],
            row["from_version"],
            row["to_version"],
            row["actor_type"],
            row["actor_id"],
            row["actor_role"],
            row["reason"],
            row["created_at"],
            slots,
        )

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
