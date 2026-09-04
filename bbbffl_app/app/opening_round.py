"""Opening Round deferred-selection configuration, nomination and locking
(roadmap follow-up to #31, issue #69).

Three AFL seasons in the supplied evidence (2024, 2025, 2026) used an
"Opening Round" (AFL round number 0, name "Opening Round") contested by only
some clubs; each participating club later received a compensating bye in a
different, season-specific ordinary round. BBBFFL allowed a coach/proxy to
nominate an eligible Opening Round player into a specific future BBBFFL
lineup slot; when that later round was scored, that one slot drew its
statistics from the player's Opening Round match while every other slot in
the same lineup continued to score from the round's ordinary mapped AFL
round. See docs/opening-round-deferred-selection.md for the full design,
evidence and historical mapping table.

This module intentionally never activates from `season in (2024, 2025,
2026)`, an AFL round numbered/named "Opening Round", or a club having *any*
later bye -- see `OpeningRoundRuleRepository`, which mirrors
`app.round_mapping.RoundMappingRepository`'s "acceptance is the only
activation boundary" precedent exactly. A season that never proposes/accepts
a rule here behaves identically to one without this module.

## Two independent boundaries

- `OpeningRoundRuleRepository` is the season+club scoped **configuration**:
  for one `(season_id, afl_club_id)`, which AFL Opening Round it played,
  which AFL round is its compensating bye, and which BBBFFL round is the
  corresponding target. Versioned exactly like `round_afl_mapping` --
  `propose`/`accept`/`correct`/`resolve`/`history` -- so setup can be
  represented as `unresolved`/`ambiguous` and only an `accepted` revision is
  operational.

- `OpeningRoundNominationRepository` is the player-level **decision**: which
  specific owned player was nominated, by whom (an operator acting as proxy,
  never recorded as the historical coach -- see `_ensure_operator`, the same
  pattern as `app.lineup_proxy`), into which BBBFFL slot, resolved against
  which Opening Round AFL match. A nomination is corrected in place (an
  audited UPDATE, like `app.round_review`'s rulings) rather than a second
  parallel revision history -- `app.audit`'s append-only log already
  retains the required before/after/actor/reason trail for a correction.

## Locking a nominated slot

`OpeningRoundSelectionGuard` is a `lock_guard`-shaped object (see
`app.lockouts.LockGuard`) usable directly as
`app.lineups.WeeklyLineupRepository.submit`'s `lock_guard` argument, or
composed with an ordinary `app.lockouts.LockGuard` via its `inner` argument.
It rejects any submission that would place a player other than the
nominated one into a locked slot, and -- critically -- excludes locked slots
entirely from whatever inner lockout guard it wraps, so the ordinary
AFL-match lockout evaluation never attempts to resolve the deferred player's
match in the *target* round (where their club is, by construction, on its
compensating bye and has no match to resolve against; see
app/lockouts.py's `resolve_match`). This is what lets both mechanisms
coexist in one lineup: the deferred slot is frozen by its own, unrelated
rule, while every other slot in the same lineup is governed exactly as
before by `app.lockouts`.

`preload_target_lineup` seeds a nominated slot into the target round's
private draft (persisted state, not a UI-only flag) so it appears already
filled before a coach ever visits that round -- reusing
`app.lineups.WeeklyLineupRepository.save_draft`/`get_or_create_header`
rather than a second lineup-content store. It is idempotent and safe to call
repeatedly (e.g. from a round-open workflow or a replay operator), and never
touches positions with no active nomination.
"""

import dataclasses
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.exc import IntegrityError

from app.audit import ActorContext, ConnectionLike, append_event
from app.db import DatabaseConnection, _for_update_suffix, transaction
from app.lineups import POSITIONS, LineupIntegrityError, WeeklyLineupRepository
from app.lockouts import MatchResolutionError, resolve_match
from app.round_mapping import AflReferenceValidator
from app.season import _id, _now

RULE_PROPOSED = "opening_round.rule.proposed"
RULE_ACCEPTED = "opening_round.rule.accepted"
RULE_CORRECTED = "opening_round.rule.corrected"
NOMINATION_CREATED = "opening_round.nomination.created"
NOMINATION_CORRECTED = "opening_round.nomination.corrected"
SUBMISSION_CONFIRMED = "opening_round.submission.confirmed"
SUBMISSION_REOPENED = "opening_round.submission.reopened"

ENTITY_TYPE_RULE = "opening_round.rule"
ENTITY_TYPE_NOMINATION = "opening_round.nomination"
ENTITY_TYPE_SUBMISSION = "opening_round.submission"

PROXY_ACTOR_ROLES = frozenset({"scorer", "admin", "replay_operator"})

# See docs/roadmap/2027-season-roadmap.md's "Evidence and confidence
# convention" -- the same four replay evidence classifications, reused here
# rather than inventing a second vocabulary.
EVIDENCE_CLASSIFICATIONS = frozenset(
    {"known_fact", "reconstructable_behaviour", "synthetic_scenario", "unresolved_scorer_input"}
)


class OpeningRoundError(LineupIntegrityError):
    """Base class for this module's domain errors."""


class UnknownRuleError(OpeningRoundError):
    pass


class OpeningRoundRuleHasNominationsError(OpeningRoundError):
    """An accepted rule cannot be corrected while nominations reference it
    -- see `OpeningRoundRuleRepository._activate`'s docstring comment for
    why a rule correction must never silently orphan a nomination's
    denormalized target round/source match."""


class IneligiblePlayerError(OpeningRoundError):
    """The nominated player is not owned by the entry, not the rule's AFL
    club, or the Opening Round evidence does not support their
    participation."""


class UnauthorizedNominationActorError(OpeningRoundError):
    """See this module's docstring, 'Locking a nominated slot' / #69's
    'A scorer/replay operator acting as proxy must not be recorded as
    though the historical coach personally authenticated and performed the
    action.'"""


class DeferredSlotLockedError(OpeningRoundError):
    """An ordinary edit, carry-forward or resubmission attempted to change
    a slot that a nomination has already locked."""


class SubmissionConfirmedError(OpeningRoundError):
    """A nomination cannot be created or corrected for a season entry whose
    Opening Round submission has already been confirmed (issue #133 #3): a
    confirmed submission must not be silently mutated. An explicit, audited
    `OpeningRoundSubmissionRepository.reopen` is required first."""


def _ensure_operator(actor: ActorContext) -> None:
    if actor.actor_type != "anonymous_operator" or actor.actor_role not in PROXY_ACTOR_ROLES:
        raise UnauthorizedNominationActorError(
            "Opening Round nominations require an anonymous_operator actor with actor_role scorer, admin, or replay_operator "
            f"(acting as proxy for the historical coach), got actor_type={actor.actor_type!r} "
            f"actor_role={actor.actor_role!r}"
        )


# -- Configuration: OpeningRoundRule ----------------------------------------


@dataclass(frozen=True)
class OpeningRoundRule:
    rule_id: str
    season_id: str
    afl_club_id: int
    revision: int
    state: str
    afl_season_id: int | None
    afl_opening_round_id: int | None
    afl_bye_round_id: int | None
    bbbffl_round_id: str | None
    evidence_classification: str | None
    created_at: str
    created_by: str | None
    reason: str | None


def _rule(row) -> OpeningRoundRule:
    return OpeningRoundRule(
        row["rule_id"],
        row["season_id"],
        row["afl_club_id"],
        row["revision"],
        row["state"],
        row["afl_season_id"],
        row["afl_opening_round_id"],
        row["afl_bye_round_id"],
        row["bbbffl_round_id"],
        row["evidence_classification"],
        row["created_at"],
        row["created_by"],
        row["reason"],
    )


