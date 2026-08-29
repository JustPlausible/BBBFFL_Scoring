"""Scorer round-review, sign-off and correction workflow (roadmap package
28, issue #58).

This module is the ordinary-round counterpart to `app/scorer_decisions.py`
and `app/db.py`'s `DecisionsRepository` -- which remain exactly what they
were: the Grand Final/SuperScore vertical's `competition_key`/`team_key`
scoped decision store. A *persisted* BBBFFL round (`app.competition_
lifecycle`) has five matchups, each keyed by `matchup_id`/`season_entry_id`,
not a single `competition_key`/`team_key` pair -- so it needs its own
ruling/override tables (`bbbffl_matchup_slot_ruling`,
`bbbffl_matchup_interchange_ruling`, `bbbffl_matchup_override`; see
migrations/versions/0019_round_review.py) and its own read model, but
reuses everything else already built: `app.competition_lifecycle` still
owns the round/matchup/official-result transaction and the atomic
publish/correct write; `app.calculations.MatchupCalculationService` still
owns the one scoring engine and, since this package, also embeds
`app.participation` evidence (DNP state) and Interchange potential scores
into each calculated snapshot; `app.audit` still owns the append-only
history.

## Two kinds of "current state"

`RoundReviewRepository` below is a thin, `DecisionsRepository`-shaped
persistence boundary for scorer rulings/overrides: attributable,
audited, and CAS-protected via `bbbffl_matchup.review_version` (bumped by
every ruling/override write against that matchup; every write and every
sign-off/correction attempt must present the revision it read, or fail
`app.competition_lifecycle.StaleRoundVersionError` rather than silently
overwriting a decision made after it was read -- issue #58 requirement 7).

`build_round_review`/`build_matchup_review` are the read-model side: they
combine a persisted round/matchup, its latest calculated snapshot,
rulings, overrides and official-result history into the single view issue
#58 requirement 1 asks for, and derive `eligible_for_signoff`/`blockers`
so a scorer (or an API caller) never has to infer validity from low-level
records.

`attempt_signoff`/`attempt_correction` are the write side: each builds a
fresh review, refuses (`SignoffValidationError`) if anything is not ready,
freezes the exact inputs the resulting official result was computed from,
and makes exactly one call into `app.competition_lifecycle`'s existing
atomic publish/correct method -- which re-validates the same round/review
revisions *again*, this time under the row locks it already takes, so the
gap between "read the review" and "commit the write" can never let a
stale decision through (see `CompetitionLifecycleRepository.
publish_results`/`correct_matchup_result`). Nothing here opens its own
multi-row write transaction; the atomicity guarantee stays exactly where
issue #58 requirement 6 asks for it -- the existing repository.
"""

import dataclasses
from dataclasses import dataclass

from app.audit import ActorContext, append_event
from app.competition_lifecycle import StaleRoundVersionError
from app.db import _for_update_suffix, transaction
from app.lineups import POSITIONS as SLOTS
from app.season import _now

OVERRIDE_POSITIONS = tuple(slot for slot in SLOTS if slot != "Interchange")
AUTHORISED_OVERRIDE_ROLES = frozenset({"scorer", "admin"})
_AMBIGUOUS_RECOMMENDATIONS = frozenset({"review_required", "recommend_dnp"})

SLOT_RULING_RECORDED = "review.dnp_ruling.recorded"
INTERCHANGE_RULING_RECORDED = "review.interchange_ruling.recorded"
OVERRIDE_RECORDED = "review.override.recorded"
ENTITY_TYPE_SLOT_RULING = "review.slot_ruling"
ENTITY_TYPE_INTERCHANGE_RULING = "review.interchange_ruling"
ENTITY_TYPE_OVERRIDE = "review.override"


class RoundReviewError(Exception):
    """Base class for this module's domain errors."""


class UnknownMatchupError(RoundReviewError):
    pass


class UnknownRoundError(RoundReviewError):
    pass


class UnknownEntryError(RoundReviewError):
    """`season_entry_id` is not the home or away entry of this matchup."""


class InvalidSlotError(RoundReviewError):
    pass


class InvalidOverridePositionError(RoundReviewError):
    pass


class MissingOverrideReasonError(RoundReviewError):
    pass


class UnauthorisedActorError(RoundReviewError):
    pass


