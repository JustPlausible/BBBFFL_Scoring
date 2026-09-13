"""Issue #195: persisted, versioned premiership/wooden-spoon award records.

Read `docs/2026-finals-superscore-design.md`'s "Grand Final/season winner
recording and end-of-season completion" section before changing anything
here. Two things it establishes as settled design, not open questions:

- The premiership and wooden-spoon facts are **persisted, versioned
  records** (`season_award`, migration `0033_season_award.py`), each
  referencing the *effective* source version it was derived from -- the
  effective Grand Final official result for the premiership, and the
  *live* mathematical Round 20 ladder for the wooden spoon. The wooden
  spoon is explicitly "an H&A fact, not derived from the finals bracket or
  historical finals-seeding order" -- it is derived here from a fresh
  `app.ladder.LadderRepository.snapshot` read, never from `finals_bracket`'s
  own frozen seed/provenance (which issue #190 froze once, at bracket
  creation, specifically so it is *not* re-read for anything past Week 1
  pairing derivation).
- A later correction to either source does not by itself rewrite the
  existing record. Instead, `reconcile_premiership`/`reconcile_wooden_spoon`
  are the explicit, audited, idempotent re-recording path issue #195
  requires: read the currently effective source, derive the award, and
  create-or-supersede the record so it references the newly effective
  version -- a no-op if the active record already matches.

## Reuse, not duplication

Issue #191 (`app/finals_review.py`) already appends bare `finals.premier.
recorded`/`finals.wooden_spoon.recorded` **audit events** whenever the
Grand Final publishes or its result is corrected, and already implements
the two award-determination functions this module needs: `_premier_for_
scores` (the confirmed tie-break rule: higher score wins, a tie is won by
the higher frozen finals seed) and `_mathematical_wooden_spoon`. This
module imports and reuses `_premier_for_scores` directly for the
premiership. It does *not* reuse `_mathematical_wooden_spoon`, because that
function deliberately reads `finals_bracket`'s own *frozen* seed/ladder
provenance (correct for its own purpose -- captioning the bracket's Week 1
field) -- exactly the source the design doc says the wooden-spoon record
must *not* depend on. This module adds no duplicate premiership-determination
logic; it adds the layer #191 does not: a persisted, versioned record, and
an idempotent re-recording command.

## Concurrency

Every public function here first calls `SeasonRepository.guard_writable`
-- locking the season row -- as the first statement of its own write
transaction, before reading anything else. Because every other
result-changing write path in the application (ordinary/finals/SuperScore
correction and publication) is wired to take that same lock first too
(issue #195's shared write fence), holding it here for the rest of this
transaction guarantees no concurrent write can be mutating the Grand Final
result or a Round 20 result while this function reads them: such a writer
would block acquiring the season lock before it could touch either. This is
also why no separate `SELECT ... FOR UPDATE`-and-recheck is needed on the
individual Grand Final/Round-20 matchup rows here (contrast bracket
creation's own `_resolve_seed`, which must re-verify under lock because it
reads *before* any season-wide serialization point exists) -- the season
lock alone already serializes this read against every writer that matters.
"""

import json
from dataclasses import dataclass
from uuid import uuid4

from app.audit import ActorContext, append_event
from app.db import _for_update_suffix, transaction
from app.finals_review import _premier_for_scores
from app.ladder import LadderRepository
from app.season import SeasonRepository, _now

PREMIERSHIP_RECORDED = "season.premiership.recorded"
WOODEN_SPOON_RECORDED = "season.wooden_spoon.recorded"
ENTITY_TYPE_SEASON = "season"

PREMIERSHIP = "premiership"
WOODEN_SPOON = "wooden_spoon"
AWARD_TYPES = (PREMIERSHIP, WOODEN_SPOON)


class SeasonAwardError(RuntimeError):
    """Base class for this module's domain errors."""


class AwardNotReadyError(SeasonAwardError):
    """The Grand Final result, or the ordinary competition's Round 20
    ladder, is not yet in a state this award can be derived from (no
    finals bracket/Grand Final pairing yet, no published Grand Final
    result yet, or no ordinary results at all yet)."""