class OpeningRoundRuleRepository:
    """Season+club scoped, versioned Opening Round configuration -- the
    `app.round_mapping.RoundMappingRepository` of this module. Acceptance is
    the only activation boundary; nothing here infers activation from a
    season year, an AFL round's name/number, or a club having a later bye
    (see this module's docstring)."""

    def __init__(self, database: DatabaseConnection):
        self.database = database

    def propose(
        self,
        season_id: str,
        afl_club_id: int,
        *,
        state: str = "unresolved",
        afl_season_id: int | None = None,
        afl_opening_round_id: int | None = None,
        afl_bye_round_id: int | None = None,
        bbbffl_round_id: str | None = None,
        evidence_classification: str | None = None,
        actor: ActorContext = ActorContext.anonymous_operator("admin"),
        reason: str | None = None,
    ) -> OpeningRoundRule:
        if state not in {"unresolved", "ambiguous"}:
            raise ValueError("a proposal must be unresolved or ambiguous; use accept() to activate")
        self._validate_evidence_classification(evidence_classification)
        with transaction(self.database) as conn:
            head = conn.execute(
                "SELECT * FROM opening_round_rule WHERE season_id=? AND afl_club_id=?"
                + _for_update_suffix(self.database),
                (season_id, afl_club_id),
            ).fetchone()
            if head:
                current = self._current(conn, head["rule_id"])
                if current["state"] == "accepted":
                    raise ValueError("accepted rule requires authorised correction")
                rule_id, revision = head["rule_id"], head["current_revision"] + 1
                conn.execute("UPDATE opening_round_rule SET current_revision=? WHERE rule_id=?", (revision, rule_id))
            else:
                rule_id, revision = _id(), 1
                conn.execute(
                    "INSERT INTO opening_round_rule VALUES (?, ?, ?, ?, ?)",
                    (rule_id, season_id, afl_club_id, revision, _now()),
                )
            self._insert_revision(
                conn,
                rule_id,
                revision,
                state,
                afl_season_id,
                afl_opening_round_id,
                afl_bye_round_id,
                bbbffl_round_id,
                evidence_classification,
                actor,
                reason,
            )
            append_event(
                conn,
                actor=actor,
                action=RULE_PROPOSED,
                entity_type=ENTITY_TYPE_RULE,
                entity_id=rule_id,
                entity_version=str(revision),
                reason=reason,
                after_state={
                    "state": state,
                    "afl_season_id": afl_season_id,
                    "afl_opening_round_id": afl_opening_round_id,
                    "afl_bye_round_id": afl_bye_round_id,
                    "bbbffl_round_id": bbbffl_round_id,
                },
            )
            return self._get(conn, rule_id)

    def accept(
        self,
        season_id: str,
        afl_club_id: int,
        afl_season_id: int,
        afl_opening_round_id: int,
        afl_bye_round_id: int,
        bbbffl_round_id: str,
        validator: AflReferenceValidator,
        *,
        evidence_classification: str | None = "known_fact",
        actor: ActorContext = ActorContext.anonymous_operator("admin"),
        reason: str | None = None,
    ) -> OpeningRoundRule:
        if not validator.round_exists(afl_season_id, afl_opening_round_id):
            raise ValueError("AFL Opening Round reference does not exist")
        if not validator.round_exists(afl_season_id, afl_bye_round_id):
            raise ValueError("AFL compensating bye round reference does not exist")
        return self._activate(
            season_id,
            afl_club_id,
            afl_season_id,
            afl_opening_round_id,
            afl_bye_round_id,
            bbbffl_round_id,
            evidence_classification,
            actor,
            reason,
            correction=False,
        )

    def correct(
        self,
        season_id: str,
        afl_club_id: int,
        afl_season_id: int,
        afl_opening_round_id: int,
        afl_bye_round_id: int,
        bbbffl_round_id: str,
        validator: AflReferenceValidator,
        *,
        reason: str,
        evidence_classification: str | None = "known_fact",
        actor: ActorContext = ActorContext.anonymous_operator("admin"),
    ) -> OpeningRoundRule:
        if not reason:
            raise ValueError("an authorised correction requires a reason")
        if not validator.round_exists(afl_season_id, afl_opening_round_id):
            raise ValueError("AFL Opening Round reference does not exist")
        if not validator.round_exists(afl_season_id, afl_bye_round_id):
            raise ValueError("AFL compensating bye round reference does not exist")
        return self._activate(
            season_id,
            afl_club_id,
            afl_season_id,
            afl_opening_round_id,
            afl_bye_round_id,
            bbbffl_round_id,
            evidence_classification,
            actor,
            reason,
            correction=True,
        )

    def resolve(self, season_id: str, afl_club_id: int) -> OpeningRoundRule | None:
        row = self.database.execute(
            self._select() + " WHERE m.season_id=? AND m.afl_club_id=? AND r.state='accepted'",
            (season_id, afl_club_id),
        ).fetchone()
        return _rule(row) if row else None

    def resolve_by_id(self, rule_id: str) -> OpeningRoundRule | None:
        row = self.database.execute(self._select() + " WHERE m.rule_id=?", (rule_id,)).fetchone()
        return _rule(row) if row else None

    def list_accepted_for_season(self, season_id: str) -> list[OpeningRoundRule]:
        rows = self.database.execute(
            self._select() + " WHERE m.season_id=? AND r.state='accepted' ORDER BY m.afl_club_id", (season_id,)
        ).fetchall()
        return [_rule(row) for row in rows]

    def list_accepted_for_season_locked(self, conn: ConnectionLike, season_id: str) -> list[OpeningRoundRule]:
        """Same as `list_accepted_for_season`, but read via an already-open
        transaction connection -- for a caller (e.g. the 2026 replay
        bootstrap) that must reconcile accepted rules alongside other writes
        in one transaction. See `OpeningRoundNominationRepository.
        active_positions_locked` for the identical pattern."""
        rows = conn.execute(
            self._select() + " WHERE m.season_id=? AND r.state='accepted' ORDER BY m.afl_club_id", (season_id,)
        ).fetchall()
        return [_rule(row) for row in rows]

    def resolve_locked(self, conn: ConnectionLike, season_id: str, afl_club_id: int) -> OpeningRoundRule | None:
        """Same as `resolve`, but read via an already-open transaction
        connection -- see `list_accepted_for_season_locked`."""
        row = conn.execute(
            self._select() + " WHERE m.season_id=? AND m.afl_club_id=? AND r.state='accepted'",
            (season_id, afl_club_id),
        ).fetchone()
        return _rule(row) if row else None

    def history(self, season_id: str, afl_club_id: int) -> list[OpeningRoundRule]:
        rows = self.database.execute(
            self._select(False) + " WHERE m.season_id=? AND m.afl_club_id=? ORDER BY r.revision",
            (season_id, afl_club_id),
        ).fetchall()
        return [_rule(row) for row in rows]

    def accept_locked(
        self,
        conn: ConnectionLike,
        season_id: str,
        afl_club_id: int,
        afl_season_id: int,
        afl_opening_round_id: int,
        afl_bye_round_id: int,
        bbbffl_round_id: str,
        validator: AflReferenceValidator,
        *,
        evidence_classification: str | None = "known_fact",
        actor: ActorContext = ActorContext.anonymous_operator("admin"),
        reason: str | None = None,
    ) -> OpeningRoundRule:
        """Same activation semantics as `accept`, but participates in an
        already-open transaction/connection instead of opening its own --
        for a caller (e.g. the 2026 replay bootstrap) that must accept
        several rules atomically alongside other writes in one outer
        transaction. `accept`/`correct` themselves open a fresh connection
        per call (see `app.db.transaction`), so calling them from inside
        another already-open transaction would silently run against a
        second, independent connection/transaction -- exactly the nested-
        transaction hazard this method exists to avoid."""
        if not validator.round_exists(afl_season_id, afl_opening_round_id):
            raise ValueError("AFL Opening Round reference does not exist")
        if not validator.round_exists(afl_season_id, afl_bye_round_id):
            raise ValueError("AFL compensating bye round reference does not exist")
        return self._activate_on_conn(
            conn,
            season_id,
            afl_club_id,
            afl_season_id,
            afl_opening_round_id,
            afl_bye_round_id,
            bbbffl_round_id,
            evidence_classification,
            actor,
            reason,
            correction=False,
        )

    def _activate(
        self,
        season_id,
        afl_club_id,
        afl_season_id,
        afl_opening_round_id,
        afl_bye_round_id,
        bbbffl_round_id,
        evidence_classification,
        actor,
        reason,
        correction,
    ) -> OpeningRoundRule:
        with transaction(self.database) as conn:
            return self._activate_on_conn(
                conn,
                season_id,
                afl_club_id,
                afl_season_id,
                afl_opening_round_id,
                afl_bye_round_id,
                bbbffl_round_id,
                evidence_classification,
                actor,
                reason,
                correction,
            )

    def _activate_on_conn(
        self,
        conn: ConnectionLike,
        season_id,
        afl_club_id,
        afl_season_id,
        afl_opening_round_id,
        afl_bye_round_id,
        bbbffl_round_id,
        evidence_classification,
        actor,
        reason,
        correction,
    ) -> OpeningRoundRule:
        self._validate_evidence_classification(evidence_classification)
        head = conn.execute(
            "SELECT * FROM opening_round_rule WHERE season_id=? AND afl_club_id=?" + _for_update_suffix(self.database),
            (season_id, afl_club_id),
        ).fetchone()
        if not head:
            if correction:
                raise ValueError("correction requires an accepted rule")
            rule_id, revision = _id(), 1
            conn.execute(
                "INSERT INTO opening_round_rule VALUES (?, ?, ?, ?, ?)",
                (rule_id, season_id, afl_club_id, revision, _now()),
            )
        else:
            rule_id = head["rule_id"]
            old = self._current(conn, rule_id)
            if correction != (old["state"] == "accepted"):
                raise ValueError(
                    "use correction for an accepted rule"
                    if old["state"] == "accepted"
                    else "correction requires an accepted rule"
                )
            if (
                correction
                and conn.execute("SELECT 1 FROM opening_round_nomination WHERE rule_id=?", (rule_id,)).fetchone()
            ):
                # Every nomination denormalizes this rule's target round
                # and resolves its source match against this rule's
                # Opening Round at nomination time (see
                # `OpeningRoundNominationRepository.nominate`); neither
                # is updated by a later rule correction. Rather than
                # leave those nominations pointing at a now-superseded
                # AFL Opening Round/target round -- a hybrid
                # configuration `app.calculations` could silently score
                # against the wrong fixture -- refuse outright. An
                # operator must correct/reassign the affected
                # nominations (see `OpeningRoundNominationRepository.
                # correct`) before this rule itself can be corrected.
                raise OpeningRoundRuleHasNominationsError(
                    f"rule {rule_id} has existing nominations; correct or reassign them "
                    "before correcting the rule itself"
                )
            revision = head["current_revision"] + 1
            conn.execute("UPDATE opening_round_rule SET current_revision=? WHERE rule_id=?", (revision, rule_id))
        self._insert_revision(
            conn,
            rule_id,
            revision,
            "accepted",
            afl_season_id,
            afl_opening_round_id,
            afl_bye_round_id,
            bbbffl_round_id,
            evidence_classification,
            actor,
            reason,
        )
        append_event(
            conn,
            actor=actor,
            action=RULE_CORRECTED if correction else RULE_ACCEPTED,
            entity_type=ENTITY_TYPE_RULE,
            entity_id=rule_id,
            entity_version=str(revision),
            reason=reason,
            after_state={
                "state": "accepted",
                "afl_season_id": afl_season_id,
                "afl_opening_round_id": afl_opening_round_id,
                "afl_bye_round_id": afl_bye_round_id,
                "bbbffl_round_id": bbbffl_round_id,
            },
        )
        return self._get(conn, rule_id)

    @staticmethod
    def _validate_evidence_classification(value):
        if value is not None and value not in EVIDENCE_CLASSIFICATIONS:
            raise ValueError(f"evidence_classification must be one of {sorted(EVIDENCE_CLASSIFICATIONS)} or None")

    @staticmethod
    def _insert_revision(
        conn: ConnectionLike,
        rule_id,
        revision,
        state,
        afl_season_id,
        afl_opening_round_id,
        afl_bye_round_id,
        bbbffl_round_id,
        evidence_classification,
        actor: ActorContext,
        reason,
    ) -> None:
        conn.execute(
            "INSERT INTO opening_round_rule_revision VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rule_id,
                revision,
                state,
                afl_season_id,
                afl_opening_round_id,
                afl_bye_round_id,
                bbbffl_round_id,
                evidence_classification,
                _now(),
                actor.actor_id,
                reason,
            ),
        )

    @staticmethod
    def _current(conn: ConnectionLike, rule_id):
        return conn.execute(
            "SELECT r.* FROM opening_round_rule m JOIN opening_round_rule_revision r ON r.rule_id=m.rule_id AND r.revision=m.current_revision WHERE m.rule_id=?",
            (rule_id,),
        ).fetchone()

    @staticmethod
    def _select(current: bool = True) -> str:
        join = " AND r.revision=m.current_revision" if current else ""
        return (
            "SELECT m.rule_id, m.season_id, m.afl_club_id, r.* FROM opening_round_rule m "
            "JOIN opening_round_rule_revision r ON r.rule_id=m.rule_id" + join
        )

    def _get(self, conn: ConnectionLike, rule_id) -> OpeningRoundRule:
        return _rule(conn.execute(self._select() + " WHERE m.rule_id=?", (rule_id,)).fetchone())


