"""Audited Scorer/Admin adjudication of a missed *initial* weekly-lineup
submission after lockout has already activated (issue #146).

## The narrow case this covers

A coach saves a private draft before a lockout, but never creates an
authoritative submission before an activated trigger closes ordinary
submission for one or more positions. Ordinary submission then correctly
refuses the draft's now-locked players (`app.lockouts.LockedSelectionError`,
issue #144), and issue #137's locked-lineup correction workflow correctly
has no prior authoritative submission to correct (`NoEffectiveSubmissionError`)
-- there is no in-app path back to a submitted lineup at all.

**League consultation/quorum happens entirely outside this application.**
This module implements neither voting, ballot counting nor approval
collection: the authorised Scorer/Administrator records the externally
reached decision -- accept the evidenced pre-lockout draft, or apply the
established carry-forward fallback -- as a substantive reason, in exactly
the same style as issue #137's locked-lineup correction reason. Nothing
here implies the application itself approved the request or conducted any
consultation.

## Relationship to `app.lineups`/`app.lockouts`

`app.lineups.WeeklyLineupRepository.submit_adjudicated_first_submission` is
the domain primitive: it atomically creates a lineup's *first* authoritative
submission (always version 1) through a distinct, audited source, under the
same row lock/CAS discipline every other submission path uses. It knows
nothing about AFL matches or lockout triggers (see its own docstring and
app/lineups.py's module docstring on why that import direction is
disallowed) -- this module supplies the two lockout/draft-evidence-aware
callbacks it needs (`resolve_positions`/`record_adjudication`), reusing
`app.lockouts.LockoutRepository`'s three small public wrappers added for
this issue (`materialize_round_triggers`/`trigger_coverage_locked`/
`evaluate_draft_position_locked`) rather than duplicating any lock-decision
logic.

## Evidence model (see also this issue's PR description)

`app.lineups.WeeklyLineupRepository.get_draft_slot_provenance` /
`save_draft` (issue #146) give each position of a private draft its own
`updated_at`/actor columns, advanced only when that position's *value*
actually changes. `_resolve_evidenced_positions` below is the one place
that turns that evidence, plus each activated trigger's durable
`effective_lock_at`, into a per-position decision:

- a position an active Opening Round nomination governs always resolves to
  the nominated player (issue #69's existing authoritative rule, never
  reinterpreted here);
- a position with no current draft selection is simply vacant -- there is
  nothing to prove wrong about an empty slot;
- a position that is not currently locked (`LockState.EDITABLE`) resolves
  to whatever the current draft holds -- it remains completable through the
  ordinary live submission workflow after this capture regardless;
- a position that *is* locked or indeterminate resolves to the draft's
  current value only if that position's own `updated_at` is at or before
  the covering trigger's `effective_lock_at` (`evidence_status=
  "proven_pre_lock"`); otherwise it resolves to vacant
  (`evidence_status="unproven_defaulted_vacant"`) -- a post-lockout edit to
  a still-open position can never be misrepresented as pre-lockout evidence
  for an unrelated, already-locked position, and an operator can never
  substitute, move or add a locked player beyond what this evidence proves.

This evaluation always runs twice: once outside any transaction, for the
human-readable preview a Scorer/Admin UI shows before confirmation
(`describe_candidate`), and once again *inside* `submit_adjudicated_first_
submission`'s own transaction, reading the draft and trigger coverage fresh
on that transaction's own connection -- so a trigger that activates, or a
draft edit that lands, between the preview and the confirmed decision is
always caught by the second, authoritative evaluation rather than trusting
the first.

## Resolution B: carry-forward fallback

`apply_carry_forward_fallback` sources the current round's first
authoritative submission from the previous round's effective submitted
lineup via `app.carry_forward.CarryForwardService.resolve_source` -- the
same established BBBFFL carry-forward rule ordinary carry-forward uses --
and re-validates the source is unchanged *inside* the same transaction
(`require_unchanged`, exactly as `app.carry_forward.CarryForwardService.
carry_forward` already does). It never reads or merges anything from the
entry's own rejected private draft: the only positions ever added on top
of the copied source are the current round's own active Opening Round
deferred nominations, which -- like Resolution A -- are always the
authoritative value regardless of what any draft or carry-forward source
holds.
"""

