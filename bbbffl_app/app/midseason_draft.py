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
        so a multi-leg trade's squad-capacity checks see each entry's net
        position). A *pick* leg is recorded as approved but not applied
        until `generate_selection_table` builds the allocation.

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
                for leg in legs:
                    if leg["leg_type"] != "player":
                        continue
                    self.ownership.acquire_in_transaction(
                        conn,
                        leg["season_player_id"],
                        leg["to_season_entry_id"],
                        effective_at=now,
                        actor=actor,
                        reason=reason or "mid-season trade approved",
                        correlation_id=trade["correlation_id"],
                        allow_closed_window=True,
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
        after this point."""
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

    # -- 12/13/14. Generate the final numbered selection table -----------

    def generate_selection_table(self, season_id, *, actor=ActorContext.anonymous_operator("scorer"), reason=None):
        """Only legal once delistings are locked. Computes each entry's
        vacancy count from its now-live squad size against the season's
        configured squad limit, allocates picks in confirmed team order
        (skipping any entry once satisfied), applies approved round-based
        pick-trade legs to that allocation, and materialises it as the
        mid-season `season_draft`/`draft_pick` rows -- from this point,
        selections are made through `app.draft.DraftRepository`."""
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
            vacancies = {}
            for entry_id in ordered_entry_ids:
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM player_ownership_period WHERE season_entry_id=? AND released_at IS NULL",
                    (entry_id,),
                ).fetchone()["n"]
                vacancies[entry_id] = max(squad_limit - count, 0)
            approved_pick_legs = conn.execute(
                "SELECT l.* FROM midseason_trade_leg l JOIN midseason_trade t ON t.trade_id=l.trade_id "
                "WHERE t.midseason_draft_id=? AND t.status='approved' AND l.leg_type='pick' "
                "ORDER BY t.decided_at, l.leg_id",
                (draft["midseason_draft_id"],),
            ).fetchall()
            allocations = [
                {
                    "overall": overall,
                    "round": round_number,
                    "position": position,
                    "original": original,
                    "current": current,
                }
                for overall, round_number, position, original, current in vacancy_allocations(
                    ordered_entry_ids, vacancies
                )
            ]
            if not allocations:
                raise MidseasonDraftStateError("no team requires a mid-season draft selection")
            # Resolve every approved pick-trade leg against each
            # allocation's *original* vacancy-owner, not a shared
            # (round, current_owner) lookup mutated leg-by-leg: overwriting
            # that lookup as legs are applied silently drops one side of a
            # same-round two-way swap (leg 1's write clobbers the very key
            # leg 2 needs to find leg 2's own original allocation). Walking
            # the redirect chain from each allocation's original owner
            # instead handles a swap (a two-node cycle -- stop once a
            # revisit is detected, leaving the chain's last resolved owner)
            # and a longer trade chain (A's pick traded to B, then that same
            # pick traded on by B to C) identically and correctly.
            redirect = {
                (leg["draft_round"], leg["from_season_entry_id"]): leg["to_season_entry_id"]
                for leg in approved_pick_legs
            }
            visited = set()

            def _resolve(round_number, owner):
                seen = {owner}
                current = owner
                visited.add((round_number, current))
                while (round_number, current) in redirect:
                    following = redirect[(round_number, current)]
                    if following in seen:
                        break
                    seen.add(following)
                    current = following
                    visited.add((round_number, current))
                return current

            for allocation in allocations:
                allocation["current"] = _resolve(allocation["round"], allocation["current"])
            unapplied_leg_ids = [
                leg["leg_id"]
                for leg in approved_pick_legs
                if (leg["draft_round"], leg["from_season_entry_id"]) not in visited
            ]
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
            now = _now()
            conn.execute(
                "UPDATE midseason_draft SET state='draft_open', updated_at=?, version=version+1 WHERE midseason_draft_id=?",
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