# -- Decision: OpeningRoundNomination ----------------------------------------


@dataclass(frozen=True)
class OpeningRoundNomination:
    nomination_id: str
    season_id: str
    rule_id: str
    bbbffl_round_id: str
    season_entry_id: str
    position: str
    season_player_id: str
    source_afl_match_id: int | None
    actor_type: str
    actor_id: str | None
    actor_role: str | None
    effective_at: str
    created_at: str
    updated_at: str


def _nomination(row) -> OpeningRoundNomination:
    return OpeningRoundNomination(
        row["nomination_id"],
        row["season_id"],
        row["rule_id"],
        row["bbbffl_round_id"],
        row["season_entry_id"],
        row["position"],
        row["season_player_id"],
        row["source_afl_match_id"],
        row["actor_type"],
        row["actor_id"],
        row["actor_role"],
        row["effective_at"],
        row["created_at"],
        row["updated_at"],
    )


class OpeningRoundMatchFacts(Protocol):
    def get_matches(self, afl_round_id: int) -> list: ...


class OpeningRoundNominationRepository:
    """Player-level Opening Round deferred-selection decisions. Every write
    is attributed to an operator acting as proxy (`_ensure_operator`) --
    see this module's docstring and `app.lineup_proxy`'s identical
    rationale."""

    def __init__(self, database: DatabaseConnection):
        self.database = database
        self.rules = OpeningRoundRuleRepository(database)
        self.submissions = OpeningRoundSubmissionRepository(database)

    def nominate(
        self,
        rule_id: str,
        season_entry_id: str,
        position: str,
        season_player_id: str,
        afl_client: OpeningRoundMatchFacts,
        *,
        actor: ActorContext,
        reason: str | None = None,
        effective_at: str | None = None,
    ) -> OpeningRoundNomination:
        """Validate and persist a new nomination under an accepted rule.

        Validates (issue #69's nomination workflow): the rule is accepted;
        `position` is a legal BBBFFL slot; the player belongs to the rule's
        season and is currently owned by `season_entry_id`; the player's
        cached AFL club matches the rule's `afl_club_id`; and the AFL
        Opening Round evidence actually resolves the player's club to
        exactly one match (`app.lockouts.resolve_match`) -- an ineligible
        player, a club mismatch, or unresolved/ambiguous Opening Round
        evidence all fail explicitly rather than nominating a player the
        evidence does not support.
        """
        _ensure_operator(actor)
        if position not in POSITIONS:
            raise OpeningRoundError(f"unknown scoring position: {position!r}")
        rule = self.rules.resolve_by_id(rule_id)
        if rule is None or rule.state != "accepted":
            raise UnknownRuleError(f"rule {rule_id} is not an accepted Opening Round rule")
        with transaction(self.database) as conn:
            if self.submissions.lock_state(conn, rule.season_id, season_entry_id) == "confirmed":
                raise SubmissionConfirmedError(
                    f"season entry {season_entry_id} has already confirmed its Opening Round submission; "
                    "reopen it before adding a new nomination"
                )
            player = conn.execute(
                "SELECT season_id, afl_team_id FROM season_player_pool WHERE season_player_id=?"
                + _for_update_suffix(self.database),
                (season_player_id,),
            ).fetchone()
            if not player or player["season_id"] != rule.season_id:
                raise IneligiblePlayerError("nominated player must belong to the rule's season")
            if player["afl_team_id"] != rule.afl_club_id:
                raise IneligiblePlayerError(
                    f"player's AFL club {player['afl_team_id']!r} does not match rule club {rule.afl_club_id!r}"
                )
            owner = conn.execute(
                "SELECT season_entry_id FROM player_ownership_period WHERE season_player_id=? AND released_at IS NULL",
                (season_player_id,),
            ).fetchone()
            if not owner or owner["season_entry_id"] != season_entry_id:
                raise IneligiblePlayerError("nominated player is not currently owned by the nominating entry")
            # Explicit pre-checks for this module's three slot-uniqueness
            # invariants (see migrations/versions/0020_opening_round_deferral.py),
            # so a violation raises a clear, dialect-independent message
            # rather than parsing a database-specific constraint-violation
            # string (SQLite and PostgreSQL phrase the same violation
            # differently). The `FOR UPDATE`-guarded reads above already
            # serialize this transaction against a concurrent nomination for
            # the same player/entry; a genuine race that still slips through
            # is caught by the database constraint itself, immediately below.
            if conn.execute(
                "SELECT 1 FROM opening_round_nomination WHERE rule_id=? AND season_entry_id=?",
                (rule_id, season_entry_id),
            ).fetchone():
                raise OpeningRoundError(
                    "this entry already has a nomination under this rule; use correct() to change it"
                )
            if conn.execute(
                "SELECT 1 FROM opening_round_nomination WHERE bbbffl_round_id=? AND season_entry_id=? AND position=?",
                (rule.bbbffl_round_id, season_entry_id, position),
            ).fetchone():
                raise OpeningRoundError(f"target slot {position} for this round/entry is already nominated")
            if conn.execute(
                "SELECT 1 FROM opening_round_nomination WHERE bbbffl_round_id=? AND season_entry_id=? AND season_player_id=?",
                (rule.bbbffl_round_id, season_entry_id, season_player_id),
            ).fetchone():
                raise OpeningRoundError("this player is already nominated into another slot for this round/entry")
            source_match_id = self._resolve_source_match(afl_client, rule.afl_opening_round_id, rule.afl_club_id)
            nomination_id, now = _id(), _now()
            at = effective_at or now
            try:
                conn.execute(
                    "INSERT INTO opening_round_nomination VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        nomination_id,
                        rule.season_id,
                        rule_id,
                        rule.bbbffl_round_id,
                        season_entry_id,
                        position,
                        season_player_id,
                        source_match_id,
                        actor.actor_type,
                        actor.actor_id,
                        actor.actor_role,
                        at,
                        now,
                        now,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 -- a genuine concurrent race past the checks above
                raise OpeningRoundError(
                    "nomination violates a slot/player/rule uniqueness invariant (concurrent write)"
                ) from exc
            append_event(
                conn,
                actor=actor,
                action=NOMINATION_CREATED,
                entity_type=ENTITY_TYPE_NOMINATION,
                entity_id=nomination_id,
                reason=reason,
                after_state={
                    "bbbffl_round_id": rule.bbbffl_round_id,
                    "season_entry_id": season_entry_id,
                    "position": position,
                    "season_player_id": season_player_id,
                    "source_afl_match_id": source_match_id,
                },
                payload={"rule_id": rule_id},
            )
            return _nomination(
                conn.execute(
                    "SELECT * FROM opening_round_nomination WHERE nomination_id=?", (nomination_id,)
                ).fetchone()
            )

    def correct(
        self,
        nomination_id: str,
        *,
        position: str | None = None,
        season_player_id: str | None = None,
        actor: ActorContext,
        reason: str,
    ) -> OpeningRoundNomination:
        """Authorised in-place correction of an existing nomination's slot
        and/or player, preserving the original state as the audit event's
        `before_state` (see this module's docstring on why a correction is
        an audited UPDATE rather than a second revision-history table).
        Never rewrites an already-scored/published round's result -- that
        remains #58's separate correction workflow.

        When `season_player_id` is supplied, the replacement player is
        revalidated against exactly the same eligibility rules `nominate()`
        enforces (season, current ownership by this nomination's entry, and
        the rule's AFL club) -- a correction must never be able to install
        an unowned, wrong-season or wrong-club player that `nominate()`
        itself would have refused."""
        _ensure_operator(actor)
        if not reason:
            raise OpeningRoundError("a nomination correction requires a reason")
        with transaction(self.database) as conn:
            existing = conn.execute(
                "SELECT * FROM opening_round_nomination WHERE nomination_id=?" + _for_update_suffix(self.database),
                (nomination_id,),
            ).fetchone()
            if not existing:
                raise KeyError(nomination_id)
            if self.submissions.lock_state(conn, existing["season_id"], existing["season_entry_id"]) == "confirmed":
                raise SubmissionConfirmedError(
                    f"season entry {existing['season_entry_id']} has already confirmed its Opening Round "
                    "submission; reopen it before correcting a nomination"
                )
            new_position = position if position is not None else existing["position"]
            new_player = season_player_id if season_player_id is not None else existing["season_player_id"]
            if new_position not in POSITIONS:
                raise OpeningRoundError(f"unknown scoring position: {new_position!r}")
            if season_player_id is not None and season_player_id != existing["season_player_id"]:
                rule = self.rules.resolve_by_id(existing["rule_id"])
                if rule is None:
                    raise UnknownRuleError(f"rule {existing['rule_id']} no longer exists")
                player = conn.execute(
                    "SELECT season_id, afl_team_id FROM season_player_pool WHERE season_player_id=?",
                    (new_player,),
                ).fetchone()
                if not player or player["season_id"] != rule.season_id:
                    raise IneligiblePlayerError("replacement player must belong to the rule's season")
                if player["afl_team_id"] != rule.afl_club_id:
                    raise IneligiblePlayerError(
                        f"replacement player's AFL club {player['afl_team_id']!r} does not match "
                        f"rule club {rule.afl_club_id!r}"
                    )
                owner = conn.execute(
                    "SELECT season_entry_id FROM player_ownership_period WHERE season_player_id=? AND released_at IS NULL",
                    (new_player,),
                ).fetchone()
                if not owner or owner["season_entry_id"] != existing["season_entry_id"]:
                    raise IneligiblePlayerError("replacement player is not currently owned by the nominating entry")
            conflicting_slot = conn.execute(
                "SELECT nomination_id FROM opening_round_nomination "
                "WHERE bbbffl_round_id=? AND season_entry_id=? AND position=? AND nomination_id<>?",
                (existing["bbbffl_round_id"], existing["season_entry_id"], new_position, nomination_id),
            ).fetchone()
            if conflicting_slot:
                raise OpeningRoundError(f"target slot {new_position} for this round/entry is already nominated")
            conflicting_player = conn.execute(
                "SELECT nomination_id FROM opening_round_nomination "
                "WHERE bbbffl_round_id=? AND season_entry_id=? AND season_player_id=? AND nomination_id<>?",
                (existing["bbbffl_round_id"], existing["season_entry_id"], new_player, nomination_id),
            ).fetchone()
            if conflicting_player:
                raise OpeningRoundError("this player is already nominated into another slot for this round/entry")
            before = {"position": existing["position"], "season_player_id": existing["season_player_id"]}
            now = _now()
            try:
                conn.execute(
                    "UPDATE opening_round_nomination SET position=?, season_player_id=?, updated_at=? "
                    "WHERE nomination_id=?",
                    (new_position, new_player, now, nomination_id),
                )
            except IntegrityError as exc:
                # Pre-checks provide useful deterministic messages; the
                # constraint translation closes the concurrent-write race.
                raise OpeningRoundError(
                    "nomination correction conflicts with an existing target slot or nominated player"
                ) from exc
            self._reconcile_corrected_target_draft(conn, existing, new_position, new_player, now)
            append_event(
                conn,
                actor=actor,
                action=NOMINATION_CORRECTED,
                entity_type=ENTITY_TYPE_NOMINATION,
                entity_id=nomination_id,
                reason=reason,
                before_state=before,
                after_state={"position": new_position, "season_player_id": new_player},
            )
            return _nomination(
                conn.execute(
                    "SELECT * FROM opening_round_nomination WHERE nomination_id=?", (nomination_id,)
                ).fetchone()
            )

    @staticmethod
    def _reconcile_corrected_target_draft(conn, previous, new_position: str, new_player: str, now: str) -> None:
        """Reconcile an already-preloaded draft in the correction transaction.

        The old position is cleared only when it still contains the exact
        assignment originally preloaded by this nomination. A later,
        legitimate draft value is never blindly erased. The corrected active
        nomination is then installed exactly once and the draft revision is
        advanced so concurrent browser edits fail rather than overwrite it.
        If no target draft exists yet, the normal preload operation will seed
        the corrected nomination when that lineup is first opened.
        """
        lineup = conn.execute(
            "SELECT lineup_id FROM weekly_lineup WHERE bbbffl_round_id=? AND season_entry_id=?",
            (previous["bbbffl_round_id"], previous["season_entry_id"]),
        ).fetchone()
        if lineup is None:
            return
        lineup_id = lineup["lineup_id"]
        if previous["position"] != new_position:
            old_slot = conn.execute(
                "SELECT season_player_id FROM weekly_lineup_draft_slot WHERE lineup_id=? AND position=?",
                (lineup_id, previous["position"]),
            ).fetchone()
            if old_slot is not None and old_slot["season_player_id"] == previous["season_player_id"]:
                conn.execute(
                    "UPDATE weekly_lineup_draft_slot SET season_player_id=NULL WHERE lineup_id=? AND position=?",
                    (lineup_id, previous["position"]),
                )
        conn.execute(
            "UPDATE weekly_lineup_draft_slot SET season_player_id=? WHERE lineup_id=? AND position=?",
            (new_player, lineup_id, new_position),
        )
        conn.execute(
            "UPDATE weekly_lineup SET draft_revision=draft_revision+1, updated_at=? WHERE lineup_id=?",
            (now, lineup_id),
        )

    @staticmethod
    def _resolve_source_match(afl_client, afl_opening_round_id, afl_club_id) -> int | None:
        try:
            matches = afl_client.get_matches(afl_opening_round_id)
        except Exception as exc:  # noqa: BLE001 -- surfaced as an explicit domain failure, never guessed
            raise IneligiblePlayerError(
                f"could not retrieve Opening Round {afl_opening_round_id} matches: {exc}"
            ) from exc
        try:
            match = resolve_match(afl_club_id, matches)
        except MatchResolutionError as exc:
            raise IneligiblePlayerError(f"Opening Round evidence does not support this nomination: {exc}") from exc
        return match.match_id

    def active_positions(self, bbbffl_round_id: str, season_entry_id: str) -> dict[str, str]:
        """`{position: season_player_id}` for every current nomination
        targeting this round/entry -- the read model
        `app.lineup_validation`/read surfaces use to distinguish a deferred
        slot from an ordinary bye, and `OpeningRoundSelectionGuard` uses to
        decide what to lock."""
        return self._active_positions(self.database, bbbffl_round_id, season_entry_id)

    def active_positions_locked(
        self, conn: ConnectionLike, bbbffl_round_id: str, season_entry_id: str
    ) -> dict[str, str]:
        """Same as `active_positions`, but read via an already-open
        transaction connection -- used by `OpeningRoundSelectionGuard`
        inside `WeeklyLineupRepository.submit`'s own transaction."""
        return self._active_positions(conn, bbbffl_round_id, season_entry_id)

    @staticmethod
    def _active_positions(conn, bbbffl_round_id, season_entry_id) -> dict[str, str]:
        rows = conn.execute(
            "SELECT position, season_player_id FROM opening_round_nomination WHERE bbbffl_round_id=? AND season_entry_id=?",
            (bbbffl_round_id, season_entry_id),
        ).fetchall()
        return {row["position"]: row["season_player_id"] for row in rows}

    def list_for_round(self, bbbffl_round_id: str) -> list[OpeningRoundNomination]:
        rows = self.database.execute(
            "SELECT * FROM opening_round_nomination WHERE bbbffl_round_id=? ORDER BY season_entry_id, position",
            (bbbffl_round_id,),
        ).fetchall()
        return [_nomination(row) for row in rows]

    def list_for_season_entry(self, season_id: str, season_entry_id: str) -> list[OpeningRoundNomination]:
        """Every persisted nomination for one `(season_id, season_entry_id)`,
        exactly once by `nomination_id` -- the season/entry-scoped read the
        represented-entry response needs (issue #133).

        The prior represented-entry response iterated every accepted rule
        and called `list_for_round(rule.bbbffl_round_id)` once per rule,
        re-emitting the same persisted row once for every rule that happened
        to share a target BBBFFL round. Querying `opening_round_nomination`
        directly by its own `season_id`/`season_entry_id` columns returns
        each row once regardless of how many accepted rules target its
        round, without needing to de-duplicate the result afterward."""
        rows = self.database.execute(
            "SELECT * FROM opening_round_nomination WHERE season_id=? AND season_entry_id=? "
            "ORDER BY bbbffl_round_id, position",
            (season_id, season_entry_id),
        ).fetchall()
        return [_nomination(row) for row in rows]

    def get(self, nomination_id: str) -> OpeningRoundNomination | None:
        row = self.database.execute(
            "SELECT * FROM opening_round_nomination WHERE nomination_id=?", (nomination_id,)
        ).fetchone()
        return _nomination(row) if row else None

    def deferred_context(self, bbbffl_round_id: str, season_entry_id: str, position: str) -> dict | None:
        """Read-model detail for one slot, if deferred: rule identity,
        source Opening Round, source match and provenance -- what scorer/
        replay tooling needs to explain *why* a slot is locked and where its
        statistics come from (issue #69's API/read-model expectations)."""
        row = self.database.execute(
            "SELECT n.*, r.afl_club_id, rev.afl_opening_round_id, rev.afl_bye_round_id, rev.evidence_classification "
            "FROM opening_round_nomination n "
            "JOIN opening_round_rule r ON r.rule_id=n.rule_id "
            "JOIN opening_round_rule_revision rev ON rev.rule_id=r.rule_id AND rev.revision=r.current_revision "
            "WHERE n.bbbffl_round_id=? AND n.season_entry_id=? AND n.position=?",
            (bbbffl_round_id, season_entry_id, position),
        ).fetchone()
        if row is None:
            return None
        return {
            "nomination_id": row["nomination_id"],
            "rule_id": row["rule_id"],
            "afl_club_id": row["afl_club_id"],
            "afl_opening_round_id": row["afl_opening_round_id"],
            "afl_bye_round_id": row["afl_bye_round_id"],
            "evidence_classification": row["evidence_classification"],
            "season_player_id": row["season_player_id"],
            "source_afl_match_id": row["source_afl_match_id"],
            "actor_type": row["actor_type"],
            "actor_id": row["actor_id"],
            "actor_role": row["actor_role"],
            "effective_at": row["effective_at"],
        }

    def preload_target_lineup(
        self,
        lineups: WeeklyLineupRepository,
        season_id: str,
        competition_id: str,
        bbbffl_round_id: str,
        season_entry_id: str,
    ) -> None:
        """Seed every currently-nominated slot into the target round's
        private draft so it is already filled/locked before a coach ever
        visits that round (issue #69: "preload that player into the mapped
        later BBBFFL round/slot"). Idempotent: a no-op once the draft
        already matches, and never touches a position with no active
        nomination. Reuses `WeeklyLineupRepository.save_draft` -- the same
        durable, versioned draft store an ordinary coach edit uses -- rather
        than a second lineup-content mechanism.

        This does not itself run inside `submit`'s lock enforcement (see
        `OpeningRoundSelectionGuard`); it only ensures the draft a coach
        first sees already reflects the locked slot. A caller (a round-open
        workflow, an admin/replay operation, or a test) should invoke this
        once the target round's lineup context exists.
        """
        deferred = self.active_positions(bbbffl_round_id, season_entry_id)
        if not deferred:
            return
        lineup_id, _ = lineups.get_or_create_header(season_id, competition_id, bbbffl_round_id, season_entry_id)
        draft = lineups.get_draft(season_id, competition_id, bbbffl_round_id, season_entry_id)
        merged = dict(draft.positions)
        if all(merged.get(position) == player for position, player in deferred.items()):
            return
        merged.update(deferred)
        lineups.save_draft(
            season_id, competition_id, bbbffl_round_id, season_entry_id, merged, expected_revision=draft.revision
        )


class OpeningRoundSelectionGuard:
    """`lock_guard`-shaped object for `app.lineups.WeeklyLineupRepository.
    submit`/`submit_positions`: rejects any submitted change to a slot an
    active nomination has locked, and hides locked slots from `inner` (an
    ordinary `app.lockouts.LockGuard`, or `None`) so the two mechanisms
    coexist correctly -- see this module's docstring."""

    def __init__(self, nominations: OpeningRoundNominationRepository, inner=None):
        self._nominations = nominations
        self._inner = inner

    def materialize(self, lineup_id: str) -> None:
        if self._inner is not None and hasattr(self._inner, "materialize"):
            self._inner.materialize(lineup_id)

    def __call__(self, conn, lineup_row, previous_positions, proposed_positions) -> None:
        deferred = self._nominations.active_positions_locked(
            conn, lineup_row["bbbffl_round_id"], lineup_row["season_entry_id"]
        )
        for position, nominated_player in deferred.items():
            if proposed_positions.get(position) != nominated_player:
                raise DeferredSlotLockedError(
                    f"position {position} is locked by an Opening Round deferred nomination "
                    f"(player {nominated_player}) and cannot be changed by ordinary submission"
                )
        if self._inner is not None:
            remaining_previous = {p: v for p, v in previous_positions.items() if p not in deferred}
            remaining_proposed = {p: v for p, v in proposed_positions.items() if p not in deferred}
            self._inner(conn, lineup_row, remaining_previous, remaining_proposed)


# -- Confirmation: OpeningRoundSubmission ------------------------------------
#
# Issue #133: the historical BBBFFL Opening Round process was always a
# *partial* submission -- a coach/proxy could nominate zero or more eligible
# owned players and deliberately leave the rest vacant. Owning an eligible
# player, or an accepted rule existing for a club represented in an entry's
# squad, must never itself imply a required nomination. The only thing that
# makes an entry's Opening Round input "complete" is this explicit,
# separately persisted confirmation boundary -- never an inferred count of
# nominations against eligible rules/players (see `build_opening_round_
# readiness`, which now keys season/round readiness off this table).
#
# Versioned exactly like `OpeningRoundRuleRepository` (header + revision,
# `current_revision` pointer): a `'draft'` revision represents an entry that
# has not (or no longer) confirmed; a `'confirmed'` revision is authoritative
# completeness, with confirmation time and actor/reason provenance. There is
# no row at all until an entry's submission is first confirmed. Reopening a
# confirmed submission is a distinct, reason-required operation that writes
# a fresh `'draft'` revision -- a confirmed submission is never silently
# mutated back to draft, and its full history remains inspectable.


@dataclass(frozen=True)
class OpeningRoundSubmission:
    submission_id: str
    season_id: str
    season_entry_id: str
    revision: int
    state: str
    confirmed_at: str | None
    actor_type: str
    actor_id: str | None
    actor_role: str | None
    reason: str | None
    created_at: str


def _submission(row) -> OpeningRoundSubmission:
    return OpeningRoundSubmission(
        row["submission_id"],
        row["season_id"],
        row["season_entry_id"],
        row["revision"],
        row["state"],
        row["confirmed_at"],
        row["actor_type"],
        row["actor_id"],
        row["actor_role"],
        row["reason"],
        row["created_at"],
    )


class OpeningRoundSubmissionRepository:
    """Season-entry scoped Opening Round submission confirmation boundary.
    Every write is attributed to an operator acting as proxy
    (`_ensure_operator`), exactly like `OpeningRoundNominationRepository`."""

    def __init__(self, database: DatabaseConnection):
        self.database = database

    def get(self, season_id: str, season_entry_id: str) -> OpeningRoundSubmission | None:
        row = self.database.execute(
            self._select() + " WHERE m.season_id=? AND m.season_entry_id=?", (season_id, season_entry_id)
        ).fetchone()
        return _submission(row) if row else None

    def is_confirmed(self, season_id: str, season_entry_id: str) -> bool:
        current = self.get(season_id, season_entry_id)
        return current is not None and current.state == "confirmed"

    @staticmethod
    def lock_state(conn: ConnectionLike, season_id: str, season_entry_id: str) -> str | None:
        """Current state (`'draft'`/`'confirmed'`), or `None` if never
        touched, after locking this entry's submission for the remainder of
        the caller's transaction -- see `_lock`'s docstring. Used by
        `OpeningRoundNominationRepository.nominate`/`correct` to refuse
        mutating a nomination for an already-confirmed entry."""
        _, _, state = OpeningRoundSubmissionRepository._lock(conn, season_id, season_entry_id)
        return state

    @staticmethod
    def _lock(conn: ConnectionLike, season_id: str, season_entry_id: str) -> tuple[str, int, str | None]:
        """Atomically get-or-create this entry's submission header row and
        lock it for the remainder of the caller's still-open transaction,
        returning `(submission_id, current_revision, state)` -- `state` is
        `None` if the entry has never had a revision written.

        This is the one shared, always-lockable resource
        `OpeningRoundNominationRepository.nominate`/`correct` and this
        class's own `confirm`/`reopen` all serialize on (issue #133 PR
        review, P1). Without it, a nomination write and a confirmation
        could each read state the other's concurrent commit invalidates --
        e.g. a confirmation landing while a nomination write is mid-flight,
        leaving a confirmed submission whose nominations were mutated
        without the required reopen.

        `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` is used instead of
        a plain `SELECT ... FOR UPDATE` because the header row may not
        exist yet on an entry's very first touch -- there would be nothing
        to lock. The upsert always produces exactly one row and takes the
        same row-level lock a `FOR UPDATE` read would, atomically, on both
        PostgreSQL and SQLite. A header whose `current_revision` is `0` is a
        pure lock placeholder with no matching revision row -- `get()`/
        `history()` still correctly read such an entry as untouched."""
        row = conn.execute(
            "INSERT INTO opening_round_submission (submission_id, season_id, season_entry_id, current_revision, created_at) "
            "VALUES (?, ?, ?, 0, ?) "
            "ON CONFLICT(season_id, season_entry_id) DO UPDATE SET season_id=excluded.season_id "
            "RETURNING submission_id, current_revision",
            (_id(), season_id, season_entry_id, _now()),
        ).fetchone()
        submission_id, current_revision = row["submission_id"], row["current_revision"]
        state = None
        if current_revision:
            state_row = conn.execute(
                "SELECT state FROM opening_round_submission_revision WHERE submission_id=? AND revision=?",
                (submission_id, current_revision),
            ).fetchone()
            state = state_row["state"] if state_row else None
        return submission_id, current_revision, state

    def list_for_season(self, season_id: str) -> list[OpeningRoundSubmission]:
        rows = self.database.execute(self._select() + " WHERE m.season_id=?", (season_id,)).fetchall()
        return [_submission(row) for row in rows]

    def history(self, season_id: str, season_entry_id: str) -> list[OpeningRoundSubmission]:
        rows = self.database.execute(
            self._select(False) + " WHERE m.season_id=? AND m.season_entry_id=? ORDER BY r.revision",
            (season_id, season_entry_id),
        ).fetchall()
        return [_submission(row) for row in rows]

    def confirm(
        self,
        season_id: str,
        season_entry_id: str,
        *,
        actor: ActorContext,
        reason: str | None = None,
    ) -> OpeningRoundSubmission:
        """Confirm an entry's Opening Round submission -- explicitly
        permitting zero, or any partial legal set of, nominations (issue
        #133). Refuses while the season has no accepted Opening Round rule
        at all (PR #134 review): confirming before Opening Round is even
        configured would let a later-accepted rule's readiness silently
        count a confirmation the operator never reviewed against it.
        Idempotent/concurrency-safe: calling this again while the
        submission is already confirmed is a no-op that returns the current
        state without writing a new revision or audit event, and is
        serialized against a concurrent `nominate`/`correct`/`reopen` for
        the same entry via `_lock` (PR #134 review, P1)."""
        _ensure_operator(actor)
        if not OpeningRoundRuleRepository(self.database).list_accepted_for_season(season_id):
            raise OpeningRoundError(
                f"season {season_id} has no accepted Opening Round rule yet; there is nothing to confirm"
            )
        with transaction(self.database) as conn:
            submission_id, current_revision, state = self._lock(conn, season_id, season_entry_id)
            if state == "confirmed":
                return self._get(conn, submission_id)
            revision = current_revision + 1
            conn.execute(
                "UPDATE opening_round_submission SET current_revision=? WHERE submission_id=?",
                (revision, submission_id),
            )
            now = _now()
            conn.execute(
                "INSERT INTO opening_round_submission_revision VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    submission_id,
                    revision,
                    "confirmed",
                    now,
                    actor.actor_type,
                    actor.actor_id,
                    actor.actor_role,
                    reason,
                    now,
                ),
            )
            append_event(
                conn,
                actor=actor,
                action=SUBMISSION_CONFIRMED,
                entity_type=ENTITY_TYPE_SUBMISSION,
                entity_id=submission_id,
                entity_version=str(revision),
                reason=reason,
                after_state={"state": "confirmed", "season_entry_id": season_entry_id},
            )
            return self._get(conn, submission_id)

    def reopen(
        self,
        season_id: str,
        season_entry_id: str,
        *,
        actor: ActorContext,
        reason: str,
    ) -> OpeningRoundSubmission:
        """Audited reopen of a confirmed submission back to draft (issue
        #133 #3): requires a reason, requires the submission to currently be
        confirmed, and writes a new `'draft'` revision rather than mutating
        the confirmed one -- the confirmation this supersedes remains in
        `history()` exactly as it was recorded. Serialized against a
        concurrent `nominate`/`correct`/`confirm` for the same entry via
        `_lock` (PR #134 review, P1)."""
        _ensure_operator(actor)
        if not reason:
            raise OpeningRoundError("reopening a confirmed Opening Round submission requires a reason")
        with transaction(self.database) as conn:
            submission_id, current_revision, state = self._lock(conn, season_id, season_entry_id)
            if state != "confirmed":
                raise OpeningRoundError("only a confirmed Opening Round submission can be reopened")
            revision = current_revision + 1
            conn.execute(
                "UPDATE opening_round_submission SET current_revision=? WHERE submission_id=?",
                (revision, submission_id),
            )
            now = _now()
            conn.execute(
                "INSERT INTO opening_round_submission_revision VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    submission_id,
                    revision,
                    "draft",
                    None,
                    actor.actor_type,
                    actor.actor_id,
                    actor.actor_role,
                    reason,
                    now,
                ),
            )
            append_event(
                conn,
                actor=actor,
                action=SUBMISSION_REOPENED,
                entity_type=ENTITY_TYPE_SUBMISSION,
                entity_id=submission_id,
                entity_version=str(revision),
                reason=reason,
                before_state={"state": "confirmed"},
                after_state={"state": "draft"},
            )
            return self._get(conn, submission_id)

    @staticmethod
    def _select(current: bool = True) -> str:
        join = " AND r.revision=m.current_revision" if current else ""
        return (
            "SELECT m.submission_id, m.season_id, m.season_entry_id, r.* FROM opening_round_submission m "
            "JOIN opening_round_submission_revision r ON r.submission_id=m.submission_id" + join
        )

    def _get(self, conn: ConnectionLike, submission_id: str) -> OpeningRoundSubmission:
        return _submission(conn.execute(self._select() + " WHERE m.submission_id=?", (submission_id,)).fetchone())


