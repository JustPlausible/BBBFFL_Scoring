"""Mid-season draft workflow (issue #164): continuing the 2026 historical
replay -- and any future live season -- past its configured trigger round.

Builds on, rather than duplicating:

- the ladder read model (`app.ladder.LadderRepository`) -- confirming a
  mid-season draft order freezes an independent *copy* of one calculated
  `LadderSnapshot`. The live ladder calculation is never touched: a wrong
  underlying result/statistic is corrected through the existing audited
  match/result/player-stat pathways and the ladder recalculates, exactly as
  it always has; this module has no mechanism to rewrite it.
- the preseason draft engine (`app.draft.DraftRepository`) -- once
  delistings are locked, `generate_selection_table` computes a vacancy-based
  allocation and hands it to `DraftRepository.materialize_draft_in_transaction`
  under `draft_kind="midseason"`. From that point, selections are made
  through the very same `execute_pick`/`next_pick`/`correct_pick`/`finalize`
  engine the preseason draft uses -- turn sequencing, pick-owner resolution,
  player eligibility/availability, squad-capacity validation and
  concurrency control are not reimplemented here.
- the authoritative ownership ledger (`app.player_pool.OwnershipRepository`)
  -- every delisting release, approved player-trade leg, and draft
  selection updates `player_ownership_period` through this same ledger,
  using its `allow_closed_window` escape hatch (the preseason window is
  long closed by the time a mid-season draft runs).
- the append-only audit boundary (`app.audit`) for actor/reason/before-
  after history on every lifecycle transition, delisting, trade decision
  and exceptional correction.

## Lifecycle

Persisted, season-scoped, one row per season in `midseason_draft`::

    Round N final -> ladder_confirmed -> delisting_open -> delistings_locked
        -> draft_open -> draft_complete -> complete

`ladder_confirmed`: `confirm_ladder` requires every BBBFFL round through the
season's configured `midseason_draft_trigger_round` to be final, then
freezes an immutable copy of the calculated ladder
(`midseason_ladder_snapshot`/`_row`/`_reference`) as the draft-order basis
and seeds `midseason_draft_order` from its reverse order (last place picks
first; ties broken by season_entry_id, matching `app.ladder`'s own
documented non-sporting tie-break). `override_draft_order` may replace
`midseason_draft_order`'s rows with an audited Scorer determination at any
point before `delistings_locked` -- the frozen ladder snapshot is never
touched by an override.

`delisting_open`: `open_delisting_window` lets coaches (or an audited
Scorer proxy) submit/withdraw formal delistings, and propose/decide player
and round-based pick trades. Proposing a trade never itself changes
ownership. An approved *player* leg is applied immediately (release then
acquire, like `app.preseason.submit_trade`). An approved *pick* leg is
deferred: there is no concrete pick to own yet, so it is only applied when
`generate_selection_table` builds the allocation.

`delistings_locked`: `lock_delistings` refuses while any trade for this
draft is still `pending`. It releases every still-active delisted player's
ownership (so they immediately enter the available pool) and marks every
active delisting locked.

`draft_open`: `generate_selection_table` computes each entry's vacancy
count (configured squad limit minus its now-live squad size), allocates
picks via `vacancy_allocations` (reverse-ladder round-robin, skipping any
entry once satisfied), applies approved pick-trade legs to that allocation,
and materialises the selection table. `execute_pick` is a thin wrapper
around `DraftRepository.execute_pick(..., draft_kind="midseason")` that
also reconciles automatic completion.

`draft_complete`: entered once the final required selection completes and
`DraftRepository.finalize` accepts every resulting squad at the configured
squad size -- no separate Scorer lock is needed for this transition.
Post-draft player trades remain possible until the Scorer explicitly closes
them (`close_post_draft_trading`); Round 11's own lockout mechanism is
untouched by this module, matching the plan's "the Scorer decides when the
phase is closed."

`complete`: terminal for this module. The season proceeds into Round 11
using the resulting squads exactly as any other week reads
`player_ownership_period` -- nothing further is required here.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.audit import ActorContext, append_event, new_correlation_id
from app.db import _for_update_suffix, transaction
from app.draft import DraftRepository
from app.ladder import LadderRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository

MIDSEASON_DRAFT_KIND = "midseason"

STATES = (
    "ladder_confirmed",
    "delisting_open",
    "delistings_locked",
    "draft_open",
    "draft_complete",
    "complete",
)


def _id():
    return str(uuid4())


def _now():
    return datetime.now(timezone.utc).isoformat()


class MidseasonDraftStateError(ValueError):
    pass


class MidseasonRoundNotFinalError(MidseasonDraftStateError):
    pass


class MidseasonDraftExistsError(MidseasonDraftStateError):
    pass


class MidseasonPendingTradesError(MidseasonDraftStateError):
    """Raised by `lock_delistings` while one or more trades for this draft
    are still `pending`. `.trade_ids` names every one so a caller can
    resolve (approve/reject) each before retrying -- see the module
    docstring's `delistings_locked` boundary."""

    def __init__(self, message, trade_ids):
        super().__init__(message)
        self.trade_ids = trade_ids


class MidseasonTradeValidationError(MidseasonDraftStateError):
    """Raised by `propose_trade` when one or more legs are invalid.
    `.issues` lists every problem found; nothing is written when this is
    raised."""

    def __init__(self, message, issues):
        super().__init__(message)
        self.issues = issues


class MidseasonPickReconciliationError(MidseasonDraftStateError):
    """Raised by `lock_delistings` when the planned selection allocation
    (see `_plan_selection_allocations`) doesn't cleanly account for every
    approved round-based pick-trade leg, or an entry's roster itself is
    still over its configured limit. Three distinct defects are reported
    together, any of which is enough to raise:

    - `.overfull`: one or more entries' *live* squad size still exceeds the
      configured squad limit -- e.g. a player-for-pick acquisition was
      tolerated (`decide_trade`'s `allow_capacity_overage`) because an
      active delisting covered it at approval time, but that delisting was
      later withdrawn before lock. Vacancy itself clamps to 0 for such an
      entry, which would otherwise silently hide the overage from
      `.mismatched` (both sides read 0).
    - `.mismatched`: one or more entries' final selection counts don't
      exactly match their own roster vacancies -- either direction is a
      defect: too many would strand the draft (a pick for that entry could
      never be legally executed once its squad is full), too few would
      silently leave that entry's roster permanently short.
    - `.unapplied_leg_ids`: one or more approved pick legs have no
      allocation to redirect at all (their named `from_season_entry_id` has
      no vacancy in that round -- e.g. a player-for-pick trade whose
      incoming player and matching delisting exactly cancelled out the
      sender's own vacancy). Left unchecked this would silently honour the
      ownership-mutating side of a trade while quietly dropping its pick
      side, with only an easily-missed audit-event field to show for it.

    Raising here rolls back the whole lock (including any delisted-player
    releases already applied in this same transaction) and leaves the
    delisting window open, so the Scorer can submit another delisting,
    rebalance with a compensating trade, or -- since a pending proposal can
    be rejected but an *approved* trade cannot, `decide_trade` only ever
    accepting a pending one -- reverse the offending trade's approval
    outright with `reverse_trade_approval`, before retrying the lock."""

    def __init__(self, message, mismatched, unapplied_leg_ids=(), overfull=None):
        super().__init__(message)
        self.mismatched = mismatched
        self.unapplied_leg_ids = list(unapplied_leg_ids)
        self.overfull = dict(overfull or {})


@dataclass(frozen=True)
class MidseasonDraft:
    midseason_draft_id: str
    season_id: str
    competition_id: str
    trigger_round_sequence: int
    state: str
    ladder_confirmed_at: str
    delisting_opened_at: str | None
    delistings_locked_at: str | None
    draft_completed_at: str | None
    completed_at: str | None
    created_at: str
    updated_at: str
    version: int


@dataclass(frozen=True)
class LadderSnapshotRow:
    season_entry_id: str
    rank: int
    tied: bool
    played: int
    wins: int
    draws: int
    losses: int
    points_for: Decimal
    points_against: Decimal
    percentage: Decimal
    competition_points: int


@dataclass(frozen=True)
class LadderOrderSnapshot:
    snapshot_id: str
    through_round: int
    created_at: str
    rows: tuple[LadderSnapshotRow, ...]


@dataclass(frozen=True)
class Delisting:
    delisting_id: str
    midseason_draft_id: str
    season_entry_id: str
    season_player_id: str
    submitted_at: str
    withdrawn_at: str | None
    locked_at: str | None
    reason: str | None


@dataclass(frozen=True)
class MidseasonTrade:
    trade_id: str
    midseason_draft_id: str
    status: str
    proposed_at: str
    decided_at: str | None
    decision_reason: str | None
    correlation_id: str
    audit_event_id: str