class UnresolvedWoodenSpoonTieError(SeasonAwardError):
    """The live mathematical Round 20 ladder has an unresolved tie for
    last place -- exactly `app.finals_seeding.UnresolvedLadderTieError`'s
    reasoning applied to the bottom of the ladder instead of the top: this
    requires an explicit, audited competition-governance determination,
    never an arbitrary tie-break or the ladder's own serialization order."""


@dataclass(frozen=True)
class SeasonAward:
    award_id: str
    season_id: str
    award_type: str
    season_entry_id: str
    runner_up_season_entry_id: str | None
    provenance: dict
    status: str
    superseded_by_award_id: str | None
    created_at: str
    created_by: str | None
    reason: str | None


def _row_to_award(row) -> SeasonAward:
    values = dict(row)
    values["provenance"] = json.loads(values["provenance"])
    return SeasonAward(**values)


def _encode(provenance: dict) -> str:
    return json.dumps(provenance, sort_keys=True, default=str)


class SeasonAwardRepository:
    """Read/query boundary plus the shared create-or-supersede primitive
    both award types share. `reconcile_premiership`/`reconcile_wooden_spoon`
    (below) are the only callers of `_record_or_supersede` -- there is
    deliberately no public "just overwrite it" method, so every write to
    this table goes through one of this module's two audited, idempotent
    commands."""

    def __init__(self, database):
        self.database = database

    def get_active(self, season_id: str, award_type: str) -> SeasonAward | None:
        row = self.database.execute(
            "SELECT * FROM season_award WHERE season_id=? AND award_type=? AND status='active'",
            (season_id, award_type),
        ).fetchone()
        return _row_to_award(row) if row else None

    def history(self, season_id: str, award_type: str) -> list[SeasonAward]:
        rows = self.database.execute(
            "SELECT * FROM season_award WHERE season_id=? AND award_type=? ORDER BY created_at",
            (season_id, award_type),
        ).fetchall()
        return [_row_to_award(row) for row in rows]

    def _record_or_supersede(
        self,
        conn,
        season_id: str,
        award_type: str,
        season_entry_id: str,
        provenance: dict,
        *,
        actor: ActorContext,
        reason: str | None,
        runner_up_season_entry_id: str | None = None,
    ) -> tuple[SeasonAward, bool]:
        """Idempotent: a no-op (returns the existing record, `created=False`)
        if the currently active record already names the identical entry
        (and runner-up) derived from the identical provenance. Otherwise
        inserts a new `status='active'` row and flips the previous one (if
        any) to `status='superseded'`, atomically, inside the caller's own
        transaction -- never a bare `UPDATE` of the existing row's
        award-describing fields."""
        existing = conn.execute(
            "SELECT * FROM season_award WHERE season_id=? AND award_type=? AND status='active'"
            + _for_update_suffix(self.database),
            (season_id, award_type),
        ).fetchone()
        encoded_provenance = _encode(provenance)
        if (
            existing is not None
            and existing["season_entry_id"] == season_entry_id
            and existing["runner_up_season_entry_id"] == runner_up_season_entry_id
            and existing["provenance"] == encoded_provenance
        ):
            return _row_to_award(existing), False

        award_id = str(uuid4())
        now = _now()
        # Three steps, not two -- mirroring `app.finals.FinalsBracketRepository.
        # rewind_bracket`'s identical reasoning: the partial unique index
        # (`uq_season_award_active_type`) allows only one *active* row per
        # `(season_id, award_type)` at a time, so the old row must be flipped
        # to `superseded` (pointer left NULL) *before* the new row can be
        # inserted as `active` -- inserting first would transiently leave two
        # active rows and violate that index. Only once the new row exists
        # can the old row's `superseded_by_award_id` pointer be completed.
        if existing is not None:
            conn.execute(
                "UPDATE season_award SET status='superseded' WHERE award_id=?",
                (existing["award_id"],),
            )
        conn.execute(
            "INSERT INTO season_award VALUES (?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, ?)",
            (
                award_id,
                season_id,
                award_type,
                season_entry_id,
                runner_up_season_entry_id,
                encoded_provenance,
                now,
                actor.actor_id,
                reason,
            ),
        )
        if existing is not None:
            conn.execute(
                "UPDATE season_award SET superseded_by_award_id=? WHERE award_id=?",
                (award_id, existing["award_id"]),
            )
        append_event(
            conn,
            actor=actor,
            action=PREMIERSHIP_RECORDED if award_type == PREMIERSHIP else WOODEN_SPOON_RECORDED,
            entity_type=ENTITY_TYPE_SEASON,
            entity_id=season_id,
            entity_version=award_id,
            reason=reason,
            before_state=(
                {"season_entry_id": existing["season_entry_id"], "award_id": existing["award_id"]}
                if existing is not None
                else None
            ),
            after_state={"season_entry_id": season_entry_id, "award_id": award_id},
            payload={
                "award_type": award_type,
                "provenance": provenance,
                "supersedes_award_id": existing["award_id"] if existing is not None else None,
            },
        )
        row = conn.execute("SELECT * FROM season_award WHERE award_id=?", (award_id,)).fetchone()
        return _row_to_award(row), True