# -- Presentation: human-readable rule/round/club labels --------------------
#
# Issue #131: an accepted rule's `afl_club_id`/`afl_opening_round_id`/
# `afl_bye_round_id`/`bbbffl_round_id` are internal identifiers a replay
# operator should never have to read as the *primary* label. This never
# mutates accepted-rule history (see `OpeningRoundRuleRepository`'s
# docstring) -- it only joins already-cached AFL club facts
# (`season_player_pool.afl_team_name`, populated by
# `app.player_pool.PlayerPoolRepository.refresh_player`), the persisted
# `bbbffl_round` label/sequence, and `afl_client.get_rounds` (the same
# `AflDataSource` boundary `app.round_preflight`/`app.round_mapping` already
# use) to *resolve* those identifiers for display. Internal IDs remain
# present on the returned dict alongside the resolved labels -- never
# replaced -- so an existing consumer that reads `rule_id`/`afl_club_id`/etc
# keeps working unchanged.


def _afl_round_label(round_number: int | None) -> str | None:
    if round_number is None:
        return None
    return "Opening Round" if round_number == 0 else f"AFL Round {round_number}"


def _resolve_afl_round_numbers(afl_client, afl_season_id: int) -> dict[int, int]:
    """`{afl_round_id: round_number}` for one AFL season, or `{}` if the
    evidence source cannot currently supply it -- a resolution failure must
    never block the underlying accepted-rule/nomination data from being
    returned, only fall back to showing the raw identifier (see
    `describe_accepted_rules`)."""
    try:
        rounds = afl_client.get_rounds(afl_season_id)
    except Exception:  # noqa: BLE001 -- presentation fallback only, never a domain failure
        return {}
    return {r.round_id: r.round_number for r in rounds}


