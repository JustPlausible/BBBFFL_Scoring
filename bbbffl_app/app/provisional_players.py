"""Provisional player creation, Coach nomination and canonical afl-api
reconciliation (issue #242).

## Why this exists

A legitimate AFL player sometimes needs to participate in the BBBFFL draft
before `afl-api` represents them (a genuine rookie or a mid-season recruit
whose upstream identity has not yet been published). `docs/2027-live-season-
readiness.md` recorded this as an outstanding gap: `season_player_pool.
canonical_player_id` was `NOT NULL`/positive-only, and no domain function
implemented the provisional-creation-then-reconciliation lifecycle
`docs/plans/2027-season-model.md`'s "AFL player identity and provisional
players" section anticipates.

## Identity model

`season_player_pool.season_player_id` (migration `0006_player_pool_ownership`)
was already BBBFFL's stable internal player identity, distinct from
`canonical_player_id`, the cached afl-api association -- see `app.player_pool`'s
module docstring and `docs/player-pool-ownership.md`. This module does not
introduce a second identity concept; it only lets that association be
temporarily absent (`canonical_player_id IS NULL`, migration
`0038_provisional_players`) and later attached without ever changing
`season_player_id` -- every ownership period, draft pick, weekly selection,
shortlist entry and audit event a provisional player accumulates stays
attached to the same row before and after reconciliation. A player is
*currently* provisional exactly when `canonical_player_id IS NULL`; there is
no separate cached "is provisional" flag to drift out of sync with it.
`was_provisional` is a one-way, permanent marker so a reconciled player's
provisional origin remains visible ("do not destroy the fact that the
player previously existed provisionally").

## Role boundary (issue #242)

- **Coach**: may only *nominate* a missing player (`PlayerNominationRepository.
  submit`) -- surfaced to Scorer/Administrator, never creating a BBBFFL
  identity directly.
- **Scorer/Administrator**: verify and create the provisional player
  (`ProvisionalPlayerRepository.create`), and later review/decide on a
  detected candidate match (`approve`/`reject_candidate`/`defer_candidate`).
  Route-level authorization (`app.routes.provisional_players`) is what
  actually enforces this boundary; this module's own methods take an
  `ActorContext` for audit attribution only, exactly like every other
  domain repository in this codebase.

## Candidate detection

`detect_candidates` is a recommendation/detection system, never automatic
reconciliation (see the module's own docstring below): it never mutates
`canonical_player_id`. Its only side effects are (a) recording a suggested
`provisional_match_candidate` row and (b) quarantining the candidate afl-api
player's own freshly-imported `season_player_pool` row (`eligible=False`) so
nobody can draft "the same real person twice" -- one under the provisional
identity, one under the newly-arrived canonical one -- while the match is
still unresolved. Deliberately called from the application-service layer
(`app.season_setup.refresh_player_pool`), not from `app.player_pool.
PlayerPoolRepository.refresh_season_pool` itself: this module sits on top of
the season model (`app.player_pool`, `app.season`), the same layering
`app.ladder_tie_ruling` uses relative to `app.ladder` -- `tests/
test_architecture.py`'s `test_ladder_governance_is_an_application_service`
establishes the same "season model must not depend back on its governance
sibling" rule this module follows too.

## Reconciliation safety

`reconcile` treats the operation as high-risk and transactional (see its own
docstring): it locks both the provisional source row and the canonical
target row before validating, so a concurrent reconciliation attempt against
the same provisional player serializes and then fails closed (`source is
no longer provisional`) rather than double-applying. It refuses outright if
the target canonical pool row already carries any BBBFFL ownership history
(a duplicate that was somehow drafted before its identity question was
resolved) -- reconciliation only merges into the *existing* provisional
identity, never repoints history away from it.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from app.audit import ActorContext, append_event
from app.db import _for_update_suffix, transaction
from app.player_pool import PlayerPoolRepository, SeasonPlayer
from app.season import SeasonRepository


def _id() -> str:
    return str(uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


NOMINATION_SUBMITTED = "player_pool.nomination.submitted"
NOMINATION_DISMISSED = "player_pool.nomination.dismissed"
NOMINATION_RESOLVED = "player_pool.nomination.resolved"
PROVISIONAL_CREATED = "player_pool.provisional.created"
CANDIDATE_DETECTED = "player_pool.provisional.candidate_detected"
CANDIDATE_REJECTED = "player_pool.provisional.candidate_rejected"
CANDIDATE_DEFERRED = "player_pool.provisional.candidate_deferred"
PROVISIONAL_RECONCILED = "player_pool.provisional.reconciled"

ENTITY_TYPE_NOMINATION = "player_pool.nomination"
ENTITY_TYPE_PROVISIONAL_PLAYER = "player_pool.provisional_player"
ENTITY_TYPE_MATCH_CANDIDATE = "player_pool.match_candidate"

MATCH_BASIS_GIVEN_FAMILY_NAME = "given_name+family_name"

# `season_player_pool.source_provider` for a provisional row -- never a live
# afl-api provider string (`app.season_setup.live_source_provider`'s
# `"afl-api-v1/season-<id>"` shape), so a caller can always tell the two
# apart. Codex review on PR #258 (P1, sixth round): `app.season_setup.
# _live_pool_afl_season_id` must exclude this provider when deciding whether
# the pool was populated from exactly one live afl-api season -- a
# provisional player created before the preseason draft order is accepted
# must not make that check see two providers and refuse to start the draft.
PROVISIONAL_SOURCE_PROVIDER = "bbbffl-provisional"


class NominationStateError(ValueError):
    pass


class ProvisionalPlayerError(ValueError):
    pass


class NotProvisionalError(ProvisionalPlayerError):
    """Raised when a reconciliation/candidate-decision target is not (or is
    no longer) provisional -- including a repeated reconciliation attempt
    against a player another request already reconciled."""


class InvalidReconciliationTargetError(ProvisionalPlayerError):
    """The proposed canonical target is not a valid, distinct, unowned
    canonical pool entry in the same season."""


class ReconciliationConflictError(ProvisionalPlayerError):
    """The target cannot be merged safely -- it already carries BBBFFL
    history, or removing its pool row would violate a referential
    integrity constraint this module did not anticipate."""


@dataclass(frozen=True)
class PlayerNomination:
    nomination_id: str
    season_id: str
    season_entry_id: str
    player_name: str
    afl_club_note: str | None
    note: str | None
    status: str
    created_at: str
    resolved_at: str | None
    resulting_season_player_id: str | None


@dataclass(frozen=True)
class MatchCandidate:
    candidate_id: str
    season_id: str
    season_player_id: str
    canonical_player_id: int
    match_basis: str
    status: str
    detected_at: str
    decided_at: str | None
    # Codex review on PR #258 (P1, second round): the canonical row's
    # `eligible` value right before detection quarantined it, so releasing
    # quarantine restores this recorded value instead of assuming `True`.
    restore_eligible_on_release: bool = True


@dataclass(frozen=True)
class OutstandingProvisionalPlayer:
    """Read model for the Coach/Scorer/Admin dashboard notices and the
    Scorer/Admin management page: one currently-provisional player plus its
    current owner (if drafted) and any detected candidate matches. Never an
    authority -- `player.canonical_player_id IS NULL` on `season_player_pool`
    remains the sole fact that decides whether a player is provisional."""

    player: SeasonPlayer
    owner_season_entry_id: str | None
    owner_team_name: str | None
    candidates: tuple[MatchCandidate, ...]

    @property
    def is_ambiguous(self) -> bool:
        return len([c for c in self.candidates if c.status == "pending"]) > 1

    @property
    def has_candidate(self) -> bool:
        return any(c.status == "pending" for c in self.candidates)


def _nomination(row) -> PlayerNomination:
    return PlayerNomination(**dict(row))


def _candidate(row) -> MatchCandidate:
    values = dict(row)
    values["restore_eligible_on_release"] = bool(values["restore_eligible_on_release"])
    return MatchCandidate(**values)


class PlayerNominationRepository:
    """Issue #242's Coach-facing "report a missing player" mechanism. A
    Coach may only ever create/read a nomination here -- there is no method
    on this class that creates a `season_player_pool` row; that authority
    belongs solely to `ProvisionalPlayerRepository.create`, which a Scorer/
    Administrator route reaches after reviewing a pending nomination (or
    without one at all, when the Scorer identifies the need directly)."""

    def __init__(self, database):
        self.database = database

    def submit(
        self,
        season_id: str,
        season_entry_id: str,
        player_name: str,
        *,
        afl_club_note: str | None = None,
        note: str | None = None,
        actor: ActorContext,
        reason: str | None = None,
    ) -> PlayerNomination:
        if not player_name or not player_name.strip():
            raise ValueError("a player nomination requires a player name")
        with transaction(self.database) as conn:
            SeasonRepository(self.database).guard_writable(conn, season_id)
            entry = conn.execute(
                "SELECT season_id FROM season_entry WHERE season_entry_id=?", (season_entry_id,)
            ).fetchone()
            if not entry or entry["season_id"] != season_id:
                raise KeyError(season_entry_id)
            nomination_id, created = _id(), _now()
            conn.execute(
                "INSERT INTO player_nomination "
                "(nomination_id, season_id, season_entry_id, player_name, afl_club_note, note, status, "
                "created_at, resolved_at, resulting_season_player_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL)",
                (nomination_id, season_id, season_entry_id, player_name.strip(), afl_club_note, note, created),
            )
            append_event(
                conn,
                actor=actor,
                action=NOMINATION_SUBMITTED,
                entity_type=ENTITY_TYPE_NOMINATION,
                entity_id=nomination_id,
                reason=reason,
                after_state={
                    "season_id": season_id,
                    "season_entry_id": season_entry_id,
                    "player_name": player_name.strip(),
                },
            )
        return self.get(nomination_id)

    def get(self, nomination_id: str) -> PlayerNomination | None:
        row = self.database.execute(
            "SELECT * FROM player_nomination WHERE nomination_id=?", (nomination_id,)
        ).fetchone()
        return _nomination(row) if row else None

    def list_for_season(self, season_id: str, *, status: str | None = None) -> list[PlayerNomination]:
        if status:
            rows = self.database.execute(
                "SELECT * FROM player_nomination WHERE season_id=? AND status=? ORDER BY created_at",
                (season_id, status),
            ).fetchall()
        else:
            rows = self.database.execute(
                "SELECT * FROM player_nomination WHERE season_id=? ORDER BY created_at", (season_id,)
            ).fetchall()
        return [_nomination(row) for row in rows]

    def list_for_entry(self, season_entry_id: str) -> list[PlayerNomination]:
        rows = self.database.execute(
            "SELECT * FROM player_nomination WHERE season_entry_id=? ORDER BY created_at", (season_entry_id,)
        ).fetchall()
        return [_nomination(row) for row in rows]

    def dismiss(self, season_id: str, nomination_id: str, *, actor: ActorContext, reason: str) -> PlayerNomination:
        """`season_id` must be the season the caller's authorization covers
        (Codex review on PR #258, P1) -- see `ProvisionalPlayerRepository.
        reconcile`'s docstring for why every mutation here binds to it
        rather than trusting the resource id alone.

        Locks the season row (`guard_writable`) *before* the nomination row
        (Codex review on PR #258, P2, third round) -- `create` locks in
        that same order when resolving a nomination, and the season row
        must always be the first lock taken across every write path here
        (see `app.season.SeasonRepository.guard_writable`'s own docstring)
        or two concurrent requests can deadlock on PostgreSQL by acquiring
        these two locks in opposite order."""
        if not reason or not reason.strip():
            raise ValueError("dismissing a nomination requires a reason")
        with transaction(self.database) as conn:
            SeasonRepository(self.database).guard_writable(conn, season_id)
            nomination = conn.execute(
                "SELECT * FROM player_nomination WHERE nomination_id=? AND season_id=?"
                + _for_update_suffix(self.database),
                (nomination_id, season_id),
            ).fetchone()
            if not nomination:
                raise KeyError(nomination_id)
            if nomination["status"] != "pending":
                raise NominationStateError(f"nomination is already {nomination['status']}")
            at = _now()
            conn.execute(
                "UPDATE player_nomination SET status='dismissed', resolved_at=? WHERE nomination_id=?",
                (at, nomination_id),
            )
            append_event(
                conn,
                actor=actor,
                action=NOMINATION_DISMISSED,
                entity_type=ENTITY_TYPE_NOMINATION,
                entity_id=nomination_id,
                reason=reason,
                before_state={"status": "pending"},
                after_state={"status": "dismissed"},
            )
        return self.get(nomination_id)


class ProvisionalPlayerRepository:
    def __init__(self, database):
        self.database = database
        self.pool = PlayerPoolRepository(database)

    def create(
        self,
        season_id: str,
        *,
        display_name: str,
        given_name: str,
        family_name: str,
        note: str,
        afl_team_name: str | None = None,
        actor: ActorContext,
        reason: str | None = None,
        nomination_id: str | None = None,
    ) -> SeasonPlayer:
        """Scorer/Administrator-only (enforced at the route layer): verify
        and create a provisional BBBFFL player identity with no afl-api
        canonical association. Requires the identifying information the
        issue calls for -- `display_name`/`given_name`/`family_name` (the
        same authoritative-name shape `app.player_pool` already caches for
        every canonical player, and the evidence `detect_candidates` later
        matches against) and a `note` recording why/how the Scorer verified
        this is a real, legitimate AFL player -- never a fabricated or
        guessed `afl-api` id. Optionally resolves a Coach's pending
        `player_nomination` in the same transaction."""
        required = {
            "display name": display_name,
            "given name": given_name,
            "family name": family_name,
            "reason/source note": note,
        }
        for label, value in required.items():
            if not value or not value.strip():
                raise ValueError(f"creating a provisional player requires a {label}")
        with transaction(self.database) as conn:
            SeasonRepository(self.database).guard_writable(conn, season_id)
            nomination = None
            if nomination_id is not None:
                nomination = conn.execute(
                    "SELECT * FROM player_nomination WHERE nomination_id=?" + _for_update_suffix(self.database),
                    (nomination_id,),
                ).fetchone()
                if not nomination or nomination["season_id"] != season_id:
                    raise KeyError(nomination_id)
                if nomination["status"] != "pending":
                    raise NominationStateError(f"nomination is already {nomination['status']}")
            season_player_id, created = _id(), _now()
            club = afl_team_name.strip() if afl_team_name else None
            conn.execute(
                "INSERT INTO season_player_pool "
                "(season_player_id, season_id, canonical_player_id, display_name, afl_team_id, afl_team_name, "
                "eligible, source_provider, source_fetched_at, source_updated_at, created_at, updated_at, "
                "given_name, family_name, was_provisional, provisional_note, provisional_reconciled_at) "
                "VALUES (?, ?, NULL, ?, NULL, ?, TRUE, ?, ?, NULL, ?, ?, ?, ?, TRUE, ?, NULL)",
                (
                    season_player_id,
                    season_id,
                    display_name.strip(),
                    club,
                    PROVISIONAL_SOURCE_PROVIDER,
                    created,
                    created,
                    created,
                    given_name.strip(),
                    family_name.strip(),
                    note.strip(),
                ),
            )
            append_event(
                conn,
                actor=actor,
                action=PROVISIONAL_CREATED,
                entity_type=ENTITY_TYPE_PROVISIONAL_PLAYER,
                entity_id=season_player_id,
                reason=reason or note.strip(),
                after_state={
                    "season_id": season_id,
                    "display_name": display_name.strip(),
                    "given_name": given_name.strip(),
                    "family_name": family_name.strip(),
                    "afl_team_name": club,
                },
                payload={"nomination_id": nomination_id} if nomination_id else None,
            )
            if nomination is not None:
                at = _now()
                conn.execute(
                    "UPDATE player_nomination SET status='created', resolved_at=?, resulting_season_player_id=? "
                    "WHERE nomination_id=?",
                    (at, season_player_id, nomination_id),
                )
                append_event(
                    conn,
                    actor=actor,
                    action=NOMINATION_RESOLVED,
                    entity_type=ENTITY_TYPE_NOMINATION,
                    entity_id=nomination_id,
                    reason=reason,
                    before_state={"status": "pending"},
                    after_state={"status": "created", "resulting_season_player_id": season_player_id},
                )
            # Codex review on PR #258 (P2, seventh round): an exact
            # structured-name match may already exist in the canonical pool
            # at creation time -- detection otherwise only runs on the next
            # `refresh_player_pool`, which could leave both identities
            # fully eligible/draftable indefinitely. Run the same detection
            # this new row would get on that next refresh, immediately, in
            # this same transaction.
            _detect_candidates_for_provisional(
                conn, season_id, season_player_id, given_name.strip(), family_name.strip(), actor=actor
            )
        return self.pool.get_by_id(season_player_id)

    def list_outstanding(self, season_id: str) -> list[OutstandingProvisionalPlayer]:
        """Every currently-provisional player in this season (`canonical_
        player_id IS NULL`), plus current ownership and any detected
        candidates -- the shared read model behind the Coach/Scorer/Admin
        dashboard notices (persistent for as long as this list is
        non-empty for a given audience) and the Scorer/Admin management
        page."""
        rows = self.database.execute(
            "SELECT p.*, o.season_entry_id AS owner_season_entry_id, n.team_name AS owner_team_name "
            "FROM season_player_pool p "
            "LEFT JOIN player_ownership_period o ON o.season_player_id=p.season_player_id AND o.released_at IS NULL "
            "LEFT JOIN season_entry_team_name_history n ON n.season_entry_id=o.season_entry_id AND n.ended_at IS NULL "
            "WHERE p.season_id=? AND p.canonical_player_id IS NULL ORDER BY p.created_at",
            (season_id,),
        ).fetchall()
        candidates_by_player: dict[str, list[MatchCandidate]] = {}
        for row in self.database.execute(
            "SELECT * FROM provisional_match_candidate WHERE season_id=? ORDER BY detected_at", (season_id,)
        ).fetchall():
            candidates_by_player.setdefault(row["season_player_id"], []).append(_candidate(row))
        result = []
        for row in rows:
            values = dict(row)
            owner_season_entry_id = values.pop("owner_season_entry_id")
            owner_team_name = values.pop("owner_team_name")
            values["eligible"] = bool(values["eligible"])
            values["was_provisional"] = bool(values["was_provisional"])
            player = SeasonPlayer(**values)
            result.append(
                OutstandingProvisionalPlayer(
                    player=player,
                    owner_season_entry_id=owner_season_entry_id,
                    owner_team_name=owner_team_name,
                    candidates=tuple(candidates_by_player.get(player.season_player_id, ())),
                )
            )
        return result

    def coach_dashboard_notices(self, coach_id: str) -> list[dict]:
        """Issue #242's persistent Coach dashboard notice (`app.routes.auth.
        account_page`): one entry per season this coach currently occupies
        (via `season_entry_coach_history`, the same current-occupancy join
        `app.identity.IdentityRepository.list_entries` uses) that has at
        least one outstanding provisional player right now. Purely
        informational -- the Coach gets counts and names, particularly
        whether any belong to their own squad, never a reconciliation
        control (see the module docstring's role boundary)."""
        seasons = self.database.execute(
            "SELECT DISTINCT e.season_entry_id, e.season_id, s.label AS season_label "
            "FROM season_entry e "
            "JOIN season_entry_coach_history h ON h.season_entry_id=e.season_entry_id AND h.ended_at IS NULL "
            "JOIN bbbffl_season s ON s.season_id=e.season_id "
            "WHERE h.coach_id=?",
            (coach_id,),
        ).fetchall()
        notices = []
        for row in seasons:
            outstanding = self.list_outstanding(row["season_id"])
            if not outstanding:
                continue
            on_my_squad = [
                o.player.display_name for o in outstanding if o.owner_season_entry_id == row["season_entry_id"]
            ]
            notices.append(
                {
                    "season_id": row["season_id"],
                    "season_label": row["season_label"],
                    "outstanding_count": len(outstanding),
                    "on_my_squad": on_my_squad,
                }
            )
        return notices

    def reconcile(
        self, season_id: str, season_player_id: str, target_season_player_id: str, *, actor: ActorContext, reason: str
    ) -> SeasonPlayer:
        """Scorer/Administrator-only (enforced at the route layer): convert
        an existing provisional player into one associated with exactly one
        canonical afl-api player, preserving `season_player_id` (and
        therefore every ownership/draft/lineup/audit reference already
        attached to it) -- see the module docstring's "Reconciliation
        safety" section for the validations this performs before writing
        anything.

        `season_id` must be the season the caller's authorization actually
        covers (Codex review on PR #258, P1): a route only checks
        `require_role_covers_season` against the URL's `season_id`, so
        without this the source/target rows' *own* season -- taken purely
        from `season_player_id`/`target_season_player_id` -- could silently
        belong to a season-scoped Scorer's *unauthorized* season. Every
        other write below fails closed (404-shaped `KeyError`) unless the
        loaded source row actually belongs to `season_id`.

        Locks the season row (`guard_writable`) *before* the source/target
        pool rows (Codex review on PR #258, P2, third round) -- the season
        row must always be the first lock taken across every write path
        here (see `app.season.SeasonRepository.guard_writable`'s own
        docstring), or two concurrent requests can deadlock on PostgreSQL
        by acquiring these locks in opposite order."""
        if not reason or not reason.strip():
            raise ValueError("reconciliation requires an explicit reason")
        with transaction(self.database) as conn:
            SeasonRepository(self.database).guard_writable(conn, season_id)
            source = conn.execute(
                "SELECT * FROM season_player_pool WHERE season_player_id=? AND season_id=?"
                + _for_update_suffix(self.database),
                (season_player_id, season_id),
            ).fetchone()
            if not source:
                raise KeyError(season_player_id)
            if source["canonical_player_id"] is not None:
                raise NotProvisionalError("player is not provisional -- it already has a canonical afl-api identity")
            target = conn.execute(
                "SELECT * FROM season_player_pool WHERE season_player_id=?" + _for_update_suffix(self.database),
                (target_season_player_id,),
            ).fetchone()
            if not target:
                raise KeyError(target_season_player_id)
            if target["season_id"] != source["season_id"]:
                raise InvalidReconciliationTargetError(
                    "target must belong to the same season as the provisional player"
                )
            if target["season_player_id"] == source["season_player_id"]:
                raise InvalidReconciliationTargetError(
                    "target must be a different pool entry from the provisional player"
                )
            if target["canonical_player_id"] is None:
                raise InvalidReconciliationTargetError(
                    "target must itself be a canonical afl-api player, not another provisional player"
                )
            owned = conn.execute(
                "SELECT 1 FROM player_ownership_period WHERE season_player_id=?" + _for_update_suffix(self.database),
                (target_season_player_id,),
            ).fetchone()
            if owned:
                raise ReconciliationConflictError(
                    "target canonical player already has BBBFFL ownership history and cannot be merged automatically"
                )
            # Codex review on PR #258 (P2, fifth round): if the target
            # canonical player was already ineligible for an unrelated
            # policy reason before candidate detection quarantined it, that
            # ineligibility must survive the merge into the surviving
            # (provisional) row -- a provisional row is created eligible, so
            # leaving `eligible` untouched on the UPDATE below would
            # silently make a policy-restricted player draftable. Prefer the
            # winning candidate's own recorded pre-quarantine value when one
            # exists (the target may currently read `eligible=FALSE` purely
            # because detection quarantined it for *this* match, which must
            # not itself count as "ineligible"); fall back to the target
            # row's own current `eligible` when reconciling to a target that
            # was never a detected candidate for this provisional player, so
            # nothing has quarantined it and its live value is authoritative.
            # Codex review on PR #258 (P2, sixth round): restrict this to a
            # still-*pending* winning candidate -- a rejected one is no
            # longer an active quarantine, so its recorded value can be
            # stale by the time of a later, unrelated eligibility change and
            # a manual reconciliation to the same target.
            winning_candidate = conn.execute(
                "SELECT restore_eligible_on_release FROM provisional_match_candidate "
                "WHERE season_player_id=? AND canonical_player_id=? AND status='pending'",
                (season_player_id, target["canonical_player_id"]),
            ).fetchone()
            merged_eligible = (
                bool(winning_candidate["restore_eligible_on_release"])
                if winning_candidate is not None
                else bool(target["eligible"])
            )
            # Codex review on PR #258 (P2, seventh round): if some *other*
            # provisional player still has a pending candidate naming this
            # same target canonical id, `fk_candidate_target_same_season`'s
            # `ON DELETE CASCADE` (triggered by the target row delete below)
            # would silently destroy that still-unresolved suggestion --
            # this decision only establishes that *this* provisional player
            # is the target; it says nothing about whether the other one
            # also is (and it cannot be both). Capture those rows before the
            # cascade removes them, force the merged row to stay quarantined
            # while any such claim remains open, and recreate each one
            # afterwards against the same canonical id, which the merged row
            # now carries -- exactly the same suggestion, still pending.
            other_provisionals_pending = conn.execute(
                "SELECT season_player_id, match_basis, restore_eligible_on_release FROM provisional_match_candidate "
                "WHERE season_id=? AND canonical_player_id=? AND season_player_id<>? AND status='pending'",
                (source["season_id"], target["canonical_player_id"], season_player_id),
            ).fetchall()
            if other_provisionals_pending:
                merged_eligible = False
            at = _now()
            # Any other candidate suggested for this provisional player but
            # not chosen is, by this decision, a distinct real person -- it
            # must return to the ordinary available pool rather than stay
            # quarantined indefinitely for a match that was never approved.
            # But not if some *other* provisional player still has a
            # pending candidate naming that same canonical id (Codex review
            # on PR #258, P1) -- releasing it here would prematurely make
            # it draftable out from under that still-unresolved suggestion.
            # Codex review on PR #258 (P2, fourth round): only a still-pending
            # losing candidate should have its quarantine released here -- a
            # candidate already rejected by `reject_candidate` was released
            # (or refused release, per `_other_pending_candidates_exist`) at
            # rejection time, and its `restore_eligible_on_release` may since
            # be stale relative to a later, unrelated eligibility change on
            # that canonical row.
            other_candidates = conn.execute(
                "SELECT * FROM provisional_match_candidate WHERE season_player_id=? AND canonical_player_id<>? "
                "AND status='pending'",
                (season_player_id, target["canonical_player_id"]),
            ).fetchall()
            for candidate in other_candidates:
                if _other_pending_candidates_exist(
                    conn,
                    source["season_id"],
                    candidate["canonical_player_id"],
                    excluding_season_player_id=season_player_id,
                ):
                    continue
                conn.execute(
                    "UPDATE season_player_pool SET eligible=? WHERE season_id=? AND canonical_player_id=?",
                    (
                        bool(candidate["restore_eligible_on_release"]),
                        source["season_id"],
                        candidate["canonical_player_id"],
                    ),
                )
            conn.execute("DELETE FROM provisional_match_candidate WHERE season_player_id=?", (season_player_id,))
            # Retire the now-redundant duplicate pool entry *before*
            # updating the source row's canonical_player_id below -- both
            # rows cannot hold the same (season_id, canonical_player_id) at
            # once (uq_pool_season_canonical_player), so the target's row
            # must be gone first. The provisional player's own
            # season_player_id (and therefore every ownership/draft/
            # lineup/audit reference to it) is untouched throughout.
            # `fk_candidate_target_same_season`'s ON DELETE CASCADE removes
            # any other provisional player's stale candidate row naming this
            # same canonical id automatically.
            try:
                result = conn.execute(
                    "DELETE FROM season_player_pool WHERE season_player_id=?", (target_season_player_id,)
                )
            except Exception as exc:  # pragma: no cover - dialect-specific IntegrityError types
                raise ReconciliationConflictError(
                    "target canonical player is still referenced elsewhere (e.g. a private shortlist) and cannot "
                    "be merged automatically"
                ) from exc
            if getattr(result, "rowcount", 1) == 0:
                raise ReconciliationConflictError("target canonical player was removed concurrently")
            conn.execute(
                "UPDATE season_player_pool SET canonical_player_id=?, display_name=?, given_name=?, family_name=?, "
                "afl_team_id=?, afl_team_name=?, source_provider=?, source_fetched_at=?, source_updated_at=?, "
                "eligible=?, provisional_reconciled_at=?, updated_at=? WHERE season_player_id=?",
                (
                    target["canonical_player_id"],
                    target["display_name"],
                    target["given_name"],
                    target["family_name"],
                    target["afl_team_id"],
                    target["afl_team_name"],
                    target["source_provider"],
                    target["source_fetched_at"],
                    target["source_updated_at"],
                    merged_eligible,
                    at,
                    at,
                    season_player_id,
                ),
            )
            # Recreate each other provisional player's still-pending claim on
            # this canonical id, captured above before the cascade delete --
            # the merged row now carries `target["canonical_player_id"]`, so
            # the same suggestion (unchanged `match_basis`/
            # `restore_eligible_on_release`) is exactly as valid against it.
            for other in other_provisionals_pending:
                conn.execute(
                    "INSERT INTO provisional_match_candidate "
                    "(candidate_id, season_id, season_player_id, canonical_player_id, match_basis, status, "
                    "detected_at, decided_at, restore_eligible_on_release) VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL, ?)",
                    (
                        _id(),
                        source["season_id"],
                        other["season_player_id"],
                        target["canonical_player_id"],
                        other["match_basis"],
                        at,
                        other["restore_eligible_on_release"],
                    ),
                )
            append_event(
                conn,
                actor=actor,
                action=PROVISIONAL_RECONCILED,
                entity_type=ENTITY_TYPE_PROVISIONAL_PLAYER,
                entity_id=season_player_id,
                reason=reason,
                before_state={"canonical_player_id": None, "provisional_note": source["provisional_note"]},
                after_state={
                    "canonical_player_id": target["canonical_player_id"],
                    "display_name": target["display_name"],
                },
                payload={"retired_duplicate_season_player_id": target_season_player_id},
            )
        return self.pool.get_by_id(season_player_id)

    def reject_candidate(
        self, season_id: str, season_player_id: str, canonical_player_id: int, *, actor: ActorContext, reason: str
    ) -> None:
        """Scorer/Administrator-only: explicitly rule out a detected
        candidate. Restores the candidate's own pool row to selectable --
        unless some *other* provisional player still has a pending
        candidate naming the same canonical id (Codex review on PR #258,
        P1: two provisional players can share a name) -- and permanently
        excludes this exact pair from future automatic suggestion -- see
        `detect_candidates`. `season_id` must be the season the caller's
        authorization covers, and is locked (`guard_writable`) before the
        candidate row (see `reconcile`'s docstring for both)."""
        if not reason or not reason.strip():
            raise ValueError("rejecting a candidate match requires a reason")
        with transaction(self.database) as conn:
            SeasonRepository(self.database).guard_writable(conn, season_id)
            candidate = conn.execute(
                "SELECT * FROM provisional_match_candidate WHERE season_player_id=? AND canonical_player_id=? "
                "AND season_id=?" + _for_update_suffix(self.database),
                (season_player_id, canonical_player_id, season_id),
            ).fetchone()
            if not candidate:
                raise KeyError((season_player_id, canonical_player_id))
            if candidate["status"] == "rejected":
                raise ProvisionalPlayerError("candidate match is already rejected")
            at = _now()
            conn.execute(
                "UPDATE provisional_match_candidate SET status='rejected', decided_at=? WHERE candidate_id=?",
                (at, candidate["candidate_id"]),
            )
            if not _other_pending_candidates_exist(
                conn, season_id, canonical_player_id, excluding_season_player_id=season_player_id
            ):
                conn.execute(
                    "UPDATE season_player_pool SET eligible=? WHERE season_id=? AND canonical_player_id=?",
                    (bool(candidate["restore_eligible_on_release"]), season_id, canonical_player_id),
                )
            append_event(
                conn,
                actor=actor,
                action=CANDIDATE_REJECTED,
                entity_type=ENTITY_TYPE_MATCH_CANDIDATE,
                entity_id=candidate["candidate_id"],
                reason=reason,
                before_state={"status": "pending"},
                after_state={"status": "rejected"},
                payload={"season_player_id": season_player_id, "canonical_player_id": canonical_player_id},
            )

    def defer_candidate(
        self,
        season_id: str,
        season_player_id: str,
        canonical_player_id: int,
        *,
        actor: ActorContext,
        reason: str | None = None,
    ) -> None:
        """Scorer/Administrator-only: record that the candidate was seen
        and deliberately not acted on yet. Purely an audit note -- neither
        the candidate row nor the quarantined pool row changes, so the
        candidate remains exactly as actionable on the next visit.
        `season_id` must be the season the caller's authorization covers,
        and is locked (`guard_writable`) before the candidate row (see
        `reconcile`'s docstring for both)."""
        with transaction(self.database) as conn:
            SeasonRepository(self.database).guard_writable(conn, season_id)
            candidate = conn.execute(
                "SELECT * FROM provisional_match_candidate WHERE season_player_id=? AND canonical_player_id=? "
                "AND season_id=?" + _for_update_suffix(self.database),
                (season_player_id, canonical_player_id, season_id),
            ).fetchone()
            if not candidate:
                raise KeyError((season_player_id, canonical_player_id))
            # Codex review on PR #258 (P2, fifth round): a candidate already
            # rejected is terminal -- deferring it would record a
            # `candidate_deferred` audit event for an action that left
            # nothing actionable, and contradicts the earlier rejection.
            if candidate["status"] != "pending":
                raise ProvisionalPlayerError("candidate match is no longer pending and cannot be deferred")
            append_event(
                conn,
                actor=actor,
                action=CANDIDATE_DEFERRED,
                entity_type=ENTITY_TYPE_MATCH_CANDIDATE,
                entity_id=candidate["candidate_id"],
                reason=reason,
                payload={"season_player_id": season_player_id, "canonical_player_id": canonical_player_id},
            )


def detect_candidates(database, season_id: str, *, actor: ActorContext) -> int:
    """Suggest plausible canonical matches for every currently-provisional
    player in this season -- see `detect_candidates_in_transaction`, which
    this wraps in its own transaction for a standalone caller (e.g. a
    script or a test)."""
    with transaction(database) as conn:
        return detect_candidates_in_transaction(conn, database, season_id, actor=actor)


def detect_candidates_in_transaction(conn, database, season_id: str, *, actor: ActorContext) -> int:
    """As `detect_candidates`, but on the caller's own transaction-scoped
    `conn` (issue #242's afl-api candidate detection). `app.season_setup.
    refresh_player_pool` calls this in the *same* transaction as `app.
    player_pool.PlayerPoolRepository.refresh_season_pool_in_transaction`,
    immediately after it, so a newly-imported canonical duplicate of a
    provisional player is never visible to another transaction as eligible
    even momentarily -- its quarantine (below) commits atomically with its
    insertion. See the module docstring's layering note for why this lives
    here rather than inside `app.player_pool` itself. `database` is needed
    (separately from `conn`) only to resolve the SQL dialect for
    `guard_writable`'s row lock, the same as every `_in_transaction` method
    elsewhere in this module.

    Locks the season row (`guard_writable`) before mutating anything (Codex
    review on PR #258, P2, third round): this is the one mutating entry
    point in this module that is not a `ProvisionalPlayerRepository`
    method, so it must enforce the completed-season write fence itself
    rather than relying on a caller that might not.

    Matching requires an exact, case-insensitive `given_name`+`family_name`
    match between the provisional player and an already-canonical pool row
    in the same season -- matching names alone is evidence, never proof, so
    this never writes `canonical_player_id`. A provisional player missing
    either structured-name field (pre-issue-#248 data, or one created
    without them) is simply skipped -- there is nothing reliable to match
    on. A pair already explicitly rejected by a Scorer/Administrator
    (`reject_candidate`) is never re-suggested. Returns the number of newly
    recorded candidate rows."""
    SeasonRepository(database).guard_writable(conn, season_id)
    detected = 0
    provisional_rows = conn.execute(
        "SELECT season_player_id, given_name, family_name FROM season_player_pool "
        "WHERE season_id=? AND canonical_player_id IS NULL "
        "AND given_name IS NOT NULL AND family_name IS NOT NULL",
        (season_id,),
    ).fetchall()
    for provisional in provisional_rows:
        detected += _detect_candidates_for_provisional(
            conn,
            season_id,
            provisional["season_player_id"],
            provisional["given_name"],
            provisional["family_name"],
            actor=actor,
        )
    return detected


def _detect_candidates_for_provisional(
    conn, season_id: str, season_player_id: str, given_name: str, family_name: str, *, actor: ActorContext
) -> int:
    """Suggest plausible canonical matches for exactly one already-inserted
    provisional player row -- the per-player body `detect_candidates_in_
    transaction` loops over for every provisional row, and what `Provisional
    PlayerRepository.create` also calls once, in the same transaction, for
    the row it just inserted (Codex review on PR #258, P2, seventh round):
    without this, a canonical duplicate that already existed *before*
    creation would sit unflagged and fully eligible until the next
    `refresh_player_pool`, rather than being quarantined the moment the
    provisional identity that duplicates it is created. Caller must already
    hold the season write lock (`guard_writable`). Returns the number of
    newly recorded candidate rows."""
    detected = 0
    matches = conn.execute(
        "SELECT canonical_player_id, eligible FROM season_player_pool "
        "WHERE season_id=? AND canonical_player_id IS NOT NULL "
        "AND lower(given_name)=lower(?) AND lower(family_name)=lower(?)",
        (season_id, given_name, family_name),
    ).fetchall()
    for match in matches:
        canonical_player_id = match["canonical_player_id"]
        existing = conn.execute(
            "SELECT status FROM provisional_match_candidate WHERE season_player_id=? AND canonical_player_id=?",
            (season_player_id, canonical_player_id),
        ).fetchone()
        if existing is not None:
            # Already known -- either still pending (nothing new to
            # record) or explicitly rejected (never resurrected
            # automatically).
            continue
        # Codex review on PR #258 (P1 then P2, second and third rounds):
        # record whether this row was actually eligible right before
        # *any* detection run ever quarantined it, so releasing
        # quarantine later restores *that* value rather than
        # unconditionally `TRUE` -- a row already ineligible for an
        # unrelated reason must not become draftable just because an
        # unrelated candidate suggestion was rejected. Reusing an
        # already-recorded value from any other candidate row on this
        # same canonical id (this run or an earlier one) rather than
        # reading `match["eligible"]` directly is what makes this
        # correct across multiple detection runs -- once quarantined,
        # the pool row's *current* `eligible` is no longer the original
        # value, so only the first-ever candidate for a canonical id
        # may derive it from the live row; every later one must copy
        # the value that first candidate already recorded. Codex review
        # on PR #258 (P2, fourth round): restrict this to a still-*pending*
        # prior candidate. A rejected candidate's recorded value can be
        # stale by the time a new candidate is detected for the same
        # canonical id -- the row may have since been made ineligible for
        # an unrelated reason (a fresh `refresh_season_pool` call) -- so
        # once no pending candidate remains, the next one must re-derive
        # from the pool row's current `eligible` value, not a rejected
        # candidate's old one.
        prior_candidate = conn.execute(
            "SELECT restore_eligible_on_release FROM provisional_match_candidate "
            "WHERE season_id=? AND canonical_player_id=? AND status='pending' LIMIT 1",
            (season_id, canonical_player_id),
        ).fetchone()
        restore_eligible_on_release = (
            bool(prior_candidate["restore_eligible_on_release"])
            if prior_candidate is not None
            else bool(match["eligible"])
        )
        candidate_id, detected_at = _id(), _now()
        conn.execute(
            "INSERT INTO provisional_match_candidate "
            "(candidate_id, season_id, season_player_id, canonical_player_id, match_basis, status, "
            "detected_at, decided_at, restore_eligible_on_release) VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL, ?)",
            (
                candidate_id,
                season_id,
                season_player_id,
                canonical_player_id,
                MATCH_BASIS_GIVEN_FAMILY_NAME,
                detected_at,
                restore_eligible_on_release,
            ),
        )
        # Quarantine the candidate's own pool row: it must not be
        # draftable while a plausible duplicate-identity question is
        # unresolved (issue #242's "resistant to accidental
        # duplicate ... player identities").
        conn.execute(
            "UPDATE season_player_pool SET eligible=FALSE WHERE season_id=? AND canonical_player_id=?",
            (season_id, canonical_player_id),
        )
        append_event(
            conn,
            actor=actor,
            action=CANDIDATE_DETECTED,
            entity_type=ENTITY_TYPE_MATCH_CANDIDATE,
            entity_id=candidate_id,
            after_state={
                "season_player_id": season_player_id,
                "canonical_player_id": canonical_player_id,
                "match_basis": MATCH_BASIS_GIVEN_FAMILY_NAME,
            },
        )
        detected += 1
    return detected


def _other_pending_candidates_exist(
    conn, season_id: str, canonical_player_id: int, *, excluding_season_player_id: str
) -> bool:
    """Whether some *other* provisional player still has a pending
    candidate suggestion naming this same canonical player (issue #242,
    Codex review on PR #258, P1): two provisional players can plausibly
    share a name and both match the same canonical row. Releasing
    quarantine (restoring `eligible=TRUE`) must check this first, or
    resolving/rejecting one provisional player's candidate would
    prematurely make the canonical row draftable while it is still a live,
    unresolved suggestion for the other."""
    row = conn.execute(
        "SELECT 1 FROM provisional_match_candidate WHERE season_id=? AND canonical_player_id=? "
        "AND status='pending' AND season_player_id<>?",
        (season_id, canonical_player_id, excluding_season_player_id),
    ).fetchone()
    return row is not None