class SignoffValidationError(RoundReviewError):
    """Sign-off/correction was attempted while one or more matchups is not
    ready. `blockers` maps `matchup_id` -> a list of human-readable reasons;
    `round_blockers` holds round-level reasons (e.g. wrong lifecycle
    state). Both are suitable for direct API/UI display (issue #58
    requirement 4) -- never just an opaque exception message."""

    def __init__(self, blockers: dict[str, list[str]], round_blockers: list[str] | None = None):
        self.blockers = blockers
        self.round_blockers = list(round_blockers or [])
        parts = list(self.round_blockers)
        for matchup_id, reasons in blockers.items():
            parts.extend(f"{matchup_id}: {reason}" for reason in reasons)
        super().__init__("round is not ready for sign-off: " + "; ".join(parts) if parts else "round is not ready")


# -- Persisted rulings/overrides --------------------------------------------


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


class RoundReviewRepository:
    """CRUD for ordinary-round scorer rulings/overrides, scoped to
    `matchup_id`/`season_entry_id` -- the `DecisionsRepository` of the
    persisted season model. Every write is CAS-protected against
    `bbbffl_matchup.review_version` and audited in the same transaction as
    its domain write, exactly like `DecisionsRepository`'s methods."""

    def __init__(self, database):
        self.database = database

    def _locked_matchup(self, conn, matchup_id, expected_review_version, *, expected_round_id=None):
        row = conn.execute(
            "SELECT matchup_id, bbbffl_round_id, home_season_entry_id, away_season_entry_id, review_version "
            "FROM bbbffl_matchup WHERE matchup_id=?" + _for_update_suffix(self.database),
            (matchup_id,),
        ).fetchone()
        if not row:
            raise UnknownMatchupError(matchup_id)
        if expected_round_id is not None and row["bbbffl_round_id"] != expected_round_id:
            # A caller that scopes its request by round (e.g. the
            # `/{round_id}/...` API routes) must never let a `matchup_id`
            # naming a *different* round's matchup mutate that other
            # round through this one's URL -- see
            # tests/test_round_review.py::
            # test_ruling_rejects_a_matchup_from_a_different_round.
            raise UnknownMatchupError(f"matchup {matchup_id} does not belong to round {expected_round_id}")
        if expected_review_version is not None and row["review_version"] != expected_review_version:
            raise StaleRoundVersionError(
                f"matchup {matchup_id} review is at version {row['review_version']}, "
                f"not the expected {expected_review_version}"
            )
        return row

    @staticmethod
    def _ensure_entry(matchup, season_entry_id):
        if season_entry_id not in (matchup["home_season_entry_id"], matchup["away_season_entry_id"]):
            raise UnknownEntryError(f"{season_entry_id} is not a participant in matchup {matchup['matchup_id']}")

    def record_dnp_ruling(
        self,
        matchup_id,
        season_entry_id,
        slot,
        dnp,
        *,
        expected_review_version,
        actor: ActorContext,
        reason: str | None = None,
        round_id: str | None = None,
    ) -> int:
        if slot not in SLOTS:
            raise InvalidSlotError(f"Unknown slot: {slot}")
        with transaction(self.database) as conn:
            matchup = self._locked_matchup(conn, matchup_id, expected_review_version, expected_round_id=round_id)
            self._ensure_entry(matchup, season_entry_id)
            existing = conn.execute(
                "SELECT dnp FROM bbbffl_matchup_slot_ruling WHERE matchup_id=? AND season_entry_id=? AND slot=?",
                (matchup_id, season_entry_id, slot),
            ).fetchone()
            before = {"dnp": bool(existing["dnp"])} if existing is not None else {"dnp": None}
            now = _now()
            conn.execute(
                """
                INSERT INTO bbbffl_matchup_slot_ruling
                    (matchup_id, season_entry_id, slot, dnp, decided_by_type, decided_by, decided_by_role, decided_at, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(matchup_id, season_entry_id, slot) DO UPDATE SET
                    dnp=excluded.dnp, decided_by_type=excluded.decided_by_type, decided_by=excluded.decided_by,
                    decided_by_role=excluded.decided_by_role, decided_at=excluded.decided_at, reason=excluded.reason
                """,
                (
                    matchup_id,
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
            new_version = matchup["review_version"] + 1
            conn.execute("UPDATE bbbffl_matchup SET review_version=? WHERE matchup_id=?", (new_version, matchup_id))
            append_event(
                conn,
                actor=actor,
                action=SLOT_RULING_RECORDED,
                entity_type=ENTITY_TYPE_SLOT_RULING,
                entity_id=f"{matchup_id}:{season_entry_id}:{slot}",
                entity_version=str(new_version),
                reason=reason,
                before_state=before,
                after_state={"dnp": dnp},
                payload={"matchup_id": matchup_id, "season_entry_id": season_entry_id, "slot": slot},
            )
        return new_version

    def record_interchange_ruling(
        self,
        matchup_id,
        season_entry_id,
        target_position,
        *,
        expected_review_version,
        actor: ActorContext,
        reason: str | None = None,
        round_id: str | None = None,
    ) -> int:
        if target_position is not None and target_position not in OVERRIDE_POSITIONS:
            raise InvalidSlotError(f"Invalid target_position: {target_position}")
        with transaction(self.database) as conn:
            matchup = self._locked_matchup(conn, matchup_id, expected_review_version, expected_round_id=round_id)
            self._ensure_entry(matchup, season_entry_id)
            existing = conn.execute(
                "SELECT target_position FROM bbbffl_matchup_interchange_ruling "
                "WHERE matchup_id=? AND season_entry_id=?",
                (matchup_id, season_entry_id),
            ).fetchone()
            before = {"target_position": existing["target_position"] if existing is not None else None}
            now = _now()
            conn.execute(
                """
                INSERT INTO bbbffl_matchup_interchange_ruling
                    (matchup_id, season_entry_id, target_position, decided_by_type, decided_by, decided_by_role, decided_at, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(matchup_id, season_entry_id) DO UPDATE SET
                    target_position=excluded.target_position, decided_by_type=excluded.decided_by_type,
                    decided_by=excluded.decided_by, decided_by_role=excluded.decided_by_role,
                    decided_at=excluded.decided_at, reason=excluded.reason
                """,
                (
                    matchup_id,
                    season_entry_id,
                    target_position,
                    actor.actor_type,
                    actor.actor_id,
                    actor.actor_role,
                    now,
                    reason,
                ),
            )
            new_version = matchup["review_version"] + 1
            conn.execute("UPDATE bbbffl_matchup SET review_version=? WHERE matchup_id=?", (new_version, matchup_id))
            append_event(
                conn,
                actor=actor,
                action=INTERCHANGE_RULING_RECORDED,
                entity_type=ENTITY_TYPE_INTERCHANGE_RULING,
                entity_id=f"{matchup_id}:{season_entry_id}",
                entity_version=str(new_version),
                reason=reason,
                before_state=before,
                after_state={"target_position": target_position},
                payload={"matchup_id": matchup_id, "season_entry_id": season_entry_id},
            )
        return new_version

    def record_override(
        self,
        matchup_id,
        season_entry_id,
        position,
        override_score,
        calculated_score,
        reason: str | None,
        *,
        expected_review_version,
        actor: ActorContext,
        round_id: str | None = None,
    ) -> int:
        """Set (`override_score` not None) or clear (`override_score`
        None) a manual score override. Setting one always requires an
        authorised actor (`AUTHORISED_OVERRIDE_ROLES`) and an explicit
        `reason` (issue #58 requirement 3) -- `calculated_score` is the
        original calculated value at override time, retained alongside the
        replacement so both remain explainable later."""
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
            matchup = self._locked_matchup(conn, matchup_id, expected_review_version, expected_round_id=round_id)
            self._ensure_entry(matchup, season_entry_id)
            existing = conn.execute(
                "SELECT override_score, reason FROM bbbffl_matchup_override "
                "WHERE matchup_id=? AND season_entry_id=? AND position=?",
                (matchup_id, season_entry_id, position),
            ).fetchone()
            before = (
                {"override_score": float(existing["override_score"]), "reason": existing["reason"]}
                if existing is not None
                else {"override_score": None, "reason": None}
            )
            now = _now()
            if override_score is None:
                conn.execute(
                    "DELETE FROM bbbffl_matchup_override WHERE matchup_id=? AND season_entry_id=? AND position=?",
                    (matchup_id, season_entry_id, position),
                )
                after = {"override_score": None, "reason": None}
            else:
                conn.execute(
                    """
                    INSERT INTO bbbffl_matchup_override
                        (matchup_id, season_entry_id, position, override_score, calculated_score, reason,
                         decided_by_type, decided_by, decided_by_role, decided_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(matchup_id, season_entry_id, position) DO UPDATE SET
                        override_score=excluded.override_score, calculated_score=excluded.calculated_score,
                        reason=excluded.reason, decided_by_type=excluded.decided_by_type,
                        decided_by=excluded.decided_by, decided_by_role=excluded.decided_by_role,
                        decided_at=excluded.decided_at
                    """,
                    (
                        matchup_id,
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
            new_version = matchup["review_version"] + 1
            conn.execute("UPDATE bbbffl_matchup SET review_version=? WHERE matchup_id=?", (new_version, matchup_id))
            append_event(
                conn,
                actor=actor,
                action=OVERRIDE_RECORDED,
                entity_type=ENTITY_TYPE_OVERRIDE,
                entity_id=f"{matchup_id}:{season_entry_id}:{position}",
                entity_version=str(new_version),
                reason=reason,
                before_state=before,
                after_state=after,
                payload={
                    "matchup_id": matchup_id,
                    "season_entry_id": season_entry_id,
                    "position": position,
                    "calculated_score": calculated_score,
                },
            )
        return new_version

    def get_slot_rulings(self, matchup_id) -> dict[tuple[str, str], SlotRuling]:
        rows = self.database.execute(
            "SELECT * FROM bbbffl_matchup_slot_ruling WHERE matchup_id=?", (matchup_id,)
        ).fetchall()
        return {
            (row["season_entry_id"], row["slot"]): SlotRuling(
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

    def get_interchange_rulings(self, matchup_id) -> dict[str, InterchangeRuling]:
        rows = self.database.execute(
            "SELECT * FROM bbbffl_matchup_interchange_ruling WHERE matchup_id=?", (matchup_id,)
        ).fetchall()
        return {
            row["season_entry_id"]: InterchangeRuling(
                season_entry_id=row["season_entry_id"],
                target_position=row["target_position"],
                decided_by=row["decided_by"],
                decided_by_role=row["decided_by_role"],
                decided_at=row["decided_at"],
                reason=row["reason"],
            )
            for row in rows
        }

    def get_overrides(self, matchup_id) -> dict[tuple[str, str], Override]:
        rows = self.database.execute(
            "SELECT * FROM bbbffl_matchup_override WHERE matchup_id=?", (matchup_id,)
        ).fetchall()
        return {
            (row["season_entry_id"], row["position"]): Override(
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


# -- Read model ---------------------------------------------------------


@dataclass(frozen=True)
class SlotReview:
    slot: str
    season_player_id: str | None
    canonical_player_id: int | None
    played: bool
    calculated_score: float | None
    participation_state: str | None
    dnp_recommendation: str | None
    participation_reason: str | None
    dnp_ruling: bool | None
    override_score: float | None
    override_reason: str | None
    effective_score: float


@dataclass(frozen=True)
class InterchangeReview:
    season_player_id: str | None
    canonical_player_id: int | None
    played: bool
    dnp_ruling: bool | None
    target_position: str | None
    potential_scores: dict | None


@dataclass(frozen=True)
class SideReview:
    season_entry_id: str
    team_name: str | None
    coach_name: str | None
    lineup_id: str | None
    lineup_version: int | None
    calculated_score: float
    effective_score: float
    slots: list[SlotReview]
    interchange: InterchangeReview


@dataclass(frozen=True)
class MatchupReview:
    matchup_id: str
    matchup_order: int
    review_version: int
    rules_version_id: str | None
    calculation_revision: int | None
    calculation_fingerprint: str | None
    evidence_fresh: bool
    home: SideReview
    away: SideReview
    effective_official_version: int | None
    eligible_for_signoff: bool
    blockers: list[str]


@dataclass(frozen=True)
class RoundReview:
    bbbffl_round_id: str
    round_version: int
    state: str
    matchups: list[MatchupReview]
    ready_for_signoff: bool
    blockers: list[str]


def _identity_lookup(identities, entry_id):
    if identities is None:
        return None, None
    team = identities.get_public_team(entry_id)
    coach = identities.get_current_coach(entry_id)
    return (team.team_name if team else None), (coach.display_name if coach else None)


def _side_review(entry_id, side_snapshot, dnp_rulings, interchange_rulings, overrides, identities):
    team_name, coach_name = _identity_lookup(identities, entry_id)
    slots_by_position = {slot["position"]: slot for slot in side_snapshot["slots"]}
    interchange_slot = slots_by_position.get("Interchange")
    interchange_ruling = interchange_rulings.get(entry_id)
    interchange_dnp_ruling_row = dnp_rulings.get((entry_id, "Interchange"))
    interchange_dnp_ruling = interchange_dnp_ruling_row.dnp if interchange_dnp_ruling_row is not None else None
    potentials = side_snapshot.get("interchange_potential_scores") or {}

    blockers: list[str] = []
    slot_reviews: list[SlotReview] = []
    total_effective = 0.0
    vacancies: list[str] = []

    for slot_dict in side_snapshot["slots"]:
        position = slot_dict["position"]
        if position == "Interchange":
            continue
        ruling_row = dnp_rulings.get((entry_id, position))
        ruling = ruling_row.dnp if ruling_row is not None else None
        override = overrides.get((entry_id, position))
        participation = slot_dict.get("participation") or {}
        named = slot_dict["season_player_id"] is not None

        if named and ruling is None and participation.get("dnp_recommendation") in _AMBIGUOUS_RECOMMENDATIONS:
            blockers.append(f"{entry_id} {position}: DNP status unresolved -- {participation.get('reason')}")

        dnp = bool(ruling)
        vacant = dnp or not named
        targeted = interchange_ruling is not None and interchange_ruling.target_position == position
        using_interchange = vacant and targeted
        if vacant and interchange_ruling is None:
            vacancies.append(position)
        if targeted and not vacant:
            # A stale/invalid interchange ruling naming an occupied
            # position must never silently discard that player's real
            # contribution -- surface it instead of applying it.
            blockers.append(
                f"{entry_id} {position}: interchange ruling targets an occupied, non-DNP position -- resolve before sign-off"
            )

        if using_interchange:
            if interchange_dnp_ruling or interchange_slot is None or interchange_slot["season_player_id"] is None:
                base = 0.0
            else:
                base = potentials.get(position) or 0.0
        elif vacant:
            base = 0.0
        else:
            base = slot_dict["score"] or 0.0

        effective = override.override_score if override is not None else base
        total_effective += effective
        slot_reviews.append(
            SlotReview(
                slot=position,
                season_player_id=slot_dict["season_player_id"],
                canonical_player_id=slot_dict["canonical_player_id"],
                played=slot_dict["played"],
                calculated_score=slot_dict["score"],
                participation_state=participation.get("state"),
                dnp_recommendation=participation.get("dnp_recommendation"),
                participation_reason=participation.get("reason"),
                dnp_ruling=ruling,
                override_score=(override.override_score if override else None),
                override_reason=(override.reason if override else None),
                effective_score=effective,
            )
        )

    if interchange_slot is not None and interchange_slot["season_player_id"] is not None:
        interchange_participation = interchange_slot.get("participation") or {}
        if (
            interchange_dnp_ruling is None
            and interchange_participation.get("dnp_recommendation") in _AMBIGUOUS_RECOMMENDATIONS
        ):
            blockers.append(
                f"{entry_id} Interchange: DNP status unresolved -- {interchange_participation.get('reason')}"
            )
        if vacancies and interchange_ruling is None and not interchange_dnp_ruling:
            blockers.append(
                f"{entry_id}: interchange recommendation unresolved for vacant position(s) {', '.join(vacancies)}"
            )

    interchange_review = InterchangeReview(
        season_player_id=(interchange_slot["season_player_id"] if interchange_slot else None),
        canonical_player_id=(interchange_slot["canonical_player_id"] if interchange_slot else None),
        played=bool(interchange_slot["played"]) if interchange_slot else False,
        dnp_ruling=interchange_dnp_ruling,
        target_position=(interchange_ruling.target_position if interchange_ruling else None),
        potential_scores=(potentials or None),
    )
    side = SideReview(
        season_entry_id=entry_id,
        team_name=team_name,
        coach_name=coach_name,
        lineup_id=side_snapshot.get("lineup_id"),
        lineup_version=side_snapshot.get("lineup_version"),
        calculated_score=side_snapshot["score"],
        effective_score=total_effective,
        slots=slot_reviews,
        interchange=interchange_review,
    )
    return side, blockers


def build_matchup_review(lifecycle, review_repo, identities, matchup, *, evidence_fresh: bool = True) -> MatchupReview:
    """Build the review for one matchup -- issue #58 requirement 1's
    per-matchup surface. `lifecycle` is a `CompetitionLifecycleRepository`
    -shaped object (for `get_calculation`), `review_repo` a
    `RoundReviewRepository`-shaped object, `identities` an
    `IdentityRepository`-shaped object or None (team/coach names are
    display-only and optional)."""
    calc = lifecycle.get_calculation(matchup.matchup_id)
    dnp_rulings = review_repo.get_slot_rulings(matchup.matchup_id)
    interchange_rulings = review_repo.get_interchange_rulings(matchup.matchup_id)
    overrides = review_repo.get_overrides(matchup.matchup_id)

    blockers: list[str] = []
    if calc is None:
        blockers.append("no calculated result is available for this matchup yet")
        empty_home, _ = _identity_lookup(identities, matchup.home_season_entry_id)
        empty_away, _ = _identity_lookup(identities, matchup.away_season_entry_id)
        home = SideReview(
            matchup.home_season_entry_id,
            empty_home,
            None,
            None,
            None,
            0.0,
            0.0,
            [],
            InterchangeReview(None, None, False, None, None, None),
        )
        away = SideReview(
            matchup.away_season_entry_id,
            empty_away,
            None,
            None,
            None,
            0.0,
            0.0,
            [],
            InterchangeReview(None, None, False, None, None, None),
        )
        rules_version_id = calculation_revision = calculation_fingerprint = None
    else:
        if not evidence_fresh:
            blockers.append("AFL evidence behind the calculated result was not confirmed fresh; refresh and retry")
        home, home_blockers = _side_review(
            matchup.home_season_entry_id, calc.snapshot["home"], dnp_rulings, interchange_rulings, overrides, identities
        )
        away, away_blockers = _side_review(
            matchup.away_season_entry_id, calc.snapshot["away"], dnp_rulings, interchange_rulings, overrides, identities
        )
        blockers += home_blockers + away_blockers
        rules_version_id = calc.snapshot.get("rules_version_id")
        calculation_revision = calc.revision
        calculation_fingerprint = calc.input_fingerprint

    return MatchupReview(
        matchup_id=matchup.matchup_id,
        matchup_order=matchup.matchup_order,
        review_version=matchup.review_version,
        rules_version_id=rules_version_id,
        calculation_revision=calculation_revision,
        calculation_fingerprint=calculation_fingerprint,
        evidence_fresh=evidence_fresh,
        home=home,
        away=away,
        effective_official_version=matchup.effective_official_version,
        eligible_for_signoff=not blockers,
        blockers=blockers,
    )


def build_round_review(lifecycle, review_repo, identities, round_id, *, evidence_fresh: bool = True) -> RoundReview:
    """Build the full round review -- issue #58 requirement 1's round-level
    surface. Makes it immediately apparent whether any of the five
    matchups blocks publication, without the caller inferring validity
    from low-level records."""
    round_ = lifecycle.get_round(round_id)
    if round_ is None:
        raise UnknownRoundError(round_id)
    matchups = lifecycle.list_matchups(round_id)
    reviews = [
        build_matchup_review(lifecycle, review_repo, identities, matchup, evidence_fresh=evidence_fresh)
        for matchup in matchups
    ]
    round_blockers: list[str] = []
    if round_.state != "review":
        round_blockers.append(f"round is in state {round_.state!r}, not 'review'")
    if len(reviews) != 5:
        round_blockers.append("round does not have exactly five matchups")
    ready = not round_blockers and all(review.eligible_for_signoff for review in reviews)
    return RoundReview(
        bbbffl_round_id=round_id,
        round_version=round_.version,
        state=round_.state,
        matchups=reviews,
        ready_for_signoff=ready,
        blockers=round_blockers,
    )


# -- Sign-off / correction -----------------------------------------------


def _freeze_side(side: SideReview) -> dict:
    return {
        "season_entry_id": side.season_entry_id,
        "lineup_id": side.lineup_id,
        "lineup_version": side.lineup_version,
        "calculated_score": side.calculated_score,
        "effective_score": side.effective_score,
        "slots": [dataclasses.asdict(slot) for slot in side.slots],
        "interchange": dataclasses.asdict(side.interchange),
    }


def _freeze_matchup_inputs(review: MatchupReview, actor: ActorContext) -> dict:
    """The exact scoring inputs issue #58 requirement 5 asks an official
    result version to freeze: rules version, calculated-result revision/
    fingerprint, lineup versions, DNP rulings, interchange rulings and
    overrides for both sides, plus who/when finalised it. Stored verbatim
    as `bbbffl_official_result.input_snapshot` -- never re-derived from
    live lineup/rule/recommendation state, so a later edit to any of those
    cannot change what an already-published version means."""
    return {
        "matchup_id": review.matchup_id,
        "rules_version_id": review.rules_version_id,
        "calculation_revision": review.calculation_revision,
        "calculation_fingerprint": review.calculation_fingerprint,
        "home": _freeze_side(review.home),
        "away": _freeze_side(review.away),
        "finalized_by": actor.actor_id,
        "finalized_by_role": actor.actor_role,
        "finalized_at": _now(),
    }


def attempt_signoff(
    lifecycle,
    review_repo,
    identities,
    round_id,
    *,
    actor: ActorContext,
    reason: str | None = None,
    evidence_fresh: bool = True,
):
    """Validate and, if every one of the five matchups is ready, publish
    the round atomically (issue #58 requirements 4 and 6).

    Raises `SignoffValidationError` (never partially publishes) if any
    matchup is not ready. If validation passes here but a ruling/override
    or the round itself changed before the write actually commits, the
    single `lifecycle.publish_results` call below re-checks the same
    round/review revisions again under its own row locks and fails with
    `StaleRoundVersionError` instead -- the check-then-act gap is closed
    inside that one transaction, not by locking anything in this function.
    """
    review = build_round_review(lifecycle, review_repo, identities, round_id, evidence_fresh=evidence_fresh)
    if not review.ready_for_signoff:
        blockers = {m.matchup_id: m.blockers for m in review.matchups if m.blockers}
        raise SignoffValidationError(blockers, review.blockers)

    results, review_versions, input_snapshots = {}, {}, {}
    for matchup in review.matchups:
        results[matchup.matchup_id] = (matchup.home.effective_score, matchup.away.effective_score)
        review_versions[matchup.matchup_id] = matchup.review_version
        input_snapshots[matchup.matchup_id] = _freeze_matchup_inputs(matchup, actor)

    return lifecycle.publish_results(
        round_id,
        results,
        actor=actor,
        reason=reason,
        input_snapshots=input_snapshots,
        expected_round_version=review.round_version,
        expected_review_versions=review_versions,
    )


def attempt_correction(
    lifecycle,
    review_repo,
    identities,
    matchup_id,
    *,
    actor: ActorContext,
    reason: str,
    evidence_fresh: bool = True,
):
    """Correct one already-final matchup's official result (issue #58
    requirement 9): the previous official version is preserved unchanged,
    a new version is frozen from the *current* rulings/overrides/
    calculation and becomes effective, and the round never has to leave
    `final` for this -- see `CompetitionLifecycleRepository.
    correct_matchup_result`'s docstring for why that already represents
    "reopening" this one matchup safely without risking any other."""
    if not reason:
        raise ValueError("an authorised correction requires a reason")
    matchup = lifecycle.get_matchup(matchup_id)
    if matchup is None:
        raise UnknownMatchupError(matchup_id)
    review = build_matchup_review(lifecycle, review_repo, identities, matchup, evidence_fresh=evidence_fresh)
    if review.blockers:
        raise SignoffValidationError({matchup_id: review.blockers})
    snapshot = _freeze_matchup_inputs(review, actor)
    return lifecycle.correct_matchup_result(
        matchup_id,
        review.home.effective_score,
        review.away.effective_score,
        reason=reason,
        actor=actor,
        input_snapshot=snapshot,
        expected_review_version=review.review_version,
    )