from dataclasses import dataclass
from uuid import uuid4

from app.audit import ENTITY_TYPE_LINEUP, LINEUP_ADJUDICATED, ActorContext, append_event
from app.carry_forward import CARRY_FORWARD_SOURCE_TYPE, CarryForwardService, NoCarryForwardSourceError
from app.coach_lineup import CoachLineupService
from app.db import _for_update_suffix
from app.lineups import (
    ADJUDICATED_CARRY_FORWARD_SOURCE_TYPE,
    ADJUDICATED_LATE_CAPTURE_SOURCE_TYPE,
    POSITIONS,
    LineupConflictError,
    LineupIntegrityError,
)
from app.lockouts import LockState, _parse_instant
from app.season import _now

ADJUDICATION_ACTOR_ROLES = frozenset({"scorer", "admin", "replay_operator"})

DECISION_ACCEPT_EVIDENCED_DRAFT = "accept_evidenced_draft"
DECISION_APPLY_CARRY_FORWARD = "apply_carry_forward"
DECISION_TYPES = frozenset({DECISION_ACCEPT_EVIDENCED_DRAFT, DECISION_APPLY_CARRY_FORWARD})


class LineupAdjudicationError(LineupIntegrityError):
    """Base class for this module's domain errors."""


class UnauthorizedAdjudicationActorError(LineupAdjudicationError):
    """The supplied actor is not a recognised scorer/admin/replay-operator
    operator context -- see this module's docstring and `_ensure_
    adjudication_actor`. Coach and ordinary delegated/proxy authority
    (`app.lineup_proxy`) are never sufficient here."""


class RoundNotEligibleForAdjudicationError(LineupAdjudicationError):
    """One or more of this workflow's eligibility conditions is not met:
    the round is not `live`/`review`, an effective submission already
    exists, or no lockout trigger has yet activated for this round -- see
    this module's docstring's eligibility gate."""


class NoActivatedTriggerError(LineupAdjudicationError):
    """Re-checked atomically inside the adjudication transaction itself
    (not only by the pre-check in `_eligibility`): a round with no
    activated lockout trigger has nothing for this workflow to adjudicate
    -- ordinary submission remains the correct path."""


def _ensure_adjudication_actor(actor: ActorContext) -> None:
    if actor.actor_type != "anonymous_operator" or actor.actor_role not in ADJUDICATION_ACTOR_ROLES:
        raise UnauthorizedAdjudicationActorError(
            "adjudication of a missed initial submission requires an anonymous_operator actor with actor_role "
            f"scorer, admin, or replay_operator, got actor_type={actor.actor_type!r} actor_role={actor.actor_role!r}"
        )


@dataclass(frozen=True)
class AdjudicationSlotRecord:
    """One scoring position's resolved value and the evidence it traces
    back to -- returned both by the preview (`describe_candidate`) and
    persisted verbatim into `lineup_adjudication_slot` once confirmed."""

    position: str
    season_player_id: str | None
    was_locked: bool
    evidence_status: str
    lock_reason: str | None
    afl_match_id: int | None
    effective_lock_at: str | None
    observed_status: str | None
    evidence_saved_at: str | None


@dataclass(frozen=True)
class ActivatedTriggerView:
    trigger_key: str
    trigger_type: str
    activation_reason: str | None
    activating_afl_match_id: int | None
    effective_lock_at: str | None


@dataclass(frozen=True)
class CarryForwardPreview:
    source_bbbffl_round_id: str
    source_lineup_id: str
    source_submission_version: int
    positions: dict


