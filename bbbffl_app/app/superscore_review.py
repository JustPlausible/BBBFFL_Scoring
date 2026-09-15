"""Issue #192 gap #2: entry-scoped DNP/Interchange/override ruling boundary
for SuperScore.

`app.round_review.RoundReviewRepository`'s `record_dnp_ruling`/
`record_interchange_ruling`/`record_override` are keyed by `matchup_id`, and
SuperScore deliberately has no `bbbffl_matchup` rows at all (there is no
head-to-head pairing -- see docs/2026-finals-superscore-design.md's "A
genuine SuperScore-specific abstraction: leaderboard results, not
matchups"). This module is the entry-scoped counterpart: ruling rows keyed
by `(bbbffl_round_id, season_entry_id, slot)`, following `app.round_review`'s
own validation/CAS-versioning/audit conventions adapted to that key shape --
never a stream-scoped call into the existing matchup-keyed module, and never
a weakening of its own matchup-participant validation (`app.round_review`
itself is untouched).

The CAS/lock target is the entry's `superscore_entry_review_state` row
(gap #4, created during round setup by `app.superscore_round`) -- **not** a
counter on the optional, derived entry-scoped calculation row #193 will add.
Every ruling here locks and advances that row's `review_version` in the same
transaction as its own write; a failed write advances nothing.

`app.participation.assess_participation` remains the pure, stateless
evidence classifier a caller (a future Scorer UI, #193's entry-scoped
calculation service) uses to decide *what* ruling to record; this module
only ever persists the ruling decision itself, never computes a score.
"""

from dataclasses import dataclass

from app.audit import ActorContext, append_event
from app.db import _for_update_suffix, transaction
from app.lineups import POSITIONS as SLOTS
from app.participation import ParticipationEvidence, assess_participation
from app.season import SeasonCompletedError, SeasonRepository, _now

OVERRIDE_POSITIONS = tuple(slot for slot in SLOTS if slot != "Interchange")


def recommend_dnp_evidence(*, afl_team_id, bye_team_ids, match, stat_line) -> ParticipationEvidence:
    """Thin pass-through to `app.participation.assess_participation` -- the
    same pure, stateless evidence classifier the ordinary/finals review
    surface (`app.calculations`) already uses -- so a Scorer deciding a
    SuperScore entry-scoped DNP ruling sees the identical evidence
    classification, never a second, SuperScore-specific reimplementation of
    club-bye/zero-stats/unknown-participation evidence."""
    return assess_participation(afl_team_id=afl_team_id, bye_team_ids=bye_team_ids, match=match, stat_line=stat_line)


AUTHORISED_OVERRIDE_ROLES = frozenset({"scorer", "replay_operator", "admin"})

SLOT_RULING_RECORDED = "superscore.review.dnp_ruling.recorded"
INTERCHANGE_RULING_RECORDED = "superscore.review.interchange_ruling.recorded"
OVERRIDE_RECORDED = "superscore.review.override.recorded"
ENTITY_TYPE_SLOT_RULING = "superscore.review.slot_ruling"
ENTITY_TYPE_INTERCHANGE_RULING = "superscore.review.interchange_ruling"
ENTITY_TYPE_OVERRIDE = "superscore.review.override"


# Issue #208 (Codex review): the same shared completed-season write-fence
# error `app.superscore_results`/`app.finals_review`/`app.calculations`
# already raise (`app.season.SeasonRepository.guard_writable`), re-exported
# under this name so `app/routes/superscore_review.py` can translate it to
# HTTP 423 -- exactly like `app.superscore_results.CompletedSeasonError`
# already does for the calculate/publish routes -- without importing
# `app.season` directly from the routes layer (see tests/test_architecture.py's
# `test_routes_never_import_persistence_or_season_model_directly`).
CompletedSeasonError = SeasonCompletedError


class SuperScoreReviewError(Exception):
    """Base class for this module's domain errors."""


class UnknownReviewStateError(SuperScoreReviewError):
    """No `superscore_entry_review_state` row exists for this round/entry --
    `app.superscore_round.setup_round` must run (and succeed) first."""


class StaleReviewVersionError(SuperScoreReviewError):
    """A ruling was submitted against a `review_version` that is no longer
    current -- checked atomically under the same row lock that advances it,
    mirroring `app.competition_lifecycle.StaleRoundVersionError`."""


class InvalidSlotError(SuperScoreReviewError):
    pass


class InvalidOverridePositionError(SuperScoreReviewError):
    pass


class MissingOverrideReasonError(SuperScoreReviewError):
    pass


class UnauthorisedActorError(SuperScoreReviewError):
    pass