def resolve_afl_club_name(database, season_id: str, afl_club_id: int | None) -> str | None:
    """Best-effort AFL club display name for one `(season_id, afl_club_id)`.

    There is no standalone club-registry table; every eligible player of a
    club carries the same cached `afl_team_name` in `season_player_pool`
    (see `app.player_pool.PlayerPoolRepository.refresh_player`), so any one
    row for that club in that season resolves the name. Returns `None`
    (never a guess) if no cached player-pool row for that club exists yet."""
    if afl_club_id is None:
        return None
    row = database.execute(
        "SELECT afl_team_name FROM season_player_pool "
        "WHERE season_id=? AND afl_team_id=? AND afl_team_name IS NOT NULL LIMIT 1",
        (season_id, afl_club_id),
    ).fetchone()
    return row["afl_team_name"] if row else None


def describe_accepted_rule(
    rule: OpeningRoundRule, database, afl_client, *, round_number_cache: dict | None = None
) -> dict:
    """One accepted rule, plus resolved presentation fields, e.g. the
    primary operator-facing `display_label`:
    `"GWS Giants — Opening Round → compensating AFL Round 2 → BBBFFL Round 2"`.
    `round_number_cache` lets a caller resolving several rules for the same
    AFL season share one `afl_client.get_rounds` call (see
    `describe_accepted_rules`); a caller describing a single rule may omit
    it."""
    cache = round_number_cache if round_number_cache is not None else {}
    club_name = resolve_afl_club_name(database, rule.season_id, rule.afl_club_id)
    bbbffl_round = (
        database.execute(
            "SELECT label, sequence FROM bbbffl_round WHERE bbbffl_round_id=?", (rule.bbbffl_round_id,)
        ).fetchone()
        if rule.bbbffl_round_id
        else None
    )
    numbers = {}
    if rule.afl_season_id is not None:
        if rule.afl_season_id not in cache:
            cache[rule.afl_season_id] = _resolve_afl_round_numbers(afl_client, rule.afl_season_id)
        numbers = cache[rule.afl_season_id]
    opening_label = _afl_round_label(numbers.get(rule.afl_opening_round_id)) if rule.afl_opening_round_id else None
    bye_label = _afl_round_label(numbers.get(rule.afl_bye_round_id)) if rule.afl_bye_round_id else None
    bbbffl_label = bbbffl_round["label"] if bbbffl_round else None
    club_display = club_name or (f"AFL club {rule.afl_club_id}" if rule.afl_club_id is not None else "Unknown AFL club")
    display_label = (
        f"{club_display} — {opening_label or 'Opening Round (unresolved)'} "
        f"→ compensating {bye_label or 'AFL round (unresolved)'} "
        f"→ {bbbffl_label or 'BBBFFL round (unresolved)'}"
    )
    return {
        **dataclasses.asdict(rule),
        "afl_club_name": club_name,
        "afl_opening_round_label": opening_label,
        "afl_bye_round_label": bye_label,
        "bbbffl_round_label": bbbffl_label,
        "bbbffl_round_sequence": bbbffl_round["sequence"] if bbbffl_round else None,
        "display_label": display_label,
    }