@dataclass(frozen=True)
class AdjudicationCandidate:
    """The human-readable, before-confirmation read model a Scorer/Admin
    adjudication UI needs (issue #146): whether this workflow is even in
    scope, the activated trigger evidence, the evidenced-draft preview, and
    the carry-forward preview -- so a Scorer never has to interpret opaque
    internal state to choose a resolution."""

    lineup_id: str | None
    season_id: str
    competition_id: str
    bbbffl_round_id: str
    season_entry_id: str
    round_state: str
    eligible: bool
    ineligible_reason: str | None
    activated_triggers: tuple[ActivatedTriggerView, ...]
    draft_revision: int | None
    draft_updated_at: str | None
    evidenced_preview: tuple[AdjudicationSlotRecord, ...] | None
    carry_forward_preview: CarryForwardPreview | None


@dataclass(frozen=True)
class LineupAdjudication:
    """The full, immutable audited record of one adjudication decision
    (issue #146) -- structurally parallel to `app.lineups.LineupCorrection`
    (issue #137)."""

    adjudication_id: str
    lineup_id: str
    bbbffl_round_id: str
    season_entry_id: str
    decision_type: str
    submission_version: int
    actor_type: str
    actor_id: str | None
    actor_role: str | None
    reason: str
    decided_at: str
    correlation_id: str
    source_draft_revision: int | None
    source_draft_saved_at: str | None
    source_bbbffl_round_id: str | None
    source_lineup_id: str | None
    source_submission_version: int | None
    slots: tuple[AdjudicationSlotRecord, ...]