@dataclass(frozen=True)
class MidseasonTradeLeg:
    leg_id: str
    trade_id: str
    leg_type: str
    from_season_entry_id: str
    to_season_entry_id: str
    season_player_id: str | None
    draft_round: int | None


def vacancy_allocations(ordered_entry_ids, vacancies_by_entry):
    """Yield `(overall, round_number, round_position, original_entry_id,
    current_entry_id)` in confirmed team order, round after round, skipping
    any entry once its vacancy count is exhausted -- the mid-season
    equivalent of `app.draft.snake_allocations`, except each entry's number
    of picks varies with its own vacancy count rather than a uniform target
    squad size ("teams skipped once they no longer require a selection").

    Deliberately a plain round-robin, not a snake: the plan describes
    vacancy-based skipping, never round-to-round order reversal, so pick
    order within every round is the same confirmed order (reverse ladder,
    or its audited override).

    `original_entry_id`/`current_entry_id` are always equal here; a caller
    applying approved round-based pick trades reassigns `current_entry_id`
    on the yielded allocations before materialising them.
    """
    remaining = dict(vacancies_by_entry)
    overall = 0
    round_number = 0
    while any(remaining.get(entry_id, 0) > 0 for entry_id in ordered_entry_ids):
        round_number += 1
        round_position = 0
        for entry_id in ordered_entry_ids:
            if remaining.get(entry_id, 0) <= 0:
                continue
            round_position += 1
            overall += 1
            remaining[entry_id] -= 1
            yield overall, round_number, round_position, entry_id, entry_id