def describe_accepted_rules(database, afl_client, season_id: str) -> list[dict]:
    """`describe_accepted_rule` for every accepted rule in a season, sharing
    one `afl_client.get_rounds` call per distinct AFL season across rules."""
    rules = OpeningRoundRuleRepository(database).list_accepted_for_season(season_id)
    round_number_cache: dict[int, dict[int, int]] = {}
    return [describe_accepted_rule(rule, database, afl_client, round_number_cache=round_number_cache) for rule in rules]


# -- Readiness: confirmation completeness summary ----------------------------
#
# Issue #133: season/round readiness is keyed entirely off explicit
# `OpeningRoundSubmissionRepository` confirmations, never off how many
# eligible clubs/players an entry happens to own. Owning an eligible player,
# or an accepted rule existing for a club represented in an entry's squad,
# never itself implies a required nomination -- a coach could legitimately
# confirm with zero nominations. "Required" here means every season entry in
# the season: each one must explicitly confirm (even with nothing to
# nominate) before this reads ready, exactly the `N/10 teams confirmed`
# presentation issue #133 calls for. Defensive integrity checks (duplicate/
# mismatched/conflicting nominations) are unrelated to and reported
# separately from confirmation completeness -- a confirmed entry whose
# underlying nomination data is later corrupted (a trade-away, a rule
# correction race) still shows as confirmed, but the corruption remains
# independently visible and still holds `is_ready` False. Never redefines
# what an accepted rule/nomination is and never mutates rule/nomination/
# submission history; this is a read model only. Intended to be shared, not
# duplicated, by Season Centre and round-preparation readiness displays (see
# `app.season_centre.build_season_centre` and
# `app.round_preflight.build_round_preflight`).