class LineupAdjudicationService:
    def __init__(self, database, afl_client):
        self.database = database
        self._coach_lineup = CoachLineupService(database, afl_client)
        self.lineups = self._coach_lineup.lineups
        self.lockouts = self._coach_lineup.lockouts
        self.match_facts = self._coach_lineup.match_facts
        self.pool = self._coach_lineup.pool
        self.ownership = self._coach_lineup.ownership
        self.nominations = self._coach_lineup.nominations
        self._carry_forward = CarryForwardService(database, afl_client)

    # -- Eligibility ---------------------------------------------------------

    def _lineup_header(self, season_id, competition_id, bbbffl_round_id, season_entry_id):
        """Read-only `(lineup_id, effective_submission_version)` lookup --
        `(None, 0)` if this entry has never touched this round's lineup at
        all. Deliberately distinct from `WeeklyLineupRepository.
        get_or_create_header`, which *creates* an empty draft header as a
        side effect: an eligibility check must never conjure a draft into
        existence merely by being asked whether one exists (issue #146
        Codex review) -- `accept_evidenced_draft` in particular relies on
        this to tell "no draft was ever saved" apart from "a real draft
        exists but every position happens to be vacant"."""
        row = self.database.execute(
            "SELECT lineup_id, effective_submission_version FROM weekly_lineup "
            "WHERE season_id=? AND competition_id=? AND bbbffl_round_id=? AND season_entry_id=?",
            (season_id, competition_id, bbbffl_round_id, season_entry_id),
        ).fetchone()
        if row is None:
            return None, 0
        return row["lineup_id"], row["effective_submission_version"] or 0

    def _eligibility(self, season_id, competition_id, bbbffl_round_id, season_entry_id, *, evaluation_at=None):
        """Non-transactional pre-check, for a fast/clear refusal and for the
        preview UI. Every fact this also depends on is re-validated
        atomically, inside the adjudication's own transaction, by
        `submit_adjudicated_first_submission` and this module's own
        `resolve_positions` callbacks -- this is a courtesy check only, per
        the issue's "must be concurrency-safe rather than merely a UI
        check" requirement. `lineup_id` is `None` if this entry has never
        touched this round's lineup at all (see `_lineup_header`) -- a
        caller that needs a real row to write into (`apply_carry_forward_
        fallback`) creates one itself via `get_or_create_header`, only once
        it has confirmed eligibility.

        `evaluation_at`, like every other entry point in `app.lockouts`/
        `app.carry_forward`, is an explicit override for tests/replay --
        production callers leave it `None` and get the real wall clock (or
        a replay client's own clock, via `self.match_facts`)."""
        lineup_id, effective_version = self._lineup_header(season_id, competition_id, bbbffl_round_id, season_entry_id)
        round_row = self.database.execute(
            "SELECT state FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?", (bbbffl_round_id,)
        ).fetchone()
        round_state = round_row["state"] if round_row else "unknown"
        reasons = []
        if round_state not in ("live", "review"):
            reasons.append(f"round is {round_state!r}; adjudication requires the round to be live or review")
        if effective_version:
            reasons.append(
                "an effective authoritative submission already exists; use the locked-lineup correction "
                "workflow (issue #137) instead"
            )
        triggers = self.lockouts.describe_triggers(
            bbbffl_round_id, match_facts=self.match_facts, evaluation_at=evaluation_at
        )
        activated = tuple(t for t in triggers if t.activated)
        if not activated:
            reasons.append("no lockout trigger has activated for this round yet")
        return lineup_id, effective_version, round_state, activated, reasons

    # -- Read model ------------------------------------------------------

    def describe_candidate(
        self,
        season_id: str,
        competition_id: str,
        bbbffl_round_id: str,
        season_entry_id: str,
        *,
        evaluation_at=None,
    ) -> AdjudicationCandidate:
        lineup_id, _effective_version, round_state, activated, reasons = self._eligibility(
            season_id, competition_id, bbbffl_round_id, season_entry_id, evaluation_at=evaluation_at
        )
        draft = self.lineups.get_draft(season_id, competition_id, bbbffl_round_id, season_entry_id)
        evidenced_preview = None
        if draft is not None:
            provenance = self.lineups.get_draft_slot_provenance(lineup_id)
            at = self.lockouts.materialize_round_triggers(
                bbbffl_round_id, match_facts=self.match_facts, evaluation_at=evaluation_at
            )
            _resolved, slots = self._resolve_evidenced_positions(
                self.database,
                bbbffl_round_id,
                season_entry_id,
                draft.positions,
                {position: slot.updated_at for position, slot in provenance.items()},
                evaluation_at=at,
            )
            evidenced_preview = tuple(slots)
        carry_forward_preview = None
        try:
            source = self._carry_forward.resolve_source(season_id, competition_id, bbbffl_round_id, season_entry_id)
        except LineupIntegrityError:
            source = None
        if source is not None:
            carry_forward_preview = CarryForwardPreview(
                source.source_bbbffl_round_id, source.source_lineup_id, source.source_version, dict(source.positions)
            )
        return AdjudicationCandidate(
            lineup_id=lineup_id,
            season_id=season_id,
            competition_id=competition_id,
            bbbffl_round_id=bbbffl_round_id,
            season_entry_id=season_entry_id,
            round_state=round_state,
            eligible=not reasons,
            ineligible_reason="; ".join(reasons) or None,
            activated_triggers=tuple(
                ActivatedTriggerView(
                    t.trigger_key, t.trigger_type, t.activation_reason, t.activating_afl_match_id, t.effective_lock_at
                )
                for t in activated
            ),
            draft_revision=draft.revision if draft else None,
            draft_updated_at=draft.updated_at if draft else None,
            evidenced_preview=evidenced_preview,
            carry_forward_preview=carry_forward_preview,
        )

    def get_adjudication_for_lineup(self, lineup_id: str) -> LineupAdjudication | None:
        """A lineup has at most one adjudication record, since this
        workflow only ever creates a lineup's first submission (version 1,
        `ck_adjudication_first_submission_only`)."""
        row = self.database.execute("SELECT * FROM lineup_adjudication WHERE lineup_id=?", (lineup_id,)).fetchone()
        return self._to_adjudication(row) if row else None

    def _to_adjudication(self, row) -> LineupAdjudication:
        slot_rows = self.database.execute(
            "SELECT * FROM lineup_adjudication_slot WHERE adjudication_id=? ORDER BY position",
            (row["adjudication_id"],),
        ).fetchall()
        slots = tuple(
            AdjudicationSlotRecord(
                s["position"],
                s["season_player_id"],
                bool(s["was_locked"]),
                s["evidence_status"],
                s["lock_reason"],
                s["afl_match_id"],
                s["effective_lock_at"],
                s["observed_status"],
                s["evidence_saved_at"],
            )
            for s in slot_rows
        )
        return LineupAdjudication(
            row["adjudication_id"],
            row["lineup_id"],
            row["bbbffl_round_id"],
            row["season_entry_id"],
            row["decision_type"],
            row["submission_version"],
            row["actor_type"],
            row["actor_id"],
            row["actor_role"],
            row["reason"],
            row["decided_at"],
            row["correlation_id"],
            row["source_draft_revision"],
            row["source_draft_saved_at"],
            row["source_bbbffl_round_id"],
            row["source_lineup_id"],
            row["source_submission_version"],
            slots,
        )

    # -- Shared per-position evidence evaluation ------------------------------

    def _resolve_evidenced_positions(
        self, conn, bbbffl_round_id, season_entry_id, draft_positions, draft_updated_at_by_position, *, evaluation_at
    ):
        """Shared by the non-transactional preview and the transactional
        `resolve_positions` callback below -- see this module's docstring's
        'Evidence model'. `conn` need only support `.execute(...)`: either
        `self.database` (preview) or an open transaction connection
        (confirmed capture)."""
        nominated = self.nominations.active_positions_locked(conn, bbbffl_round_id, season_entry_id)
        matches = self.match_facts.matches_for(bbbffl_round_id)
        coverage = self.lockouts.trigger_coverage_locked(conn, bbbffl_round_id)
        resolved = {}
        slots = []
        for position in POSITIONS:
            if position in nominated:
                nominated_player = nominated[position]
                resolved[position] = nominated_player
                slots.append(
                    AdjudicationSlotRecord(
                        position, nominated_player, False, "opening_round_deferred", None, None, None, None, None
                    )
                )
                continue
            draft_value = draft_positions.get(position)
            evidence_saved_at = draft_updated_at_by_position.get(position)
            if draft_value is None:
                resolved[position] = None
                slots.append(
                    AdjudicationSlotRecord(position, None, False, "vacant", None, None, None, None, evidence_saved_at)
                )
                continue
            lock_eval = self.lockouts.evaluate_draft_position_locked(
                conn, position, draft_value, evaluation_at=evaluation_at, matches=matches, coverage=coverage
            )
            if lock_eval.state == LockState.EDITABLE:
                resolved[position] = draft_value
                slots.append(
                    AdjudicationSlotRecord(
                        position,
                        draft_value,
                        False,
                        "editable_current_draft",
                        lock_eval.reason,
                        lock_eval.afl_match_id,
                        lock_eval.effective_lock_at,
                        lock_eval.observed_status,
                        evidence_saved_at,
                    )
                )
                continue
            # The real boundary this position's evidence must predate is the
            # *trigger's* own activation instant, never
            # `lock_eval.effective_lock_at` (always the selected player's
            # own resolved match start -- see `_evaluate_position`). Those
            # coincide only for an ordinary single-match selective trigger
            # fired by its own match; they diverge for a main trigger (locks
            # every remaining position immediately, regardless of that
            # position's own match time) and for a grouped selective trigger
            # fired by an earlier match than this position's own (issue #146
            # Codex review) -- either would otherwise let a post-lock edit
            # slip through as apparently pre-lock evidence.
            trigger_lock_at = (
                self.lockouts.trigger_activation_instant(conn, bbbffl_round_id, lock_eval.afl_match_id)
                if lock_eval.afl_match_id is not None
                else None
            )
            proven = (
                evidence_saved_at is not None
                and trigger_lock_at is not None
                and _parse_instant(evidence_saved_at) <= _parse_instant(trigger_lock_at)
            )
            resolved_value = draft_value if proven else None
            slots.append(
                AdjudicationSlotRecord(
                    position,
                    resolved_value,
                    True,
                    "proven_pre_lock" if proven else "unproven_defaulted_vacant",
                    lock_eval.reason,
                    lock_eval.afl_match_id,
                    trigger_lock_at,
                    lock_eval.observed_status,
                    evidence_saved_at,
                )
            )
            resolved[position] = resolved_value
        return resolved, slots

    # -- Resolution A: accept evidenced pre-lockout draft ---------------------

    def accept_evidenced_draft(
        self,
        season_id: str,
        competition_id: str,
        bbbffl_round_id: str,
        season_entry_id: str,
        *,
        actor,
        reason,
        evaluation_at=None,
    ):
        """Create the round's first authoritative submission
        (`source_type='scorer_late_capture'`) from whatever the eligible
        private draft's per-position evidence actually proves predates each
        already-locked position's effective lock instant -- see this
        module's docstring's 'Evidence model'. Positions the draft's own
        evidence cannot support default to vacant, never to any operator-
        supplied substitute (issue #146's "the operator must not be able
        to move, substitute or add a locked player beyond that evidence").

        `evaluation_at` is the same explicit test/replay override every
        other `app.lockouts` entry point accepts (`None` in production)."""
        _ensure_adjudication_actor(actor)
        if not reason or not reason.strip():
            raise LineupAdjudicationError(
                "an adjudicated evidenced-draft capture requires a substantive Scorer reason recording the "
                "externally reached league decision"
            )
        _lineup_id, _effective_version, _round_state, _activated, reasons = self._eligibility(
            season_id, competition_id, bbbffl_round_id, season_entry_id, evaluation_at=evaluation_at
        )
        if reasons:
            raise RoundNotEligibleForAdjudicationError("; ".join(reasons))
        # A private draft must already exist -- `_eligibility` deliberately
        # never creates one (see `_lineup_header`'s docstring). Without this
        # check, an entry that never saved anything at all would still
        # "pass" eligibility and this method would go on to capture an
        # empty, synthetic position map as though it were genuine pre-lock
        # evidence (issue #146 Codex review).
        existing_draft = self.lineups.get_draft(season_id, competition_id, bbbffl_round_id, season_entry_id)
        if existing_draft is None:
            raise RoundNotEligibleForAdjudicationError(
                "no private draft was ever saved for this entry in this round; there is no evidence to "
                "capture -- consider the carry-forward fallback instead"
            )
        lineup_id = existing_draft.lineup_id

        # Materialized here, deliberately *before* `submit_adjudicated_
        # first_submission` opens its own transaction below -- exactly the
        # same ordering `LockGuard.materialize()` requires ahead of
        # `WeeklyLineupRepository.submit`'s transaction (see app/lockouts.py's
        # module docstring, 'Concurrency'). Calling this again from *inside*
        # `resolve_positions` (which runs on `conn`, already holding this
        # transaction's row lock) would open a second, standalone write
        # transaction nested inside the first -- harmless on PostgreSQL's
        # row-level locking, but a `database is locked` hazard on SQLite's
        # single-writer model whenever there is a genuine new activation to
        # record. `resolve_positions` below only ever *reads* trigger
        # coverage on `conn` (`trigger_coverage_locked`), never re-materializes.
        at = self.lockouts.materialize_round_triggers(
            bbbffl_round_id, match_facts=self.match_facts, evaluation_at=evaluation_at
        )
        correlation_id = str(uuid4())
        captured: dict = {}

        def resolve_positions(conn, lineup_row):
            coverage = self.lockouts.trigger_coverage_locked(conn, bbbffl_round_id)
            if not (coverage.main_activated or coverage.locked_match_ids):
                raise NoActivatedTriggerError(
                    f"round {bbbffl_round_id} has no activated lockout trigger; there is nothing to adjudicate"
                )
            draft_rows = conn.execute(
                "SELECT position, season_player_id, updated_at FROM weekly_lineup_draft_slot WHERE lineup_id=?",
                (lineup_row["lineup_id"],),
            ).fetchall()
            draft_positions = {row["position"]: row["season_player_id"] for row in draft_rows}
            draft_updated_at = {row["position"]: row["updated_at"] for row in draft_rows}
            resolved, slots = self._resolve_evidenced_positions(
                conn, bbbffl_round_id, season_entry_id, draft_positions, draft_updated_at, evaluation_at=at
            )
            captured["slots"] = slots
            captured["draft_revision"] = lineup_row["draft_revision"]
            captured["draft_updated_at"] = lineup_row["updated_at"]
            captured["resolved"] = resolved
            return resolved

        def record_adjudication(conn, version):
            adjudication_id, decided_at = str(uuid4()), _now()
            conn.execute(
                "INSERT INTO lineup_adjudication VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    adjudication_id,
                    lineup_id,
                    bbbffl_round_id,
                    season_entry_id,
                    DECISION_ACCEPT_EVIDENCED_DRAFT,
                    version,
                    actor.actor_type,
                    actor.actor_id,
                    actor.actor_role,
                    reason,
                    decided_at,
                    correlation_id,
                    captured["draft_revision"],
                    captured["draft_updated_at"],
                    None,
                    None,
                    None,
                ),
            )
            for slot in captured["slots"]:
                conn.execute(
                    "INSERT INTO lineup_adjudication_slot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        adjudication_id,
                        slot.position,
                        slot.season_player_id,
                        int(slot.was_locked),
                        slot.evidence_status,
                        slot.lock_reason,
                        slot.afl_match_id,
                        slot.effective_lock_at,
                        slot.observed_status,
                        slot.evidence_saved_at,
                    ),
                )
            append_event(
                conn,
                actor=actor,
                action=LINEUP_ADJUDICATED,
                entity_type=ENTITY_TYPE_LINEUP,
                entity_id=lineup_id,
                entity_version=str(version),
                correlation_id=correlation_id,
                reason=reason,
                after_state={"decision_type": DECISION_ACCEPT_EVIDENCED_DRAFT, "positions": captured["resolved"]},
                payload={
                    "adjudication_id": adjudication_id,
                    "locked_positions_accepted": [
                        s.position for s in captured["slots"] if s.was_locked and s.evidence_status == "proven_pre_lock"
                    ],
                    "locked_positions_unproven": [
                        s.position
                        for s in captured["slots"]
                        if s.was_locked and s.evidence_status == "unproven_defaulted_vacant"
                    ],
                },
            )

        submission, correlation_id = self.lineups.submit_adjudicated_first_submission(
            lineup_id,
            actor=actor,
            reason=reason,
            source_type=ADJUDICATED_LATE_CAPTURE_SOURCE_TYPE,
            source_detail={"lineup_id": lineup_id},
            resolve_positions=resolve_positions,
            record_adjudication=record_adjudication,
            correlation_id=correlation_id,
        )
        return submission, self.get_adjudication_for_lineup(lineup_id)

    # -- Resolution B: apply carry-forward fallback ---------------------------

    def apply_carry_forward_fallback(
        self,
        season_id: str,
        competition_id: str,
        bbbffl_round_id: str,
        season_entry_id: str,
        *,
        actor,
        reason,
        evaluation_at=None,
    ):
        """Create the round's first authoritative submission
        (`source_type='scorer_adjudicated_carry_forward'`) sourced from the
        previous round's effective submitted lineup, exactly like
        `app.carry_forward.CarryForwardService.carry_forward` -- but
        through this adjudicated path, which never invokes an ordinary
        `app.lockouts.LockGuard`'s rejecting callable (issue #146:
        "create the current round's first authoritative submission through
        this adjudicated path even though normal carry-forward would now
        collide with activated locks"). Never reads or merges the entry's
        own rejected private draft."""
        _ensure_adjudication_actor(actor)
        if not reason or not reason.strip():
            raise LineupAdjudicationError(
                "an adjudicated carry-forward fallback requires a substantive Scorer reason recording the "
                "externally reached league decision"
            )
        _lineup_id, _effective_version, _round_state, _activated, reasons = self._eligibility(
            season_id, competition_id, bbbffl_round_id, season_entry_id, evaluation_at=evaluation_at
        )
        if reasons:
            raise RoundNotEligibleForAdjudicationError("; ".join(reasons))
        source = self._carry_forward.resolve_source(season_id, competition_id, bbbffl_round_id, season_entry_id)
        if source is None:
            raise NoCarryForwardSourceError(
                f"no previous submitted lineup exists for entry {season_entry_id} in competition "
                f"{competition_id} before round {bbbffl_round_id}; the carry-forward fallback is not available"
            )
        # Unlike Resolution A, this never needs a draft to already exist --
        # `_eligibility` deliberately never creates the header row (see
        # `_lineup_header`), so it is created here, now that eligibility and
        # source-availability are both confirmed.
        lineup_id, _ = self.lineups.get_or_create_header(season_id, competition_id, bbbffl_round_id, season_entry_id)

        correlation_id = str(uuid4())
        captured: dict = {}

        def resolve_positions(conn, lineup_row):
            coverage = self.lockouts.trigger_coverage_locked(conn, bbbffl_round_id)
            if not (coverage.main_activated or coverage.locked_match_ids):
                raise NoActivatedTriggerError(
                    f"round {bbbffl_round_id} has no activated lockout trigger; there is nothing to adjudicate"
                )
            # Re-validated atomically, inside this same transaction, so a
            # resubmission of the source round between `resolve_source`
            # above and this commit is caught rather than silently carried
            # forward stale -- the same guarantee
            # `CarryForwardService.carry_forward`'s own `require_unchanged`
            # gives its ordinary path.
            source_row = conn.execute(
                "SELECT effective_submission_version FROM weekly_lineup WHERE lineup_id=?"
                + _for_update_suffix(self.database),
                (source.source_lineup_id,),
            ).fetchone()
            if source_row is None or (source_row["effective_submission_version"] or 0) != source.source_version:
                raise LineupConflictError("carry-forward source lineup was resubmitted; re-resolve and retry")
            positions = dict(source.positions)
            nominated = self.nominations.active_positions_locked(conn, bbbffl_round_id, season_entry_id)
            positions.update(nominated)
            captured["positions"] = positions
            captured["deferred_positions"] = set(nominated)
            return positions

        def record_adjudication(conn, version):
            adjudication_id, decided_at = str(uuid4()), _now()
            conn.execute(
                "INSERT INTO lineup_adjudication VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    adjudication_id,
                    lineup_id,
                    bbbffl_round_id,
                    season_entry_id,
                    DECISION_APPLY_CARRY_FORWARD,
                    version,
                    actor.actor_type,
                    actor.actor_id,
                    actor.actor_role,
                    reason,
                    decided_at,
                    correlation_id,
                    None,
                    None,
                    source.source_bbbffl_round_id,
                    source.source_lineup_id,
                    source.source_version,
                ),
            )
            for position in POSITIONS:
                deferred = position in captured["deferred_positions"]
                conn.execute(
                    "INSERT INTO lineup_adjudication_slot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        adjudication_id,
                        position,
                        captured["positions"].get(position),
                        0,
                        "opening_round_deferred" if deferred else "carry_forward_source",
                        None,
                        None,
                        None,
                        None,
                        None,
                    ),
                )
            append_event(
                conn,
                actor=actor,
                action=LINEUP_ADJUDICATED,
                entity_type=ENTITY_TYPE_LINEUP,
                entity_id=lineup_id,
                entity_version=str(version),
                correlation_id=correlation_id,
                reason=reason,
                after_state={"decision_type": DECISION_APPLY_CARRY_FORWARD, "positions": captured["positions"]},
                payload={
                    "adjudication_id": adjudication_id,
                    "source_bbbffl_round_id": source.source_bbbffl_round_id,
                    "source_lineup_id": source.source_lineup_id,
                    "source_submission_version": source.source_version,
                },
            )

        submission, correlation_id = self.lineups.submit_adjudicated_first_submission(
            lineup_id,
            actor=actor,
            reason=reason,
            source_type=ADJUDICATED_CARRY_FORWARD_SOURCE_TYPE,
            source_detail={
                "source_bbbffl_round_id": source.source_bbbffl_round_id,
                "source_lineup_id": source.source_lineup_id,
                "source_version": source.source_version,
                "carry_forward_rule": CARRY_FORWARD_SOURCE_TYPE,
            },
            resolve_positions=resolve_positions,
            record_adjudication=record_adjudication,
            correlation_id=correlation_id,
        )
        return submission, self.get_adjudication_for_lineup(lineup_id)