def _resolve_effective_grand_final(conn, database, season_id: str):
    """Read the effective Grand Final result and this bracket's frozen
    tie-break seed order -- the *only* thing read from `finals_bracket` for
    the premiership; never its wooden-spoon-adjacent provenance."""
    bracket = conn.execute(
        "SELECT bracket_id FROM finals_bracket WHERE season_id=?" + _for_update_suffix(database), (season_id,)
    ).fetchone()
    if bracket is None:
        raise AwardNotReadyError(f"season {season_id} has no finals bracket yet; cannot derive a premiership")
    pairing = conn.execute(
        "SELECT * FROM finals_bracket_pairing WHERE bracket_id=? AND week_number=4 AND slot='grand_final' "
        "AND status='active'" + _for_update_suffix(database),
        (bracket["bracket_id"],),
    ).fetchone()
    if pairing is None or pairing["matchup_id"] is None:
        raise AwardNotReadyError(f"season {season_id}'s Grand Final has not been played yet")
    matchup = conn.execute(
        "SELECT * FROM bbbffl_matchup WHERE matchup_id=?" + _for_update_suffix(database),
        (pairing["matchup_id"],),
    ).fetchone()
    if matchup["effective_official_version"] is None:
        raise AwardNotReadyError(f"season {season_id}'s Grand Final has no published official result yet")
    result = conn.execute(
        "SELECT home_score, away_score FROM bbbffl_official_result WHERE matchup_id=? AND version=?",
        (matchup["matchup_id"], matchup["effective_official_version"]),
    ).fetchone()
    premier = _premier_for_scores(
        conn,
        bracket["bracket_id"],
        matchup["home_season_entry_id"],
        matchup["away_season_entry_id"],
        result["home_score"],
        result["away_score"],
    )
    runner_up = (
        matchup["away_season_entry_id"]
        if premier == matchup["home_season_entry_id"]
        else matchup["home_season_entry_id"]
    )
    provenance = {
        "bracket_id": bracket["bracket_id"],
        "grand_final_matchup_id": matchup["matchup_id"],
        "official_version": matchup["effective_official_version"],
    }
    return premier, runner_up, provenance


def reconcile_premiership(database, season_id: str, *, actor: ActorContext, reason: str) -> tuple[SeasonAward, bool]:
    """The explicit, audited re-recording path issue #195 requires for the
    premiership: reads the currently effective Grand Final result, derives
    the premier/runner-up, and idempotently creates or supersedes
    `season_award(award_type='premiership')` so it references the current
    effective version. Safe to call repeatedly against unchanged effective
    provenance (returns `created=False`); safe to call again after a Grand
    Final correction (supersedes the prior record)."""
    if not reason or not reason.strip():
        raise SeasonAwardError("premiership re-recording requires an explicit, substantive reason")
    with transaction(database) as conn:
        SeasonRepository(database).guard_writable(conn, season_id)
        return reconcile_premiership_in_transaction(conn, database, season_id, actor=actor, reason=reason)