@dataclass(frozen=True)
class OpeningRoundEntryReadiness:
    season_entry_id: str
    team_name: str
    is_confirmed: bool
    confirmed_at: str | None
    confirmed_by_actor_id: str | None
    confirmed_by_actor_role: str | None
    nomination_count: int


@dataclass(frozen=True)
class OpeningRoundReadiness:
    season_id: str
    total_entries: int
    total_confirmed: int
    entries: tuple[OpeningRoundEntryReadiness, ...]
    duplicate_nominations: tuple[dict, ...]
    mismatched_nominations: tuple[dict, ...]
    conflicting_nominations: tuple[dict, ...]
    is_ready: bool

    def for_entry(self, season_entry_id: str) -> OpeningRoundEntryReadiness | None:
        """This one entry's slice of a season-wide readiness result -- what
        the represented-entry-scoped Opening Round Operations page shows,
        without exposing any other entry's ownership/nomination state."""
        return next((entry for entry in self.entries if entry.season_entry_id == season_entry_id), None)


def build_opening_round_readiness(database, season_id: str) -> OpeningRoundReadiness:
    rules = OpeningRoundRuleRepository(database).list_accepted_for_season(season_id)
    entry_rows = database.execute(
        "SELECT e.season_entry_id, n.team_name FROM season_entry e "
        "JOIN season_entry_team_name_history n ON n.season_entry_id=e.season_entry_id AND n.ended_at IS NULL "
        "WHERE e.season_id=? ORDER BY e.season_entry_id",
        (season_id,),
    ).fetchall()
    team_names = {row["season_entry_id"]: row["team_name"] for row in entry_rows}

    # Defensive integrity diagnostics only -- these never feed into
    # `is_confirmed`/completeness below (see this section's docstring).
    # Queried by `rule_id` directly, not by the rule's own current
    # `bbbffl_round_id`, so a nomination whose persisted target round has
    # drifted from its rule's target round is still found here.
    duplicates: list[dict] = []
    mismatched: list[dict] = []
    conflicting: list[dict] = []
    for rule in rules:
        rows = database.execute(
            "SELECT * FROM opening_round_nomination WHERE rule_id=? ORDER BY season_entry_id, position",
            (rule.rule_id,),
        ).fetchall()
        nominations = [_nomination(row) for row in rows]
        by_entry: dict[str, list[OpeningRoundNomination]] = {}
        for nomination in nominations:
            by_entry.setdefault(nomination.season_entry_id, []).append(nomination)
            # Checked against the player's *current* club and *current*
            # owner, not merely the state true at nomination time: a trade
            # or release after nomination must surface here too.
            player = database.execute(
                "SELECT p.afl_team_id, o.season_entry_id AS current_owner_id "
                "FROM season_player_pool p "
                "LEFT JOIN player_ownership_period o "
                "ON o.season_player_id=p.season_player_id AND o.released_at IS NULL "
                "WHERE p.season_player_id=?",
                (nomination.season_player_id,),
            ).fetchone()
            is_mismatched = (
                player is None
                or player["afl_team_id"] != rule.afl_club_id
                or player["current_owner_id"] != nomination.season_entry_id
            )
            is_conflicting = nomination.bbbffl_round_id != rule.bbbffl_round_id
            if is_mismatched:
                mismatched.append(
                    {
                        "nomination_id": nomination.nomination_id,
                        "rule_id": rule.rule_id,
                        "season_entry_id": nomination.season_entry_id,
                        "season_player_id": nomination.season_player_id,
                    }
                )
            if is_conflicting:
                conflicting.append(
                    {
                        "nomination_id": nomination.nomination_id,
                        "rule_id": rule.rule_id,
                        "season_entry_id": nomination.season_entry_id,
                    }
                )
        for entry_id, entry_rows_ in by_entry.items():
            if len(entry_rows_) > 1:
                duplicates.append(
                    {
                        "rule_id": rule.rule_id,
                        "season_entry_id": entry_id,
                        "nomination_ids": [row.nomination_id for row in entry_rows_],
                    }
                )

    submissions = {
        submission.season_entry_id: submission
        for submission in OpeningRoundSubmissionRepository(database).list_for_season(season_id)
    }
    count_rows = database.execute(
        "SELECT season_entry_id, COUNT(*) AS n FROM opening_round_nomination WHERE season_id=? GROUP BY season_entry_id",
        (season_id,),
    ).fetchall()
    nomination_counts = {row["season_entry_id"]: row["n"] for row in count_rows}

    entries = []
    for entry_id in sorted(team_names):
        submission = submissions.get(entry_id)
        confirmed = submission is not None and submission.state == "confirmed"
        entries.append(
            OpeningRoundEntryReadiness(
                season_entry_id=entry_id,
                team_name=team_names.get(entry_id, "Unknown team"),
                is_confirmed=confirmed,
                confirmed_at=submission.confirmed_at if confirmed else None,
                confirmed_by_actor_id=submission.actor_id if confirmed else None,
                confirmed_by_actor_role=submission.actor_role if confirmed else None,
                nomination_count=nomination_counts.get(entry_id, 0),
            )
        )
    total_confirmed = sum(1 for entry in entries if entry.is_confirmed)
    is_ready = total_confirmed == len(entries) and not duplicates and not mismatched and not conflicting
    return OpeningRoundReadiness(
        season_id=season_id,
        total_entries=len(entries),
        total_confirmed=total_confirmed,
        entries=tuple(entries),
        duplicate_nominations=tuple(duplicates),
        mismatched_nominations=tuple(mismatched),
        conflicting_nominations=tuple(conflicting),
        is_ready=is_ready,
    )