class MidseasonDraftRepository:
    def __init__(self, database):
        self.database = database
        self.ownership = OwnershipRepository(database)
        self.player_pool = PlayerPoolRepository(database)
        self.drafts = DraftRepository(database)

    # -- Lifecycle reads -----------------------------------------------

    def get_draft(self, season_id):
        row = self.database.execute("SELECT * FROM midseason_draft WHERE season_id=?", (season_id,)).fetchone()
        return MidseasonDraft(**dict(row)) if row else None

    def _locked_draft(self, conn, season_id):
        row = conn.execute(
            "SELECT * FROM midseason_draft WHERE season_id=?" + _for_update_suffix(self.database), (season_id,)
        ).fetchone()
        if not row:
            raise KeyError(season_id)
        return row

    def _require_rounds_final(self, conn, competition_id, trigger_round):
        rows = conn.execute(
            "SELECT fixture_round_number, state FROM bbbffl_round_lifecycle "
            "WHERE competition_id=? AND fixture_round_number<=?",
            (competition_id, trigger_round),
        ).fetchall()
        present = {row["fixture_round_number"] for row in rows}
        missing = sorted(set(range(1, trigger_round + 1)) - present)
        not_final = sorted(row["fixture_round_number"] for row in rows if row["state"] != "final")
        if missing or not_final:
            raise MidseasonRoundNotFinalError(
                f"every BBBFFL round through round {trigger_round} must be final before the mid-season "
                f"draft can begin (missing: {missing}, not yet final: {not_final})"
            )

    # -- 1. Confirm the ladder snapshot and derive the draft order ------

    def confirm_ladder(
        self, season_id, competition_id, *, actor=ActorContext.anonymous_operator("scorer"), reason=None
    ):
        """Round 10 (or whichever round the season configures) must be
        fully final. Freezes an immutable copy of the calculated ladder and
        seeds the draft order from its reverse (worst-placed team picks
        first); never mutates or reorders the live ladder itself."""
        with transaction(self.database) as conn:
            season = conn.execute(
                "SELECT * FROM bbbffl_season WHERE season_id=?" + _for_update_suffix(self.database), (season_id,)
            ).fetchone()
            if not season:
                raise KeyError(season_id)
            trigger = (
                season["midseason_draft_trigger_round"] if "midseason_draft_trigger_round" in season.keys() else None
            )
            if trigger is None:
                raise MidseasonDraftStateError("season has no configured mid-season draft trigger round")
            if conn.execute("SELECT 1 FROM midseason_draft WHERE season_id=?", (season_id,)).fetchone():
                raise MidseasonDraftExistsError("a mid-season draft already exists for this season")
            competition = conn.execute(
                "SELECT season_id, stream_type FROM competition_stream WHERE competition_id=?", (competition_id,)
            ).fetchone()
            if not competition or competition["season_id"] != season_id or competition["stream_type"] != "ordinary":
                raise MidseasonDraftStateError(
                    "competition_id must name an ordinary competition belonging to this season"
                )
            self._require_rounds_final(conn, competition_id, trigger)
            entries = [
                row["season_entry_id"]
                for row in conn.execute(
                    "SELECT season_entry_id FROM season_entry WHERE season_id=?", (season_id,)
                ).fetchall()
            ]
            if not entries:
                raise MidseasonDraftStateError("season has no entries")

        # A pure read of the (deliberately never locked) calculated ladder
        # -- see the module docstring. Taken outside the write transaction,
        # like every other caller of this read model.
        ladder = LadderRepository(self.database).snapshot(competition_id, trigger)

        with transaction(self.database) as conn:
            if conn.execute(
                "SELECT 1 FROM midseason_draft WHERE season_id=?" + _for_update_suffix(self.database), (season_id,)
            ).fetchone():
                raise MidseasonDraftExistsError("a mid-season draft already exists for this season")
            # Re-lock and re-read the season's trigger round: between the
            # first transaction releasing its lock and this one starting,
            # `set_midseason_draft_trigger_round` could have changed it --
            # its own guard only refuses once a `midseason_draft` row
            # exists, and that row is not inserted until this transaction.
            # The ladder snapshot above was already computed against the
            # *old* trigger, so silently freezing it under a since-changed
            # trigger would leave the season's configuration permanently
            # inconsistent with its own immutable snapshot (worse, once
            # this insert lands the setter refuses to ever change it
            # again). Refuse and let the caller retry `confirm_ladder` from
            # scratch instead -- nothing has been written yet.
            current_season = conn.execute(
                "SELECT midseason_draft_trigger_round FROM bbbffl_season WHERE season_id=?"
                + _for_update_suffix(self.database),
                (season_id,),
            ).fetchone()
            current_trigger = (
                current_season["midseason_draft_trigger_round"]
                if current_season and "midseason_draft_trigger_round" in current_season.keys()
                else None
            )
            if current_trigger != trigger:
                raise MidseasonDraftStateError(
                    f"the season's configured trigger round changed from {trigger} to {current_trigger} while "
                    "confirming the ladder -- retry confirm_ladder against the current configuration"
                )
            midseason_draft_id, now = _id(), _now()
            conn.execute(
                "INSERT INTO midseason_draft VALUES "
                "(?, ?, ?, ?, 'ladder_confirmed', ?, NULL, NULL, NULL, NULL, ?, ?, 1)",
                (midseason_draft_id, season_id, competition_id, trigger, now, now, now),
            )
            snapshot_id = _id()
            conn.execute(
                "INSERT INTO midseason_ladder_snapshot VALUES (?, ?, ?, ?)",
                (snapshot_id, midseason_draft_id, trigger, now),
            )
            for row in ladder.rows:
                conn.execute(
                    "INSERT INTO midseason_ladder_snapshot_row VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _id(),
                        snapshot_id,
                        row.season_entry_id,
                        row.rank,
                        row.tied,
                        row.played,
                        row.wins,
                        row.draws,
                        row.losses,
                        str(row.points_for),
                        str(row.points_against),
                        str(row.percentage),
                        row.competition_points,
                    ),
                )
            for reference in ladder.result_references:
                conn.execute(
                    "INSERT INTO midseason_ladder_snapshot_reference VALUES (?, ?, ?, ?)",
                    (_id(), snapshot_id, reference.matchup_id, reference.official_version),
                )
            # Reverse ladder order: last place selects first. Ties (same
            # rank) are broken by season_entry_id -- a stable, documented,
            # non-sporting tie-break identical in spirit to app.ladder's own
            # serialization convention; a genuine competition-determined
            # exception is available via `override_draft_order`.
            ordered_entry_ids = [
                row.season_entry_id for row in sorted(ladder.rows, key=lambda row: (-row.rank, row.season_entry_id))
            ]
            for position, entry_id in enumerate(ordered_entry_ids, 1):
                conn.execute(
                    "INSERT INTO midseason_draft_order VALUES (?, ?, ?, 'ladder')",
                    (midseason_draft_id, position, entry_id),
                )
            append_event(
                conn,
                actor=actor,
                action="midseason.ladder.confirmed",
                entity_type="midseason.draft",
                entity_id=midseason_draft_id,
                reason=reason,
                after_state={
                    "season_id": season_id,
                    "competition_id": competition_id,
                    "trigger_round_sequence": trigger,
                    "order": ordered_entry_ids,
                },
            )
        return self.get_draft(season_id)

    def ladder_snapshot(self, season_id):
        draft = self.get_draft(season_id)
        if not draft:
            return None
        snap = self.database.execute(
            "SELECT * FROM midseason_ladder_snapshot WHERE midseason_draft_id=?", (draft.midseason_draft_id,)
        ).fetchone()
        if not snap:
            return None
        rows = self.database.execute(
            "SELECT * FROM midseason_ladder_snapshot_row WHERE snapshot_id=? ORDER BY rank, season_entry_id",
            (snap["snapshot_id"],),
        ).fetchall()
        return LadderOrderSnapshot(
            snapshot_id=snap["snapshot_id"],
            through_round=snap["through_round"],
            created_at=snap["created_at"],
            rows=tuple(
                LadderSnapshotRow(
                    season_entry_id=row["season_entry_id"],
                    rank=row["rank"],
                    tied=bool(row["tied"]),
                    played=row["played"],
                    wins=row["wins"],
                    draws=row["draws"],
                    losses=row["losses"],
                    points_for=Decimal(row["points_for"]),
                    points_against=Decimal(row["points_against"]),
                    percentage=Decimal(row["percentage"]),
                    competition_points=row["competition_points"],
                )
                for row in rows
            ),
        )

    def draft_order(self, season_id):
        draft = self.get_draft(season_id)
        if not draft:
            return []
        rows = self.database.execute(
            "SELECT position, season_entry_id, source FROM midseason_draft_order "
            "WHERE midseason_draft_id=? ORDER BY position",
            (draft.midseason_draft_id,),
        ).fetchall()
        return [(row["position"], row["season_entry_id"], row["source"]) for row in rows]

    def override_draft_order(self, season_id, ordered_entry_ids, *, actor, reason):
        """An audited Scorer/Admin determination of the mid-season draft
        order, for a genuine unresolved tie or an exceptional competition
        decision -- never touches `midseason_ladder_snapshot`."""
        if not reason or not reason.strip():
            raise ValueError("a draft-order override requires an explicit reason")
        ordered_entry_ids = list(ordered_entry_ids)
        with transaction(self.database) as conn:
            draft = self._locked_draft(conn, season_id)
            if draft["state"] not in ("ladder_confirmed", "delisting_open"):
                raise MidseasonDraftStateError("draft order can only be overridden before delistings are locked")
            current = conn.execute(
                "SELECT position, season_entry_id FROM midseason_draft_order "
                "WHERE midseason_draft_id=? ORDER BY position",
                (draft["midseason_draft_id"],),
            ).fetchall()
            expected = {row["season_entry_id"] for row in current}
            if len(ordered_entry_ids) != len(expected) or set(ordered_entry_ids) != expected:
                raise MidseasonDraftStateError("override must reorder exactly the existing draft-order entries")
            conn.execute("DELETE FROM midseason_draft_order WHERE midseason_draft_id=?", (draft["midseason_draft_id"],))
            for position, entry_id in enumerate(ordered_entry_ids, 1):
                conn.execute(
                    "INSERT INTO midseason_draft_order VALUES (?, ?, ?, 'override')",
                    (draft["midseason_draft_id"], position, entry_id),
                )
            now = _now()
            conn.execute(
                "UPDATE midseason_draft SET updated_at=?, version=version+1 WHERE midseason_draft_id=?",
                (now, draft["midseason_draft_id"]),
            )
            append_event(
                conn,
                actor=actor,
                action="midseason.draft_order.overridden",
                entity_type="midseason.draft",
                entity_id=draft["midseason_draft_id"],
                reason=reason,
                before_state={"order": [row["season_entry_id"] for row in current]},
                after_state={"order": ordered_entry_ids},
            )
        return self.draft_order(season_id)

    # -- 2. Open formal delisting/trading --------------------------------

    def open_delisting_window(self, season_id, *, actor=ActorContext.anonymous_operator("scorer"), reason=None):
        with transaction(self.database) as conn:
            draft = self._locked_draft(conn, season_id)
            if draft["state"] != "ladder_confirmed":
                raise MidseasonDraftStateError(
                    "the delisting window can only be opened from the ladder-confirmed state"
                )
            now = _now()
            conn.execute(
                "UPDATE midseason_draft SET state='delisting_open', delisting_opened_at=?, updated_at=?, "
                "version=version+1 WHERE midseason_draft_id=?",
                (now, now, draft["midseason_draft_id"]),
            )
            append_event(
                conn,
                actor=actor,
                action="midseason.delisting_window.opened",
                entity_type="midseason.draft",
                entity_id=draft["midseason_draft_id"],
                reason=reason,
                before_state={"state": "ladder_confirmed"},
                after_state={"state": "delisting_open"},
            )
        return self.get_draft(season_id)

    # -- 3. Formal delistings ---------------------------------------------

    def submit_delisting(self, season_id, season_entry_id, season_player_id, *, actor, reason=None):
        """Formal entry of a delisting -- a Scorer/Admin proxy may call this
        directly on a coach's behalf (issue #164's 2026-replay allowance);
        the actor recorded is whoever this caller passes."""
        with transaction(self.database) as conn:
            draft = self._locked_draft(conn, season_id)
            if draft["state"] != "delisting_open":
                raise MidseasonDraftStateError("delistings can only be submitted while the delisting window is open")
            owner = conn.execute(
                "SELECT season_entry_id FROM player_ownership_period WHERE season_player_id=? AND released_at IS NULL"
                + _for_update_suffix(self.database),
                (season_player_id,),
            ).fetchone()
            if not owner or owner["season_entry_id"] != season_entry_id:
                raise MidseasonDraftStateError("player is not currently owned by that entry")
            existing = conn.execute(
                "SELECT 1 FROM midseason_delisting WHERE midseason_draft_id=? AND season_player_id=? "
                "AND withdrawn_at IS NULL" + _for_update_suffix(self.database),
                (draft["midseason_draft_id"], season_player_id),
            ).fetchone()
            if existing:
                raise MidseasonDraftStateError("player is already formally delisted")
            delisting_id, now = _id(), _now()
            conn.execute(
                "INSERT INTO midseason_delisting VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)",
                (delisting_id, draft["midseason_draft_id"], season_entry_id, season_player_id, now, reason),
            )
            append_event(
                conn,
                actor=actor,
                action="midseason.delisting.submitted",
                entity_type="midseason.delisting",
                entity_id=delisting_id,
                reason=reason,
                after_state={"season_entry_id": season_entry_id, "season_player_id": season_player_id},
            )
        return self.get_delisting(delisting_id)

    def withdraw_delisting(self, season_id, delisting_id, *, actor, reason=None):
        """Amendment before lock: a formal delisting may be withdrawn (e.g.
        because a trade opportunity has arisen) and, if still wanted,
        resubmitted -- there is no separate "amend in place" call."""
        with transaction(self.database) as conn:
            draft = self._locked_draft(conn, season_id)
            if draft["state"] != "delisting_open":
                raise MidseasonDraftStateError("delistings can only be withdrawn while the delisting window is open")
            row = conn.execute(
                "SELECT * FROM midseason_delisting WHERE delisting_id=?" + _for_update_suffix(self.database),
                (delisting_id,),
            ).fetchone()
            if not row or row["midseason_draft_id"] != draft["midseason_draft_id"]:
                raise KeyError(delisting_id)
            if row["withdrawn_at"] is not None:
                raise MidseasonDraftStateError("delisting is already withdrawn")
            now = _now()
            conn.execute("UPDATE midseason_delisting SET withdrawn_at=? WHERE delisting_id=?", (now, delisting_id))
            append_event(
                conn,
                actor=actor,
                action="midseason.delisting.withdrawn",
                entity_type="midseason.delisting",
                entity_id=delisting_id,
                reason=reason,
                before_state={"withdrawn_at": None},
                after_state={"withdrawn_at": now},
            )
        return self.get_delisting(delisting_id)

    def get_delisting(self, delisting_id):
        row = self.database.execute(
            "SELECT * FROM midseason_delisting WHERE delisting_id=?", (delisting_id,)
        ).fetchone()
        return Delisting(**dict(row)) if row else None

    def list_delistings(self, season_id, *, include_withdrawn=True):
        draft = self.get_draft(season_id)
        if not draft:
            return []
        clause = "" if include_withdrawn else " AND withdrawn_at IS NULL"
        rows = self.database.execute(
            f"SELECT * FROM midseason_delisting WHERE midseason_draft_id=?{clause} ORDER BY submitted_at",
            (draft.midseason_draft_id,),
        ).fetchall()
        return [Delisting(**dict(row)) for row in rows]

    # -- Trades: player and round-based pick legs ------------------------

    def propose_trade(self, season_id, legs, *, actor, reason=None):
        """Every leg is validated before anything is written; proposing a
        trade never itself changes player or pick ownership -- see the
        module docstring's `delisting_open` section. `legs` is a sequence of
        mappings with `leg_type` ('player' or 'pick'),
        `from_season_entry_id`, `to_season_entry_id`, and either
        `season_player_id` (player leg) or `draft_round` (pick leg, a
        positive integer round number)."""
        legs = [dict(leg) for leg in legs]
        if not legs:
            raise MidseasonTradeValidationError("a trade requires at least one leg", issues=[])
        with transaction(self.database) as conn:
            draft = self._locked_draft(conn, season_id)
            if draft["state"] not in ("delisting_open", "draft_complete"):
                raise MidseasonDraftStateError(
                    "trades can only be proposed while the delisting window is open, or after the draft completes"
                )
            issues = []
            resolved = []
            seen_pick_entitlements = set()
            for index, leg in enumerate(legs):
                leg_type = leg.get("leg_type")
                from_entry = leg.get("from_season_entry_id")
                to_entry = leg.get("to_season_entry_id")
                if leg_type not in ("player", "pick") or not from_entry or not to_entry:
                    issues.append(
                        {"leg": index, "problem": "leg_type, from_season_entry_id and to_season_entry_id are required"}
                    )
                    continue
                if from_entry == to_entry:
                    issues.append({"leg": index, "problem": "from and to entry must differ"})
                    continue
                from_row = conn.execute(
                    "SELECT season_id FROM season_entry WHERE season_entry_id=?", (from_entry,)
                ).fetchone()
                to_row = conn.execute(
                    "SELECT season_id FROM season_entry WHERE season_entry_id=?", (to_entry,)
                ).fetchone()
                if not from_row or from_row["season_id"] != season_id or not to_row or to_row["season_id"] != season_id:
                    issues.append({"leg": index, "problem": "entries must belong to this season"})
                    continue
                if leg_type == "pick" and draft["state"] != "delisting_open":
                    issues.append(
                        {"leg": index, "problem": "round-based pick trades are only available before delistings lock"}
                    )
                    continue
                if leg_type == "player":
                    player_id = leg.get("season_player_id")
                    if not player_id:
                        issues.append({"leg": index, "problem": "season_player_id is required for a player leg"})
                        continue
                    current = conn.execute(
                        "SELECT season_entry_id FROM player_ownership_period "
                        "WHERE season_player_id=? AND released_at IS NULL",
                        (player_id,),
                    ).fetchone()
                    if not current or current["season_entry_id"] != from_entry:
                        issues.append({"leg": index, "problem": f"player is not currently owned by entry {from_entry}"})
                        continue
                    resolved.append(
                        {
                            "leg_type": "player",
                            "from_entry": from_entry,
                            "to_entry": to_entry,
                            "season_player_id": player_id,
                            "draft_round": None,
                        }
                    )
                else:
                    draft_round = leg.get("draft_round")
                    if not isinstance(draft_round, int) or isinstance(draft_round, bool) or draft_round <= 0:
                        issues.append(
                            {"leg": index, "problem": "draft_round must be a positive integer for a pick leg"}
                        )
                        continue
                    entitlement = (draft_round, from_entry)
                    if entitlement in seen_pick_entitlements:
                        issues.append(
                            {
                                "leg": index,
                                "problem": (
                                    f"entry {from_entry}'s round {draft_round} pick is already committed to "
                                    "another leg of this same trade"
                                ),
                            }
                        )
                        continue
                    seen_pick_entitlements.add(entitlement)
                    resolved.append(
                        {
                            "leg_type": "pick",
                            "from_entry": from_entry,
                            "to_entry": to_entry,
                            "season_player_id": None,
                            "draft_round": draft_round,
                        }
                    )
            if issues:
                raise MidseasonTradeValidationError(
                    "mid-season trade rejected: every leg of a trade must be valid", issues=issues
                )
            trade_id, correlation, now = _id(), new_correlation_id(), _now()
            event = append_event(
                conn,
                actor=actor,
                action="midseason.trade.proposed",
                entity_type="midseason.trade",
                entity_id=trade_id,
                correlation_id=correlation,
                reason=reason,
                after_state={"legs": resolved},
            )
            conn.execute(
                "INSERT INTO midseason_trade VALUES (?, ?, 'pending', ?, NULL, NULL, ?, ?)",
                (trade_id, draft["midseason_draft_id"], now, correlation, event.event_id),
            )
            for leg in resolved:
                conn.execute(
                    "INSERT INTO midseason_trade_leg VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        _id(),
                        trade_id,
                        leg["leg_type"],
                        leg["from_entry"],
                        leg["to_entry"],
                        leg["season_player_id"],
                        leg["draft_round"],
                    ),
                )
        return self.get_trade(trade_id)

    def decide_trade(self, season_id, trade_id, approve, *, actor, reason=None):
        """Only an authorised Scorer/Admin decision changes ownership --
        approval applies every *player* leg immediately (release every leg
        first, then acquire every leg, matching `app.preseason.submit_trade`
        so a multi-leg trade sees each entry's net position). A *pick* leg
        is recorded as approved but not applied until
        `generate_selection_table` builds the allocation.

        Player-leg acquisitions only bypass the normal squad-capacity
        ceiling (`allow_capacity_overage=True`) when the resulting overage
        is demonstrably temporary: the draft must still be `delisting_open`
        (there is no later lock step in `draft_complete` to ever resolve
        it), and the receiving entry must hold enough still-active
        delistings under this same draft to cover the excess once they
        release at `lock_delistings`. Anything else -- an overage in
        `draft_complete`, or one with no covering delisting -- refuses the
        whole decision with the ordinary squad-capacity ceiling in force,
        rather than silently creating a squad that nothing will ever bring
        back down to its limit. This does not relax anything else: a stale
        or duplicate leg is still refused below, and `lock_delistings`
        itself re-validates that every entry's final pick allocation
        reconciles with its actual vacancies before it ever commits.

        Only legal while the draft is in `delisting_open` or
        `draft_complete` -- the two states in which a trade can legitimately
        still be pending (`lock_delistings` already refuses to run while any
        trade is pending, so no trade is ever pending during
        `delistings_locked`/`draft_open`); once `close_post_draft_trading`
        moves the draft to the terminal `complete` state, a trade left
        pending through that boundary can no longer be decided at all,
        never approved to mutate ownership after the phase has closed.

        Re-verifies each player leg's claimed `from_season_entry_id` against
        the *current* owner under this transaction's lock before releasing
        it -- a proposal's ownership was only ever checked at proposal time,
        and a different trade approved in between can have moved the player
        on since. A stale leg refuses the whole decision rather than
        silently releasing a new owner's player on an old declaration.
        """
        with transaction(self.database) as conn:
            draft = self._locked_draft(conn, season_id)
            if draft["state"] not in ("delisting_open", "draft_complete"):
                raise MidseasonDraftStateError(
                    "trades can only be decided while the delisting window is open, or after the draft completes"
                )
            trade = conn.execute(
                "SELECT * FROM midseason_trade WHERE trade_id=?" + _for_update_suffix(self.database), (trade_id,)
            ).fetchone()
            if not trade or trade["midseason_draft_id"] != draft["midseason_draft_id"]:
                raise KeyError(trade_id)
            if trade["status"] != "pending":
                raise MidseasonDraftStateError(f"trade is already {trade['status']}")
            legs = conn.execute("SELECT * FROM midseason_trade_leg WHERE trade_id=?", (trade_id,)).fetchall()
            now = _now()
            new_status = "approved" if approve else "rejected"
            if approve:
                for leg in legs:
                    if leg["leg_type"] != "player":
                        continue
                    current = conn.execute(
                        "SELECT season_entry_id FROM player_ownership_period "
                        "WHERE season_player_id=? AND released_at IS NULL" + _for_update_suffix(self.database),
                        (leg["season_player_id"],),
                    ).fetchone()
                    if not current or current["season_entry_id"] != leg["from_season_entry_id"]:
                        raise MidseasonDraftStateError(
                            f"player {leg['season_player_id']} is no longer owned by "
                            f"{leg['from_season_entry_id']} -- this trade is stale and cannot be approved"
                        )
                for leg in legs:
                    if leg["leg_type"] != "pick":
                        continue
                    # A pick entitlement is not tracked by a live ownership
                    # row the way a player is, so two separate proposals can
                    # both claim to sell the same (round, from_entity)
                    # entitlement without either one being individually
                    # invalid at proposal time. Refuse here, at approval,
                    # rather than letting `generate_selection_table` silently
                    # apply whichever one happens to sort last and discard
                    # the other's already-audited approval.
                    conflicting = conn.execute(
                        "SELECT t.trade_id FROM midseason_trade_leg l JOIN midseason_trade t ON t.trade_id=l.trade_id "
                        "WHERE t.midseason_draft_id=? AND t.status='approved' AND l.leg_type='pick' "
                        "AND l.draft_round=? AND l.from_season_entry_id=?" + _for_update_suffix(self.database),
                        (draft["midseason_draft_id"], leg["draft_round"], leg["from_season_entry_id"]),
                    ).fetchone()
                    if conflicting:
                        raise MidseasonDraftStateError(
                            f"entry {leg['from_season_entry_id']}'s round {leg['draft_round']} pick was already "
                            f"traded away in an approved trade ({conflicting['trade_id']}) -- reject or withdraw "
                            "the conflicting proposal first"
                        )
                for leg in legs:
                    if leg["leg_type"] != "player":
                        continue
                    self.ownership.release_in_transaction(
                        conn,
                        leg["season_player_id"],
                        effective_at=now,
                        actor=actor,
                        reason=reason or "mid-season trade approved",
                        correlation_id=trade["correlation_id"],
                        allow_closed_window=True,
                    )
                    # A trade moving a player supersedes any still-active
                    # formal delisting of that same player -- the delisting
                    # was that player's *previous* owner's declaration, and
                    # `lock_delistings` must never release a player from
                    # whoever now actually owns it based on that stale
                    # declaration. Auto-withdraw it, audited, as part of
                    # this approval rather than leaving it to silently
                    # mismatch at lock time.
                    stale_delisting = conn.execute(
                        "SELECT * FROM midseason_delisting WHERE midseason_draft_id=? AND season_player_id=? "
                        "AND withdrawn_at IS NULL" + _for_update_suffix(self.database),
                        (draft["midseason_draft_id"], leg["season_player_id"]),
                    ).fetchone()
                    if stale_delisting:
                        conn.execute(
                            "UPDATE midseason_delisting SET withdrawn_at=? WHERE delisting_id=?",
                            (now, stale_delisting["delisting_id"]),
                        )
                        append_event(
                            conn,
                            actor=actor,
                            action="midseason.delisting.withdrawn",
                            entity_type="midseason.delisting",
                            entity_id=stale_delisting["delisting_id"],
                            correlation_id=trade["correlation_id"],
                            reason="superseded by an approved trade moving this player",
                            before_state={"withdrawn_at": None},
                            after_state={"withdrawn_at": now},
                        )
                player_legs = [leg for leg in legs if leg["leg_type"] == "player"]
                if player_legs:
                    config = conn.execute(
                        "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?"
                        + _for_update_suffix(self.database),
                        (season_id,),
                    ).fetchone()
                    if not config:
                        raise MidseasonDraftStateError(
                            "season squad limit must be configured before deciding a player trade"
                        )
                    squad_limit = config["squad_limit"]
                for leg in player_legs:
                    to_entry = leg["to_season_entry_id"]
                    # PostgreSQL rejects `SELECT COUNT(*) ... FOR UPDATE` (FOR
                    # UPDATE is not allowed against an aggregate). Lock the
                    # receiving entry's own row first -- the same parent-row
                    # lock `OwnershipRepository.acquire_in_transaction` always
                    # takes before it validates squad capacity -- so a
                    # concurrent acquisition into this entry (mid-season
                    # draft pick, another trade, a direct correction) is
                    # blocked until this decision commits, then count the
                    # now-stable ownership rows without a lock.
                    conn.execute(
                        "SELECT season_entry_id FROM season_entry WHERE season_entry_id=?"
                        + _for_update_suffix(self.database),
                        (to_entry,),
                    ).fetchone()
                    live_count = conn.execute(
                        "SELECT COUNT(*) AS n FROM player_ownership_period "
                        "WHERE season_entry_id=? AND released_at IS NULL",
                        (to_entry,),
                    ).fetchone()["n"]
                    allow_overage = False
                    if live_count + 1 > squad_limit:
                        if draft["state"] != "delisting_open":
                            raise MidseasonDraftStateError(
                                f"approving this trade would leave entry {to_entry} above its squad limit of "
                                f"{squad_limit}, and there is no later delisting lock outside the delisting "
                                "window to ever resolve it -- reject this trade instead"
                            )
                        # Not separately locked: the whole draft row is
                        # already held FOR UPDATE by `_locked_draft` above,
                        # and every delisting mutation (`submit_delisting`,
                        # `withdraw_delisting`) takes that same lock before
                        # touching `midseason_delisting`, so this count is
                        # already race-free without an (invalid) aggregate
                        # FOR UPDATE of its own.
                        active_delistings = conn.execute(
                            "SELECT COUNT(*) AS n FROM midseason_delisting WHERE midseason_draft_id=? "
                            "AND season_entry_id=? AND withdrawn_at IS NULL",
                            (draft["midseason_draft_id"], to_entry),
                        ).fetchone()["n"]
                        if live_count + 1 - active_delistings > squad_limit:
                            raise MidseasonDraftStateError(
                                f"approving this trade would leave entry {to_entry} above its squad limit of "
                                f"{squad_limit} even once its {active_delistings} active delisting(s) release -- "
                                "reject this trade, or submit a covering delisting first"
                            )
                        allow_overage = True
                    self.ownership.acquire_in_transaction(
                        conn,
                        leg["season_player_id"],
                        to_entry,
                        effective_at=now,
                        actor=actor,
                        reason=reason or "mid-season trade approved",
                        correlation_id=trade["correlation_id"],
                        allow_closed_window=True,
                        allow_capacity_overage=allow_overage,
                    )
            conn.execute(
                "UPDATE midseason_trade SET status=?, decided_at=?, decision_reason=? WHERE trade_id=?",
                (new_status, now, reason, trade_id),
            )
            append_event(
                conn,
                actor=actor,
                action=f"midseason.trade.{new_status}",
                entity_type="midseason.trade",
                entity_id=trade_id,
                correlation_id=trade["correlation_id"],
                reason=reason,
                before_state={"status": "pending"},
                after_state={"status": new_status},
            )
        return self.get_trade(trade_id)

    def reverse_trade_approval(self, season_id, trade_id, *, actor, reason):
        """Exceptional Scorer/Admin correction: undoes an already-*approved*
        trade while the delisting window is still open. Exists specifically
        for a round-based pick leg approved in good faith that later turns
        out to be undeliverable -- `lock_delistings`'
        `MidseasonPickReconciliationError.unapplied_leg_ids` names it -- and
        for which there is otherwise no way back: `decide_trade` only ever
        accepts a *pending* trade, and once approved a trade has no path to
        `pending` again.

        Reverses every player leg exactly as if the trade had never been
        approved: validates every leg's current owner first, then releases
        every leg, then reacquires every leg back to its original owner --
        matching `decide_trade`'s own release-all-then-acquire-all
        ordering, so a balanced player-for-player swap between two full
        squads can still be reversed (reversing leg-by-leg instead would
        try to reacquire into an entry still holding the *other* incoming
        player, over capacity). Refuses outright if a player has since
        moved on again, for the same staleness reason `decide_trade`
        itself refuses a stale leg. Also auto-withdraws any delisting the
        current holder has since placed on that same player (mirroring
        what approval itself does), since it would otherwise be a stale
        declaration about a player this reversal is taking away from them.
        Simply drops every pick leg's `approved` status by moving the whole
        trade to `rejected`, so `_plan_selection_allocations` (and
        therefore `lock_delistings`) stops considering it at all. Requires
        an explicit reason, like every other exceptional correction in this
        module.

        Only legal while `delisting_open`: once delistings are locked, no
        pick leg is ever applied by anything but `generate_selection_table`
        (there is nothing left to unstick), and an approved trade in
        `draft_complete` is real post-draft trading this correction path
        does not cover."""
        if not reason or not reason.strip():
            raise ValueError("reversing an approved trade requires an explicit reason")
        with transaction(self.database) as conn:
            draft = self._locked_draft(conn, season_id)
            if draft["state"] != "delisting_open":
                raise MidseasonDraftStateError(
                    "an approved trade can only be reversed while the delisting window is open"
                )
            trade = conn.execute(
                "SELECT * FROM midseason_trade WHERE trade_id=?" + _for_update_suffix(self.database), (trade_id,)
            ).fetchone()
            if not trade or trade["midseason_draft_id"] != draft["midseason_draft_id"]:
                raise KeyError(trade_id)
            if trade["status"] != "approved":
                raise MidseasonDraftStateError(
                    f"only an approved trade can be reversed (this one is {trade['status']})"
                )
            legs = conn.execute("SELECT * FROM midseason_trade_leg WHERE trade_id=?", (trade_id,)).fetchall()
            player_legs = [leg for leg in legs if leg["leg_type"] == "player"]
            now = _now()
            correlation = new_correlation_id()
            for leg in player_legs:
                current = conn.execute(
                    "SELECT season_entry_id FROM player_ownership_period "
                    "WHERE season_player_id=? AND released_at IS NULL" + _for_update_suffix(self.database),
                    (leg["season_player_id"],),
                ).fetchone()
                if not current or current["season_entry_id"] != leg["to_season_entry_id"]:
                    raise MidseasonDraftStateError(
                        f"player {leg['season_player_id']} is no longer owned by {leg['to_season_entry_id']} -- "
                        "this trade's effect has already moved on and cannot be cleanly reversed"
                    )
            # Release every player leg first, then reacquire every one --
            # matching decide_trade's own ordering. Releasing and
            # reacquiring leg-by-leg instead would refuse a balanced
            # player-for-player swap between two full squads: reversing
            # leg 1 alone would try to reacquire into an entry that (until
            # leg 2 also releases) still holds the *other* incoming
            # player, over capacity.
            for leg in player_legs:
                self.ownership.release_in_transaction(
                    conn,
                    leg["season_player_id"],
                    effective_at=now,
                    actor=actor,
                    reason=reason,
                    correlation_id=correlation,
                    allow_closed_window=True,
                )
                # The current holder may since have formally delisted this
                # same player -- that declaration is about to become
                # meaningless once the player moves back to its original
                # owner, and `lock_delistings` must never release it from
                # whoever now actually owns it based on the stale
                # declaration (the same reasoning `decide_trade` applies
                # when it *approves* a player leg, mirrored here for the
                # reversal).
                stale_delisting = conn.execute(
                    "SELECT * FROM midseason_delisting WHERE midseason_draft_id=? AND season_player_id=? "
                    "AND withdrawn_at IS NULL" + _for_update_suffix(self.database),
                    (draft["midseason_draft_id"], leg["season_player_id"]),
                ).fetchone()
                if stale_delisting:
                    conn.execute(
                        "UPDATE midseason_delisting SET withdrawn_at=? WHERE delisting_id=?",
                        (now, stale_delisting["delisting_id"]),
                    )
                    append_event(
                        conn,
                        actor=actor,
                        action="midseason.delisting.withdrawn",
                        entity_type="midseason.delisting",
                        entity_id=stale_delisting["delisting_id"],
                        correlation_id=correlation,
                        reason="superseded by reversing the trade that moved this player",
                        before_state={"withdrawn_at": None},
                        after_state={"withdrawn_at": now},
                    )
            for leg in player_legs:
                self.ownership.acquire_in_transaction(
                    conn,
                    leg["season_player_id"],
                    leg["from_season_entry_id"],
                    effective_at=now,
                    actor=actor,
                    reason=reason,
                    correlation_id=correlation,
                    allow_closed_window=True,
                )
            conn.execute(
                "UPDATE midseason_trade SET status='rejected', decided_at=?, decision_reason=? WHERE trade_id=?",
                (now, reason, trade_id),
            )
            append_event(
                conn,
                actor=actor,
                action="midseason.trade.reversed",
                entity_type="midseason.trade",
                entity_id=trade_id,
                correlation_id=correlation,
                reason=reason,
                before_state={"status": "approved"},
                after_state={"status": "rejected"},
            )
        return self.get_trade(trade_id)

    def get_trade(self, trade_id):
        row = self.database.execute("SELECT * FROM midseason_trade WHERE trade_id=?", (trade_id,)).fetchone()
        return MidseasonTrade(**dict(row)) if row else None

    def list_trades(self, season_id, *, status=None):
        draft = self.get_draft(season_id)
        if not draft:
            return []
        if status:
            rows = self.database.execute(
                "SELECT * FROM midseason_trade WHERE midseason_draft_id=? AND status=? ORDER BY proposed_at",
                (draft.midseason_draft_id, status),
            ).fetchall()
        else:
            rows = self.database.execute(
                "SELECT * FROM midseason_trade WHERE midseason_draft_id=? ORDER BY proposed_at",
                (draft.midseason_draft_id,),
            ).fetchall()
        return [MidseasonTrade(**dict(row)) for row in rows]

    def trade_legs(self, trade_id):
        rows = self.database.execute(
            "SELECT * FROM midseason_trade_leg WHERE trade_id=? ORDER BY leg_id", (trade_id,)
        ).fetchall()
        return [MidseasonTradeLeg(**dict(row)) for row in rows]

    # -- 11. Lock delistings ----------------------------------------------

    def lock_delistings(self, season_id, *, actor=ActorContext.anonymous_operator("scorer"), reason=None):
        """The decisive boundary: refuses while any trade for this draft is
        still pending, then releases every still-active delisted player
        (they immediately enter the available pool) and marks delistings
        locked. Formal delistings cannot be withdrawn through normal action
        after this point.

        Before committing, re-plans the eventual selection allocation (the
        same computation `generate_selection_table` will materialise) and
        verifies both that every entry's final pick count -- after
        resolving approved round-based pick-trade legs -- exactly matches
        its own roster vacancies, and that every approved pick leg actually
        has a vacancy to redirect at all. An approved pick trade can only
        ever move *who* exercises an existing vacancy slot; it cannot
        manufacture a vacancy that isn't there, and a leg naming a sender
        with no vacancy in that round can never be delivered. Either defect
        raising here rolls back this entire call -- including the
        delisted-player releases just applied -- so nothing is lost: the
        Scorer can rebalance the offending trade(s) -- typically by trading
        the excess pick(s) on to an entry with spare vacancy while the
        delisting window is still open -- or reverse an already-approved
        trade's effect outright with `reverse_trade_approval` (a pending
        proposal can simply be rejected via `decide_trade`, but an approved
        one cannot go back to pending) -- and retry the lock, rather than
        the draft only discovering the problem later when a pick can never
        be legally executed or a trade's promised pick silently never
        arrives."""
        with transaction(self.database) as conn:
            draft = self._locked_draft(conn, season_id)
            if draft["state"] != "delisting_open":
                raise MidseasonDraftStateError("delistings can only be locked from the delisting-open state")
            pending = conn.execute(
                "SELECT trade_id FROM midseason_trade WHERE midseason_draft_id=? AND status='pending'"
                + _for_update_suffix(self.database),
                (draft["midseason_draft_id"],),
            ).fetchall()
            if pending:
                raise MidseasonPendingTradesError(
                    "delistings cannot be locked while trades remain pending",
                    trade_ids=[row["trade_id"] for row in pending],
                )
            config = conn.execute(
                "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?"
                + _for_update_suffix(self.database),
                (season_id,),
            ).fetchone()
            if not config:
                raise MidseasonDraftStateError("season squad limit must be configured before locking delistings")
            squad_limit = config["squad_limit"]
            order_rows = conn.execute(
                "SELECT position, season_entry_id FROM midseason_draft_order "
                "WHERE midseason_draft_id=? ORDER BY position",
                (draft["midseason_draft_id"],),
            ).fetchall()
            ordered_entry_ids = [row["season_entry_id"] for row in order_rows]
            active = conn.execute(
                "SELECT * FROM midseason_delisting WHERE midseason_draft_id=? AND withdrawn_at IS NULL"
                + _for_update_suffix(self.database),
                (draft["midseason_draft_id"],),
            ).fetchall()
            now = _now()
            correlation = new_correlation_id()
            for row in active:
                self.ownership.release_in_transaction(
                    conn,
                    row["season_player_id"],
                    effective_at=now,
                    actor=actor,
                    reason=reason or "mid-season delisting locked",
                    correlation_id=correlation,
                    allow_closed_window=True,
                )
                conn.execute(
                    "UPDATE midseason_delisting SET locked_at=? WHERE delisting_id=?", (now, row["delisting_id"])
                )
            allocations, unapplied_leg_ids, vacancies, overfull = self._plan_selection_allocations(
                conn, draft["midseason_draft_id"], ordered_entry_ids, squad_limit
            )
            allocated_counts: dict[str, int] = {}
            for allocation in allocations:
                allocated_counts[allocation["current"]] = allocated_counts.get(allocation["current"], 0) + 1
            # An entry receiving more selections than its own vacancies
            # would strand the draft (its extra pick can never be legally
            # executed). Its mirror -- an entry left with *fewer*
            # selections than its vacancies, because one of its own picks
            # was traded away without anything replacing it -- is the same
            # imbalance from the other side: it would leave that entry's
            # roster permanently short, silently, with no further
            # opportunity to fill it. Every entry with any vacancy or any
            # allocation must reconcile exactly.
            mismatched = {
                entry_id: {"allocated": allocated_counts.get(entry_id, 0), "vacancies": vacancies.get(entry_id, 0)}
                for entry_id in ordered_entry_ids
                if allocated_counts.get(entry_id, 0) != vacancies.get(entry_id, 0)
            }
            if mismatched or unapplied_leg_ids or overfull:
                messages = []
                if overfull:
                    # An overage tolerated at approval time because an
                    # active delisting covered it can still be withdrawn
                    # any time before lock -- `max(squad_limit - count, 0)`
                    # would then silently clamp this entry's vacancy to 0,
                    # hiding the fact that it is still over its limit
                    # (allocated and vacancies both read 0, so `mismatched`
                    # alone would never catch it). Refuse outright instead
                    # of committing a lock that later makes finalisation
                    # permanently impossible for this entry.
                    messages.append(
                        "one or more entries are still above their configured squad limit and have no active "
                        "delisting left to bring them back down: "
                        + ", ".join(
                            f"{entry_id} has {count} players against a limit of {squad_limit}"
                            for entry_id, count in overfull.items()
                        )
                    )
                if mismatched:
                    messages.append(
                        "one or more approved round-based pick trades leave an entry's final mid-season "
                        "selection count out of step with its own roster vacancies: "
                        + ", ".join(
                            f"{entry_id} would receive {info['allocated']} selection(s) against "
                            f"{info['vacancies']} vacanc{'y' if info['vacancies'] == 1 else 'ies'}"
                            for entry_id, info in mismatched.items()
                        )
                    )
                if unapplied_leg_ids:
                    # The named (round, from_entity) entitlement has no
                    # vacancy in that round to redirect at all -- most
                    # often because the sender's own vacancy was already
                    # absorbed elsewhere (e.g. a player-for-pick trade
                    # whose incoming player and matching delisting exactly
                    # cancelled out its own vacancy). Silently honouring
                    # only the trade's player side while dropping its pick
                    # side is exactly the kind of half-applied trade this
                    # boundary exists to catch.
                    messages.append(
                        "one or more approved round-based pick legs have no vacancy to apply to at all, so the "
                        "traded pick could never actually be delivered: "
                        + ", ".join(str(leg_id) for leg_id in unapplied_leg_ids)
                    )
                raise MidseasonPickReconciliationError(
                    "nothing has been changed by this call (the delisting window is still open): submit another "
                    "delisting to cover the overage, rebalance with a compensating trade, or reverse an approved "
                    "trade outright with reverse_trade_approval, before retrying the lock -- " + "; ".join(messages),
                    mismatched=mismatched,
                    unapplied_leg_ids=unapplied_leg_ids,
                    overfull=overfull,
                )
            conn.execute(
                "UPDATE midseason_draft SET state='delistings_locked', delistings_locked_at=?, updated_at=?, "
                "version=version+1 WHERE midseason_draft_id=?",
                (now, now, draft["midseason_draft_id"]),
            )
            append_event(
                conn,
                actor=actor,
                action="midseason.delistings.locked",
                entity_type="midseason.draft",
                entity_id=draft["midseason_draft_id"],
                correlation_id=correlation,
                reason=reason,
                before_state={"state": "delisting_open"},
                after_state={"state": "delistings_locked", "delisted_player_count": len(active)},
            )
        return self.get_draft(season_id)

    # -- shared by lock_delistings' reconciliation check and
    #    generate_selection_table's materialisation --------------------

    def _plan_selection_allocations(self, conn, midseason_draft_id, ordered_entry_ids, squad_limit):
        """Computes each entry's vacancy count from its live squad size,
        allocates picks in team order (skipping any entry once satisfied),
        then resolves every approved round-based pick-trade leg directly
        against each allocation's *original* vacancy-owner, in exactly one
        hop -- never chained through a leg's *destination*. A leg only ever
        names its `from_season_entry_id`'s own original vacancy-driven
        entitlement (the data model has no way to identify a specifically
        re-traded, already-acquired entitlement as something distinct from
        that entity's own), so a same-round three-(or more)-way rotation
        (A's pick to B, B's own pick to C, C's own pick to A) must resolve
        each leg against its *own* named sender rather than walking B's
        onward leg as if it were forwarding the specific pick it just
        received from A -- that would silently misroute A's pick to C
        instead of B. A single fixed lookup, not a walk, also makes a
        same-round two-way swap correct for free: overwriting one shared
        (round, current_owner) key leg-by-leg (the earlier, broken
        approach) clobbers the very key the other leg needs, but resolving
        every allocation from its own untouched original key never has
        that problem. `overfull` maps any entry whose *live* squad size
        already exceeds `squad_limit` to that live count -- vacancy itself
        is clamped to 0 for such an entry (it needs no further picks), but
        clamping alone would silently hide the overage from a caller that
        only compares allocated picks to vacancies (both land on 0, so
        nothing looks wrong). This can happen even for an acquisition that
        was properly covered by an active delisting at approval time, if
        that delisting is later withdrawn before lock. Returns
        (allocations, unapplied_leg_ids, vacancies, overfull)."""
        vacancies = {}
        overfull = {}
        for entry_id in ordered_entry_ids:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM player_ownership_period WHERE season_entry_id=? AND released_at IS NULL",
                (entry_id,),
            ).fetchone()["n"]
            vacancies[entry_id] = max(squad_limit - count, 0)
            if count > squad_limit:
                overfull[entry_id] = count
        approved_pick_legs = conn.execute(
            "SELECT l.* FROM midseason_trade_leg l JOIN midseason_trade t ON t.trade_id=l.trade_id "
            "WHERE t.midseason_draft_id=? AND t.status='approved' AND l.leg_type='pick' "
            "ORDER BY t.decided_at, l.leg_id",
            (midseason_draft_id,),
        ).fetchall()
        allocations = [
            {
                "overall": overall,
                "round": round_number,
                "position": position,
                "original": original,
                "current": current,
            }
            for overall, round_number, position, original, current in vacancy_allocations(ordered_entry_ids, vacancies)
        ]
        redirect = {
            (leg["draft_round"], leg["from_season_entry_id"]): leg["to_season_entry_id"] for leg in approved_pick_legs
        }
        applied_keys = set()
        for allocation in allocations:
            key = (allocation["round"], allocation["current"])
            if key in redirect:
                allocation["current"] = redirect[key]
                applied_keys.add(key)
        unapplied_leg_ids = [
            leg["leg_id"]
            for leg in approved_pick_legs
            if (leg["draft_round"], leg["from_season_entry_id"]) not in applied_keys
        ]
        return allocations, unapplied_leg_ids, vacancies, overfull

    # -- 12/13/14. Generate the final numbered selection table -----------

    def generate_selection_table(self, season_id, *, actor=ActorContext.anonymous_operator("scorer"), reason=None):
        """Only legal once delistings are locked. Computes each entry's
        vacancy count from its now-live squad size against the season's
        configured squad limit, allocates picks in confirmed team order
        (skipping any entry once satisfied), applies approved round-based
        pick-trade legs to that allocation, and materialises it as the
        mid-season `season_draft`/`draft_pick` rows -- from this point,
        selections are made through `app.draft.DraftRepository`. By this
        point `lock_delistings` has already verified every entry's final
        allocation reconciles with its vacancies, so this call cannot itself
        strand the draft.

        If literally no entry has any vacancy at all (nobody delisted
        anyone this cycle, or every delisting was withdrawn before lock),
        there is nothing to draft -- `app.draft.DraftRepository` itself
        refuses to materialise a zero-pick draft, and `delistings_locked`
        has no route back to `delisting_open` to retry from. Rather than
        stranding the season there, this call recognises that outcome as a
        trivial completion: it transitions straight to `draft_complete`
        without ever creating an underlying engine draft. Every downstream
        read (`status`, `picks`, `next_pick`, `reconcile_completion`) is
        already null-safe for "no engine draft exists for this season/kind"
        for exactly this reason."""
        with transaction(self.database) as conn:
            draft = self._locked_draft(conn, season_id)
            if draft["state"] != "delistings_locked":
                raise MidseasonDraftStateError("the selection table can only be generated once delistings are locked")
            config = conn.execute(
                "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?"
                + _for_update_suffix(self.database),
                (season_id,),
            ).fetchone()
            if not config:
                raise MidseasonDraftStateError("season squad limit must be configured before generating selections")
            squad_limit = config["squad_limit"]
            order_rows = conn.execute(
                "SELECT position, season_entry_id FROM midseason_draft_order "
                "WHERE midseason_draft_id=? ORDER BY position",
                (draft["midseason_draft_id"],),
            ).fetchall()
            ordered_entry_ids = [row["season_entry_id"] for row in order_rows]
            allocations, unapplied_leg_ids, _vacancies, _overfull = self._plan_selection_allocations(
                conn, draft["midseason_draft_id"], ordered_entry_ids, squad_limit
            )
            now = _now()
            if not allocations:
                # Nothing at all to draft: transition straight through to
                # draft_complete rather than materialising a zero-pick
                # engine draft (app.draft.DraftRepository refuses one
                # outright), which would otherwise strand the season in
                # delistings_locked with no route back to delisting_open.
                conn.execute(
                    "UPDATE midseason_draft SET state='draft_complete', draft_completed_at=?, updated_at=?, "
                    "version=version+1 WHERE midseason_draft_id=?",
                    (now, now, draft["midseason_draft_id"]),
                )
                append_event(
                    conn,
                    actor=actor,
                    action="midseason.selections.generated",
                    entity_type="midseason.draft",
                    entity_id=draft["midseason_draft_id"],
                    reason=reason or "no entry has any roster vacancy -- nothing to draft",
                    before_state={"state": "delistings_locked"},
                    after_state={"state": "draft_complete", "pick_count": 0},
                )
            else:
                self.drafts.materialize_draft_in_transaction(
                    conn,
                    season_id,
                    MIDSEASON_DRAFT_KIND,
                    ordered_entry_ids,
                    (
                        (a["overall"], a["round"], a["position"], a["original"], a["current"])
                        for a in sorted(allocations, key=lambda a: a["overall"])
                    ),
                    squad_limit,
                    actor=actor,
                    reason=reason,
                )
                conn.execute(
                    "UPDATE midseason_draft SET state='draft_open', updated_at=?, version=version+1 "
                    "WHERE midseason_draft_id=?",
                    (now, draft["midseason_draft_id"]),
                )
                append_event(
                    conn,
                    actor=actor,
                    action="midseason.selections.generated",
                    entity_type="midseason.draft",
                    entity_id=draft["midseason_draft_id"],
                    reason=reason,
                    before_state={"state": "delistings_locked"},
                    after_state={
                        "state": "draft_open",
                        "pick_count": len(allocations),
                        "unapplied_pick_trade_leg_ids": unapplied_leg_ids,
                    },
                )
        return self.get_draft(season_id)

    # -- 15. Selections, completion ---------------------------------------

    def status(self, season_id):
        return self.drafts.status(season_id, draft_kind=MIDSEASON_DRAFT_KIND)

    def next_pick(self, season_id):
        return self.drafts.next_pick(season_id, draft_kind=MIDSEASON_DRAFT_KIND)

    def picks(self, season_id, *, include_superseded=False):
        return self.drafts.picks(season_id, draft_kind=MIDSEASON_DRAFT_KIND, include_superseded=include_superseded)

    def execute_pick(
        self,
        season_id,
        selecting_entry_id,
        season_player_id,
        *,
        pick_id=None,
        actor,
        reason="mid-season draft selection",
    ):
        pick = self.drafts.execute_pick(
            season_id,
            selecting_entry_id,
            season_player_id,
            pick_id=pick_id,
            draft_kind=MIDSEASON_DRAFT_KIND,
            actor=actor,
            reason=reason,
        )
        self.reconcile_completion(season_id, actor=actor)
        return pick

    def reconcile_completion(self, season_id, *, actor):
        """Idempotent/safely-repeatable: complete automatically once the
        final required selection has been made, without a routine
        additional Scorer lock. Safe to call any number of times, and safe
        to resume after an interruption between finalising the underlying
        draft and updating this module's own lifecycle state.

        Public (not just `execute_pick`'s own internal step) so a caller can
        retry it directly after an interruption between the final pick's
        own commit and this reconciliation -- `execute_pick` cannot itself
        be retried once the final pick is already completed (there is no
        next pick left), so recovery needs its own, separately callable,
        safely-repeatable entry point. Exposed via
        `POST /{season_id}/reconcile-completion` and the replay CLI's
        `reconcile-completion` subcommand."""
        status = self.drafts.status(season_id, draft_kind=MIDSEASON_DRAFT_KIND)
        if status is None:
            return
        if status.is_complete and not status.is_finalized:
            self.drafts.finalize(
                season_id,
                draft_kind=MIDSEASON_DRAFT_KIND,
                actor=actor,
                note="mid-season draft auto-completed on final selection",
            )
            status = self.drafts.status(season_id, draft_kind=MIDSEASON_DRAFT_KIND)
        if status.is_finalized:
            with transaction(self.database) as conn:
                draft = self._locked_draft(conn, season_id)
                if draft["state"] == "draft_open":
                    now = _now()
                    conn.execute(
                        "UPDATE midseason_draft SET state='draft_complete', draft_completed_at=?, updated_at=?, "
                        "version=version+1 WHERE midseason_draft_id=?",
                        (now, now, draft["midseason_draft_id"]),
                    )
                    append_event(
                        conn,
                        actor=actor,
                        action="midseason.draft.completed",
                        entity_type="midseason.draft",
                        entity_id=draft["midseason_draft_id"],
                        before_state={"state": "draft_open"},
                        after_state={"state": "draft_complete"},
                    )

    # -- 16. Exceptional audited corrections -------------------------------

    def correct_selection(self, season_id, draft_pick_id, *, actor, reason):
        """Undo the most recently completed selection -- see
        `app.draft.DraftRepository.correct_pick`. Only legal on the active
        (not yet finalized/completed) mid-season draft; `reopen_draft` first
        if the draft has already completed."""
        return self.drafts.correct_pick(
            season_id, draft_pick_id, draft_kind=MIDSEASON_DRAFT_KIND, actor=actor, reason=reason
        )

    def reopen_draft(self, season_id, *, actor, reason):
        """Exceptional audited correction of a completed mid-season draft
        (e.g. an agreed erroneous selection) -- reopens the underlying
        engine draft and this module's own lifecycle state together.

        Only legal from `draft_complete`: refuses outright (before ever
        touching the engine draft) once the season has moved past it --
        most importantly the terminal `complete` state, reached via
        `close_post_draft_trading`. Reopens the engine draft and updates
        this module's own lifecycle state in one transaction (via
        `DraftRepository.reopen_in_transaction`), so a concurrent
        `close_post_draft_trading` can never race in between the two: either
        both commit together, or (if the lock finds the state has already
        moved on) neither does -- there is no window where the engine is
        unfinalized but the lifecycle still reads `complete`."""
        with transaction(self.database) as conn:
            locked = self._locked_draft(conn, season_id)
            if locked["state"] != "draft_complete":
                raise MidseasonDraftStateError(
                    f"the mid-season draft can only be reopened from draft_complete, not {locked['state']!r}"
                )
            self.drafts.reopen_in_transaction(
                conn, season_id, draft_kind=MIDSEASON_DRAFT_KIND, actor=actor, reason=reason
            )
            now = _now()
            conn.execute(
                "UPDATE midseason_draft SET state='draft_open', updated_at=?, version=version+1 "
                "WHERE midseason_draft_id=?",
                (now, locked["midseason_draft_id"]),
            )
            append_event(
                conn,
                actor=actor,
                action="midseason.draft.reopened",
                entity_type="midseason.draft",
                entity_id=locked["midseason_draft_id"],
                reason=reason,
                before_state={"state": "draft_complete"},
                after_state={"state": "draft_open"},
            )
        return self.get_draft(season_id)

    # -- 17/18. Post-draft trading, transition to Round 11 -----------------

    def close_post_draft_trading(self, season_id, *, actor=ActorContext.anonymous_operator("scorer"), reason=None):
        """The Scorer decides when the post-draft trading period closes --
        no automatic 12/24-hour expiry (matching `app.preseason.
        close_window`'s convention). Round 11's own lockout mechanism is
        untouched by this call; nothing further is required of this module
        for the season to proceed into Round 11 -- squads are already
        reflected in the same `player_ownership_period` ledger every other
        round reads."""
        with transaction(self.database) as conn:
            draft = self._locked_draft(conn, season_id)
            if draft["state"] != "draft_complete":
                raise MidseasonDraftStateError("post-draft trading can only be closed once the draft is complete")
            now = _now()
            conn.execute(
                "UPDATE midseason_draft SET state='complete', completed_at=?, updated_at=?, version=version+1 "
                "WHERE midseason_draft_id=?",
                (now, now, draft["midseason_draft_id"]),
            )
            append_event(
                conn,
                actor=actor,
                action="midseason.complete",
                entity_type="midseason.draft",
                entity_id=draft["midseason_draft_id"],
                reason=reason,
                before_state={"state": "draft_complete"},
                after_state={"state": "complete"},
            )
        return self.get_draft(season_id)

    # -- Available-player pool ----------------------------------------------

    def available_player_pool(self, season_id):
        """The mid-season draft pool: eligible AFL players with no
        currently-open ownership period. Identical to
        `app.player_pool.PlayerPoolRepository.list_available` -- it already
        includes anyone released by a locked delisting or a resolved trade
        (both flow through the same `player_ownership_period` ledger) and
        excludes anyone still held by another BBBFFL squad."""
        return self.player_pool.list_available(season_id)