def reconcile_premiership_in_transaction(conn, database, season_id: str, *, actor: ActorContext, reason: str):
    """Apply premiership reconciliation using a caller-owned transaction --
    the narrow seam `app.season_completion.complete_season` uses for its
    own step 3, mirroring `app.finals.FinalsBracketRepository.
    rewind_bracket_in_transaction`'s identical shape. The caller is
    responsible for having already called `guard_writable` on `conn`."""
    premier, runner_up, provenance = _resolve_effective_grand_final(conn, database, season_id)
    return SeasonAwardRepository(database)._record_or_supersede(
        conn,
        season_id,
        PREMIERSHIP,
        premier,
        provenance,
        actor=actor,
        reason=reason,
        runner_up_season_entry_id=runner_up,
    )


def _resolve_ordinary_competition_id(conn, database, season_id: str) -> str:
    rows = conn.execute(
        "SELECT competition_id FROM competition_stream WHERE season_id=? AND stream_type='ordinary'"
        + _for_update_suffix(database),
        (season_id,),
    ).fetchall()
    if len(rows) != 1:
        raise AwardNotReadyError(
            f"season {season_id} has {len(rows)} ordinary competition streams, expected exactly one; "
            "cannot derive a wooden spoon"
        )
    return rows[0]["competition_id"]


def _resolve_effective_wooden_spoon(conn, database, season_id: str):
    """Read the *live* mathematical Round 20 ladder -- deliberately never
    `finals_bracket`'s frozen seed/provenance (see module docstring)."""
    ordinary_competition_id = _resolve_ordinary_competition_id(conn, database, season_id)
    season_row = conn.execute(
        "SELECT * FROM bbbffl_season WHERE season_id=?" + _for_update_suffix(database), (season_id,)
    ).fetchone()
    round_count = season_row["regular_season_round_count"] if "regular_season_round_count" in season_row.keys() else 20
    ladder = LadderRepository(database).snapshot(ordinary_competition_id, round_count)
    if not ladder.rows:
        raise AwardNotReadyError(f"season {season_id}'s ordinary competition has no final results yet")
    last_place = ladder.rows[-1]
    if last_place.tied:
        raise UnresolvedWoodenSpoonTieError(
            f"cannot derive a deterministic wooden spoon for season {season_id}: the mathematical ladder has an "
            f"unresolved tie for last place among {last_place.tie_group} -- this requires an explicit, audited "
            "competition-governance determination"
        )
    provenance = {
        "ordinary_competition_id": ordinary_competition_id,
        "through_round": ladder.through_round,
        "latest_included_round": ladder.latest_included_round,
        "result_references": [
            {"matchup_id": reference.matchup_id, "official_version": reference.official_version}
            for reference in sorted(ladder.result_references, key=lambda reference: reference.matchup_id)
        ],
    }
    return last_place.season_entry_id, provenance


def reconcile_wooden_spoon(database, season_id: str, *, actor: ActorContext, reason: str) -> tuple[SeasonAward, bool]:
    """The explicit, audited re-recording path issue #195 requires for the
    wooden spoon: reads the currently effective (live) mathematical Round
    20 ladder, derives the last-placed entry, and idempotently creates or
    supersedes `season_award(award_type='wooden_spoon')` so it references
    the current effective result references. Safe to call repeatedly
    against unchanged effective provenance (returns `created=False`); safe
    to call again after a Round 20 result correction (supersedes the prior
    record)."""
    if not reason or not reason.strip():
        raise SeasonAwardError("wooden-spoon re-recording requires an explicit, substantive reason")
    with transaction(database) as conn:
        SeasonRepository(database).guard_writable(conn, season_id)
        return reconcile_wooden_spoon_in_transaction(conn, database, season_id, actor=actor, reason=reason)


def reconcile_wooden_spoon_in_transaction(conn, database, season_id: str, *, actor: ActorContext, reason: str):
    """Apply wooden-spoon reconciliation using a caller-owned transaction --
    see `reconcile_premiership_in_transaction`'s identical rationale."""
    season_entry_id, provenance = _resolve_effective_wooden_spoon(conn, database, season_id)
    return SeasonAwardRepository(database)._record_or_supersede(
        conn, season_id, WOODEN_SPOON, season_entry_id, provenance, actor=actor, reason=reason
    )