@dataclass(frozen=True)
class SlotRuling:
    season_entry_id: str
    slot: str
    dnp: bool
    decided_by: str | None
    decided_by_role: str | None
    decided_at: str
    reason: str | None


@dataclass(frozen=True)
class InterchangeRuling:
    season_entry_id: str
    target_position: str | None
    decided_by: str | None
    decided_by_role: str | None
    decided_at: str
    reason: str | None


@dataclass(frozen=True)
class Override:
    season_entry_id: str
    position: str
    override_score: float
    calculated_score: float | None
    reason: str
    decided_by: str | None
    decided_by_role: str | None
    decided_at: str


class SuperScoreReviewRepository:
    """CRUD for entry-scoped SuperScore rulings/overrides -- the SuperScore
    counterpart of `app.round_review.RoundReviewRepository`, keyed by
    `(bbbffl_round_id, season_entry_id, ...)` instead of `matchup_id`. Every
    write locks and advances `superscore_entry_review_state.review_version`
    in the same transaction as its own domain write and audit event."""

    def __init__(self, database):
        self.database = database

    def _guard_season_writable(self, conn, bbbffl_round_id):
        """Issue #194 (Codex review, P1): every write in this repository
        changes review state a published leaderboard's score depends on,
        so it must take the same completed-season write fence
        `app.superscore_results`/`app.finals_review`/`app.calculations`
        already take -- locked first, before any review-state row lock
        below, exactly mirroring `SuperScoreCalculationService.
        _calculate_entries`'s identical rationale. Without this, review
        state remained operator-mutable after `complete_season`, silently
        invalidating the archival guard's own assumption (`app.
        season_archival`) that no result-affecting writer remains once a
        season reads `completed`."""
        season_lookup = conn.execute(
            "SELECT season_id FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?", (bbbffl_round_id,)
        ).fetchone()
        if season_lookup is None:
            raise UnknownReviewStateError(f"no round lifecycle for round {bbbffl_round_id}")
        SeasonRepository(self.database).guard_writable(conn, season_lookup["season_id"])

    def _locked_review_state(self, conn, bbbffl_round_id, season_entry_id, expected_review_version):
        row = conn.execute(
            "SELECT review_version FROM superscore_entry_review_state WHERE bbbffl_round_id=? AND season_entry_id=?"
            + _for_update_suffix(self.database),
            (bbbffl_round_id, season_entry_id),
        ).fetchone()
        if row is None:
            raise UnknownReviewStateError(
                f"no superscore_entry_review_state row for round {bbbffl_round_id}, entry {season_entry_id}; "
                "round setup (app.superscore_round.setup_round) must run first"
            )
        if expected_review_version is not None and row["review_version"] != expected_review_version:
            raise StaleReviewVersionError(
                f"round {bbbffl_round_id} entry {season_entry_id} review is at version {row['review_version']}, "
                f"not the expected {expected_review_version}"
            )
        return row

    def record_dnp_ruling(
        self,
        bbbffl_round_id,
        season_entry_id,
        slot,
        dnp,
        *,
        expected_review_version,
        actor: ActorContext,
        reason: str | None = None,
    ) -> int:
        if slot not in SLOTS:
            raise InvalidSlotError(f"Unknown slot: {slot}")
        with transaction(self.database) as conn:
            self._guard_season_writable(conn, bbbffl_round_id)
            state = self._locked_review_state(conn, bbbffl_round_id, season_entry_id, expected_review_version)
            existing = conn.execute(
                "SELECT dnp FROM superscore_entry_slot_ruling WHERE bbbffl_round_id=? AND season_entry_id=? AND slot=?",
                (bbbffl_round_id, season_entry_id, slot),
            ).fetchone()
            before = {"dnp": bool(existing["dnp"])} if existing is not None else {"dnp": None}
            now = _now()
            conn.execute(
                """
                INSERT INTO superscore_entry_slot_ruling
                    (bbbffl_round_id, season_entry_id, slot, dnp, decided_by_type, decided_by, decided_by_role, decided_at, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bbbffl_round_id, season_entry_id, slot) DO UPDATE SET
                    dnp=excluded.dnp, decided_by_type=excluded.decided_by_type, decided_by=excluded.decided_by,
                    decided_by_role=excluded.decided_by_role, decided_at=excluded.decided_at, reason=excluded.reason
                """,
                (
                    bbbffl_round_id,
                    season_entry_id,
                    slot,
                    int(dnp),
                    actor.actor_type,
                    actor.actor_id,
                    actor.actor_role,
                    now,
                    reason,
                ),
            )
            new_version = state["review_version"] + 1
            conn.execute(
                "UPDATE superscore_entry_review_state SET review_version=?, updated_at=? "
                "WHERE bbbffl_round_id=? AND season_entry_id=?",
                (new_version, now, bbbffl_round_id, season_entry_id),
            )
            append_event(
                conn,
                actor=actor,
                action=SLOT_RULING_RECORDED,
                entity_type=ENTITY_TYPE_SLOT_RULING,
                entity_id=f"{bbbffl_round_id}:{season_entry_id}:{slot}",
                entity_version=str(new_version),
                reason=reason,
                before_state=before,
                after_state={"dnp": dnp},
                payload={"bbbffl_round_id": bbbffl_round_id, "season_entry_id": season_entry_id, "slot": slot},
            )
        return new_version

    def record_interchange_ruling(
        self,
        bbbffl_round_id,
        season_entry_id,
        target_position,
        *,
        expected_review_version,
        actor: ActorContext,
        reason: str | None = None,
    ) -> int:
        if target_position is not None and target_position not in OVERRIDE_POSITIONS:
            raise InvalidSlotError(f"Invalid target_position: {target_position}")
        with transaction(self.database) as conn:
            self._guard_season_writable(conn, bbbffl_round_id)
            state = self._locked_review_state(conn, bbbffl_round_id, season_entry_id, expected_review_version)
            existing = conn.execute(
                "SELECT target_position FROM superscore_entry_interchange_ruling "
                "WHERE bbbffl_round_id=? AND season_entry_id=?",
                (bbbffl_round_id, season_entry_id),
            ).fetchone()
            before = {"target_position": existing["target_position"] if existing is not None else None}
            now = _now()
            conn.execute(
                """
                INSERT INTO superscore_entry_interchange_ruling
                    (bbbffl_round_id, season_entry_id, target_position, decided_by_type, decided_by, decided_by_role, decided_at, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bbbffl_round_id, season_entry_id) DO UPDATE SET
                    target_position=excluded.target_position, decided_by_type=excluded.decided_by_type,
                    decided_by=excluded.decided_by, decided_by_role=excluded.decided_by_role,
                    decided_at=excluded.decided_at, reason=excluded.reason
                """,
                (
                    bbbffl_round_id,
                    season_entry_id,
                    target_position,
                    actor.actor_type,
                    actor.actor_id,
                    actor.actor_role,
                    now,
                    reason,
                ),
            )
            new_version = state["review_version"] + 1
            conn.execute(
                "UPDATE superscore_entry_review_state SET review_version=?, updated_at=? "
                "WHERE bbbffl_round_id=? AND season_entry_id=?",
                (new_version, now, bbbffl_round_id, season_entry_id),
            )
            append_event(
                conn,
                actor=actor,
                action=INTERCHANGE_RULING_RECORDED,
                entity_type=ENTITY_TYPE_INTERCHANGE_RULING,
                entity_id=f"{bbbffl_round_id}:{season_entry_id}",
                entity_version=str(new_version),
                reason=reason,
                before_state=before,
                after_state={"target_position": target_position},
                payload={"bbbffl_round_id": bbbffl_round_id, "season_entry_id": season_entry_id},
            )
        return new_version

    def record_override(
        self,
        bbbffl_round_id,
        season_entry_id,
        position,
        override_score,
        calculated_score,
        reason: str | None,
        *,
        expected_review_version,
        actor: ActorContext,
    ) -> int:
        """Set (`override_score` not None) or clear (`override_score` None)
        a manual score override -- setting one always requires an
        authorised actor and an explicit reason, exactly like
        `app.round_review.RoundReviewRepository.record_override`."""
        if position not in OVERRIDE_POSITIONS:
            raise InvalidOverridePositionError(f"Invalid position: {position}")
        if override_score is not None:
            if actor.actor_role not in AUTHORISED_OVERRIDE_ROLES:
                raise UnauthorisedActorError(
                    f"actor_role {actor.actor_role!r} is not authorised to record a manual override"
                )
            if not reason:
                raise MissingOverrideReasonError("a manual override requires an explicit reason")
        with transaction(self.database) as conn:
            self._guard_season_writable(conn, bbbffl_round_id)
            state = self._locked_review_state(conn, bbbffl_round_id, season_entry_id, expected_review_version)
            existing = conn.execute(
                "SELECT override_score, reason FROM superscore_entry_override "
                "WHERE bbbffl_round_id=? AND season_entry_id=? AND position=?",
                (bbbffl_round_id, season_entry_id, position),
            ).fetchone()
            before = (
                {"override_score": float(existing["override_score"]), "reason": existing["reason"]}
                if existing is not None
                else {"override_score": None, "reason": None}
            )
            now = _now()
            if override_score is None:
                conn.execute(
                    "DELETE FROM superscore_entry_override WHERE bbbffl_round_id=? AND season_entry_id=? AND position=?",
                    (bbbffl_round_id, season_entry_id, position),
                )
                after = {"override_score": None, "reason": None}
            else:
                conn.execute(
                    """
                    INSERT INTO superscore_entry_override
                        (bbbffl_round_id, season_entry_id, position, override_score, calculated_score, reason,
                         decided_by_type, decided_by, decided_by_role, decided_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(bbbffl_round_id, season_entry_id, position) DO UPDATE SET
                        override_score=excluded.override_score, calculated_score=excluded.calculated_score,
                        reason=excluded.reason, decided_by_type=excluded.decided_by_type,
                        decided_by=excluded.decided_by, decided_by_role=excluded.decided_by_role,
                        decided_at=excluded.decided_at
                    """,
                    (
                        bbbffl_round_id,
                        season_entry_id,
                        position,
                        override_score,
                        calculated_score,
                        reason,
                        actor.actor_type,
                        actor.actor_id,
                        actor.actor_role,
                        now,
                    ),
                )
                after = {"override_score": override_score, "reason": reason}
            new_version = state["review_version"] + 1
            conn.execute(
                "UPDATE superscore_entry_review_state SET review_version=?, updated_at=? "
                "WHERE bbbffl_round_id=? AND season_entry_id=?",
                (new_version, now, bbbffl_round_id, season_entry_id),
            )
            append_event(
                conn,
                actor=actor,
                action=OVERRIDE_RECORDED,
                entity_type=ENTITY_TYPE_OVERRIDE,
                entity_id=f"{bbbffl_round_id}:{season_entry_id}:{position}",
                entity_version=str(new_version),
                reason=reason,
                before_state=before,
                after_state=after,
                payload={
                    "bbbffl_round_id": bbbffl_round_id,
                    "season_entry_id": season_entry_id,
                    "position": position,
                    "calculated_score": calculated_score,
                },
            )
        return new_version

    def get_slot_rulings(self, bbbffl_round_id, season_entry_id) -> dict[str, SlotRuling]:
        rows = self.database.execute(
            "SELECT * FROM superscore_entry_slot_ruling WHERE bbbffl_round_id=? AND season_entry_id=?",
            (bbbffl_round_id, season_entry_id),
        ).fetchall()
        return {
            row["slot"]: SlotRuling(
                season_entry_id=row["season_entry_id"],
                slot=row["slot"],
                dnp=bool(row["dnp"]),
                decided_by=row["decided_by"],
                decided_by_role=row["decided_by_role"],
                decided_at=row["decided_at"],
                reason=row["reason"],
            )
            for row in rows
        }

    def get_interchange_ruling(self, bbbffl_round_id, season_entry_id) -> InterchangeRuling | None:
        row = self.database.execute(
            "SELECT * FROM superscore_entry_interchange_ruling WHERE bbbffl_round_id=? AND season_entry_id=?",
            (bbbffl_round_id, season_entry_id),
        ).fetchone()
        if row is None:
            return None
        return InterchangeRuling(
            season_entry_id=row["season_entry_id"],
            target_position=row["target_position"],
            decided_by=row["decided_by"],
            decided_by_role=row["decided_by_role"],
            decided_at=row["decided_at"],
            reason=row["reason"],
        )

    def get_overrides(self, bbbffl_round_id, season_entry_id) -> dict[str, Override]:
        rows = self.database.execute(
            "SELECT * FROM superscore_entry_override WHERE bbbffl_round_id=? AND season_entry_id=?",
            (bbbffl_round_id, season_entry_id),
        ).fetchall()
        return {
            row["position"]: Override(
                season_entry_id=row["season_entry_id"],
                position=row["position"],
                override_score=float(row["override_score"]),
                calculated_score=(float(row["calculated_score"]) if row["calculated_score"] is not None else None),
                reason=row["reason"],
                decided_by=row["decided_by"],
                decided_by_role=row["decided_by_role"],
                decided_at=row["decided_at"],
            )
            for row in rows
        }

    def get_review_version(self, bbbffl_round_id, season_entry_id) -> int | None:
        row = self.database.execute(
            "SELECT review_version FROM superscore_entry_review_state WHERE bbbffl_round_id=? AND season_entry_id=?",
            (bbbffl_round_id, season_entry_id),
        ).fetchone()
        return row["review_version"] if row else None
