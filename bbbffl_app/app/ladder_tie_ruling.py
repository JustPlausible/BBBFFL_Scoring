"""Issue #241: an audited, persisted manual resolution for an exact
mathematical ladder tie -- the "authorised manual ruling over an otherwise
mathematically unresolved tie" every consumer of `app.ladder` needs a single,
shared place to record and reuse.

## Why this exists

`app.ladder.calculate_ladder` has exactly three sporting criteria
(competition points, percentage, points for) and deliberately never invents
a fourth. When they leave two or more entries exactly tied,
`LadderRow.tied`/`LadderRow.tie_group` report it and `season_entry_id`
ordering inside that group exists only for repeatable *serialization* --
`app.finals_seeding.UnresolvedLadderTieError` (consumed by
`app.finals._resolve_ladder_seed`) and
`app.season_awards.UnresolvedWoodenSpoonTieError` both already fail closed
on this rather than invent an ordering, but until this module existed there
was no way to *resolve* the block: an exact tie anywhere on the ladder
permanently blocked Finals seeding, and an exact tie for last place
permanently blocked the wooden spoon and season completion.

This module is that resolution path, and nothing else: a Scorer/Administrator
records the decided relative order for one exact tie group, with a mandatory
reason, and the ruling is durably persisted and reused by every consumer
that needs a deterministic order over that same tie -- never a second
mathematical tiebreak, never a per-consumer override.

## Never a general-purpose ladder editor

`record_ruling` accepts no free-form `tie_group` -- the tie group is
whatever `decided_order`'s member set names, and it is only ever accepted if
the ladder this function recomputes *right now*, under the season's write
lock, actually reports a tied group with exactly that member set. There is
no code path here that can record a ruling for entries that are not, at the
moment of recording, genuinely and exactly tied; nothing in this module ever
writes to `bbbffl_official_result`, `bbbffl_matchup`, or any other table
`app.ladder` derives its own numbers from.

## Scope and reuse ("the same exact tie")

A ruling is scoped to `(season_id, competition_id, through_round)` -- the
exact same three values that determine one `app.ladder.LadderSnapshot` --
plus the tie group itself (`tie_group_key`, a canonical sorted join of
`season_entry_id`). `app.finals`'s ladder-seed path and
`app.season_awards`'s wooden-spoon path both resolve their tie group(s)
against this identical scope (both use the season's own
`regular_season_round_count` as `through_round` and the season's one
`ordinary` competition), so one recorded ruling is transparently reused by
both -- see `resolve_tie`.

## Staleness

`result_references` freezes the exact `(matchup_id, official_version)` set
`LadderSnapshot.result_references` reported when the ruling was recorded --
the same "freeze the exact result-version set, compare it verbatim" pattern
`app.finals`/`app.finals_seeding` already use for their own staleness
detection. `resolve_tie` compares this set, unordered, against the current
ladder's own `result_references`; any difference (a correction anywhere in
the season, whether or not it happens to change this exact tie group's
membership) makes the ruling stale, and `resolve_tie` reports it as
unresolved rather than silently applying a decision made against inputs
that no longer hold. A stale ruling is never deleted -- it stays queryable
history, exactly like a superseded one.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from app.audit import ActorContext, append_event
from app.db import _for_update_suffix, transaction
from app.ladder import LadderRepository, LadderSnapshot, ResultReference
from app.season import SeasonRepository

LADDER_TIE_RULING_RECORDED = "ladder_tie_ruling.recorded"
ENTITY_TYPE_LADDER_TIE_RULING = "ladder.tie_ruling"


class LadderTieRulingError(ValueError):
    """Base class for every refusal this module raises. No mutation is ever
    attempted once one of these is raised."""


class LadderTieRulingConflictError(LadderTieRulingError):
    """The requested `decided_order`'s member set does not name a group that
    is, right now, exactly tied on this ladder -- either it was never tied,
    the tie has already changed shape (a different set is now tied), or a
    prior ruling already resolved it and nothing has changed since. Recording
    a ruling here is refused; nothing is written."""


@dataclass(frozen=True)
class LadderTieRuling:
    ruling_id: str
    season_id: str
    competition_id: str
    through_round: int
    tie_group: tuple[str, ...]
    decided_order: tuple[str, ...]
    result_references: tuple[ResultReference, ...]
    status: str
    superseded_by_ruling_id: str | None
    created_at: str
    created_by: str | None
    reason: str


class UnresolvedTieError(LadderTieRulingError):
    """No ruling resolves this exact tie group right now -- either none was
    ever recorded (`stale=False`), or one was recorded but the ladder's
    underlying results have changed since (`stale=True`, `ruling` names the
    now-stale record for diagnostic/audit purposes). Callers in
    `app.finals_seeding`/`app.season_awards` catch this and re-raise their
    own existing, narrower exception types so this module's introduction
    never changes either module's public exception contract."""

    def __init__(self, message: str, *, tie_group: tuple[str, ...], stale: bool, ruling: LadderTieRuling | None):
        super().__init__(message)
        self.tie_group = tie_group
        self.stale = stale
        self.ruling = ruling


def _id() -> str:
    return str(uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tie_group_key(entry_ids) -> str:
    return ",".join(sorted(entry_ids))


def _encode_references(references) -> str:
    payload = [{"matchup_id": r.matchup_id, "official_version": r.official_version} for r in references]
    payload.sort(key=lambda item: item["matchup_id"])
    return json.dumps(payload, sort_keys=True)


def _decode_references(encoded: str) -> tuple[ResultReference, ...]:
    return tuple(ResultReference(item["matchup_id"], item["official_version"]) for item in json.loads(encoded))


def _row_to_ruling(row) -> LadderTieRuling:
    return LadderTieRuling(
        ruling_id=row["ruling_id"],
        season_id=row["season_id"],
        competition_id=row["competition_id"],
        through_round=row["through_round"],
        tie_group=tuple(json.loads(row["tie_group"])),
        decided_order=tuple(json.loads(row["decided_order"])),
        result_references=_decode_references(row["result_references"]),
        status=row["status"],
        superseded_by_ruling_id=row["superseded_by_ruling_id"],
        created_at=row["created_at"],
        created_by=row["created_by"],
        reason=row["reason"],
    )


def _find_tied_group(ladder: LadderSnapshot, member_set: frozenset[str]) -> tuple[str, ...] | None:
    for row in ladder.rows:
        if row.tied and frozenset(row.tie_group) == member_set:
            return row.tie_group
    return None


class LadderTieRulingRepository:
    def __init__(self, database):
        self.database = database

    # -- Reads --------------------------------------------------------------

    def get_active(self, season_id: str, competition_id: str, through_round: int, tie_group) -> LadderTieRuling | None:
        row = self.database.execute(
            "SELECT * FROM ladder_tie_ruling WHERE season_id=? AND competition_id=? AND through_round=? "
            "AND tie_group_key=? AND status='active'",
            (season_id, competition_id, through_round, _tie_group_key(tie_group)),
        ).fetchone()
        return _row_to_ruling(row) if row else None

    def history(self, season_id: str, competition_id: str, through_round: int, tie_group) -> list[LadderTieRuling]:
        rows = self.database.execute(
            "SELECT * FROM ladder_tie_ruling WHERE season_id=? AND competition_id=? AND through_round=? "
            "AND tie_group_key=? ORDER BY created_at",
            (season_id, competition_id, through_round, _tie_group_key(tie_group)),
        ).fetchall()
        return [_row_to_ruling(row) for row in rows]

    def list_active_for_season(self, season_id: str) -> list[LadderTieRuling]:
        """Every currently active ruling for this season, across every
        `(competition_id, through_round, tie_group)` scope -- the Scorer
        Operations read model's primary source, alongside a fresh ladder
        read used to report which are stale."""
        rows = self.database.execute(
            "SELECT * FROM ladder_tie_ruling WHERE season_id=? AND status='active' ORDER BY created_at",
            (season_id,),
        ).fetchall()
        return [_row_to_ruling(row) for row in rows]

    # -- Writes ---------------------------------------------------------------

    def record_ruling(
        self,
        season_id: str,
        competition_id: str,
        through_round: int,
        decided_order,
        *,
        actor: ActorContext,
        reason: str,
    ) -> tuple[LadderTieRuling, bool]:
        """Record (or idempotently confirm, or supersede) the ruling for the
        exact tie group `decided_order`'s members name. Requires an
        explicit, substantive `reason` (issue #241 requirement 5) and that
        this competition/round's ladder, recomputed right now under the
        season's write lock, reports a tied group whose member set exactly
        equals `set(decided_order)` -- otherwise `LadderTieRulingConflictError`
        is raised and nothing is written.

        Idempotent: recording the identical `decided_order` again while the
        active ruling's frozen `result_references` still match the current
        ladder is a no-op (`created=False`). A materially different
        `decided_order`, or the same order re-recorded after the ladder's
        inputs changed (the prior ruling would otherwise report `stale`),
        supersedes the existing active ruling rather than overwriting it --
        the full history remains queryable via `history`.
        """
        if not reason or not reason.strip():
            raise LadderTieRulingError("recording a ladder tie ruling requires an explicit, substantive reason")
        order = tuple(decided_order)
        if len(order) < 2:
            raise LadderTieRulingError("a tie ruling requires at least two entries to order")
        if len(set(order)) != len(order):
            raise LadderTieRulingError("decided_order must not repeat a season_entry_id")

        with transaction(self.database) as conn:
            SeasonRepository(self.database).guard_writable(conn, season_id)
            ladder = LadderRepository(self.database).snapshot(competition_id, through_round)
            if ladder.season_id != season_id:
                raise LadderTieRulingConflictError(
                    f"competition_id {competition_id!r} belongs to season {ladder.season_id!r}, not the "
                    f"requested season {season_id!r}"
                )
            tie_group = _find_tied_group(ladder, frozenset(order))
            if tie_group is None:
                raise LadderTieRulingConflictError(
                    f"no exact tie among {sorted(order)} exists on the current ladder for competition "
                    f"{competition_id!r} through round {through_round}; refusing to record a ruling for a "
                    "tie that does not (or no longer) exist"
                )
            tie_group_key = _tie_group_key(tie_group)
            encoded_references = _encode_references(ladder.result_references)

            existing = conn.execute(
                "SELECT * FROM ladder_tie_ruling WHERE season_id=? AND competition_id=? AND through_round=? "
                "AND tie_group_key=? AND status='active'" + _for_update_suffix(self.database),
                (season_id, competition_id, through_round, tie_group_key),
            ).fetchone()
            if (
                existing is not None
                and tuple(json.loads(existing["decided_order"])) == order
                and existing["result_references"] == encoded_references
            ):
                return _row_to_ruling(existing), False

            ruling_id = _id()
            now = _now()
            # Mirrors app.season_awards.SeasonAwardRepository._record_or_supersede's
            # three-step shape: the partial unique index only allows one
            # active row per (season_id, competition_id, through_round,
            # tie_group_key), so the old row is superseded before the new
            # one is inserted active.
            if existing is not None:
                conn.execute(
                    "UPDATE ladder_tie_ruling SET status='superseded' WHERE ruling_id=?", (existing["ruling_id"],)
                )
            conn.execute(
                "INSERT INTO ladder_tie_ruling VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?, ?)",
                (
                    ruling_id,
                    season_id,
                    competition_id,
                    through_round,
                    tie_group_key,
                    json.dumps(sorted(tie_group)),
                    json.dumps(list(order)),
                    encoded_references,
                    now,
                    actor.actor_id,
                    reason,
                ),
            )
            if existing is not None:
                conn.execute(
                    "UPDATE ladder_tie_ruling SET superseded_by_ruling_id=? WHERE ruling_id=?",
                    (ruling_id, existing["ruling_id"]),
                )
            append_event(
                conn,
                actor=actor,
                action=LADDER_TIE_RULING_RECORDED,
                entity_type=ENTITY_TYPE_LADDER_TIE_RULING,
                entity_id=ruling_id,
                reason=reason,
                before_state=(
                    {"decided_order": json.loads(existing["decided_order"]), "ruling_id": existing["ruling_id"]}
                    if existing is not None
                    else None
                ),
                after_state={"tie_group": sorted(tie_group), "decided_order": list(order)},
                payload={
                    "season_id": season_id,
                    "competition_id": competition_id,
                    "through_round": through_round,
                    "supersedes_ruling_id": existing["ruling_id"] if existing is not None else None,
                },
            )
            row = conn.execute("SELECT * FROM ladder_tie_ruling WHERE ruling_id=?", (ruling_id,)).fetchone()
            return _row_to_ruling(row), True


def record_ruling_for_season(
    database, season_id: str, decided_order, *, actor: ActorContext, reason: str
) -> tuple[LadderTieRuling, bool]:
    """`LadderTieRulingRepository.record_ruling`, scoped by season alone --
    the Scorer Operations route's one write entry point. Resolves
    `(competition_id, through_round)` via `resolve_ordinary_context` so the
    caller never needs to know either."""
    competition_id, through_round = resolve_ordinary_context(database, season_id)
    return LadderTieRulingRepository(database).record_ruling(
        season_id, competition_id, through_round, decided_order, actor=actor, reason=reason
    )


def resolve_tie(database, ladder: LadderSnapshot, tie_group) -> tuple[str, ...]:
    """The one integration seam `app.finals`/`app.finals_seeding` and
    `app.season_awards` should call once they have found a tied
    `LadderRow.tie_group` on an already-computed `ladder` (a plain
    `app.ladder.LadderRepository.snapshot(...)` read -- never re-derived
    here). Returns the decided best-to-worst order for `tie_group`'s members
    if an active ruling exists for `(ladder.season_id, ladder.competition_id,
    ladder.through_round, tie_group)` and its frozen `result_references`
    still match `ladder.result_references` exactly; raises
    `UnresolvedTieError` otherwise (see that class's docstring for the
    `stale` distinction it carries for a caller that wants to report it)."""
    tie_group = tuple(tie_group)
    ruling = LadderTieRulingRepository(database).get_active(
        ladder.season_id, ladder.competition_id, ladder.through_round, tie_group
    )
    if ruling is None:
        raise UnresolvedTieError(
            f"no recorded tie ruling resolves the exact tie among {sorted(tie_group)} for competition "
            f"{ladder.competition_id!r} through round {ladder.through_round} -- this requires an explicit, "
            "audited Scorer/competition-governance decision",
            tie_group=tie_group,
            stale=False,
            ruling=None,
        )
    current_references = frozenset((r.matchup_id, r.official_version) for r in ladder.result_references)
    ruling_references = frozenset((r.matchup_id, r.official_version) for r in ruling.result_references)
    if current_references != ruling_references:
        raise UnresolvedTieError(
            f"a tie ruling for {sorted(tie_group)} exists but the underlying ladder results have changed since "
            "it was recorded; it no longer applies and a fresh ruling is required",
            tie_group=tie_group,
            stale=True,
            ruling=ruling,
        )
    return ruling.decided_order


def resolve_ordinary_context(database, season_id: str) -> tuple[str, int]:
    """Resolve the one `(competition_id, through_round)` pair this season's
    ladder-tie governance always operates against -- the same resolution
    `app.finals._resolve_ladder_seed` and `app.season_awards.
    _resolve_effective_wooden_spoon` each perform independently for their
    own ordinary competition/round-count read. Centralised here so the
    Scorer Operations preview/record surface uses the identical scope
    either of those two consumers would."""
    season = database.execute("SELECT * FROM bbbffl_season WHERE season_id=?", (season_id,)).fetchone()
    if season is None:
        raise KeyError(season_id)
    rows = database.execute(
        "SELECT competition_id FROM competition_stream WHERE season_id=? AND stream_type='ordinary'", (season_id,)
    ).fetchall()
    if len(rows) != 1:
        raise LadderTieRulingError(
            f"season {season_id} has {len(rows)} ordinary competition streams, expected exactly one"
        )
    round_count = season["regular_season_round_count"] if "regular_season_round_count" in season.keys() else 20
    return rows[0]["competition_id"], round_count


@dataclass(frozen=True)
class OpenTie:
    """One Scorer Operations report row: one exact tie group on the current
    ladder, whatever it blocks and whatever resolution state it is in."""

    tie_group: tuple[str, ...]
    rank: int
    competition_points: int
    percentage: str
    points_for: str
    affects_finals_seeding: bool
    affects_wooden_spoon: bool
    status: str  # "unresolved" | "stale" | "resolved"
    ruling: LadderTieRuling | None


def preview(database, season_id: str) -> dict:
    """Read-only Scorer Operations report: never mutates, never takes a row
    lock. Every exact tie group on this season's live ordinary-competition
    ladder, whether it blocks Finals seeding (any tie, since a full
    deterministic order needs every rank resolved) and/or the Wooden Spoon
    (only the last-place group), and whether a ruling already resolves it
    (and if so, whether that ruling is fresh or stale)."""
    competition_id, through_round = resolve_ordinary_context(database, season_id)
    ladder = LadderRepository(database).snapshot(competition_id, through_round)
    repository = LadderTieRulingRepository(database)
    last_place_group = ladder.rows[-1].tie_group if ladder.rows and ladder.rows[-1].tied else None
    open_ties: list[OpenTie] = []
    handled: set[tuple[str, ...]] = set()
    for row in ladder.rows:
        if not row.tied or row.tie_group in handled:
            continue
        handled.add(row.tie_group)
        ruling = repository.get_active(season_id, competition_id, through_round, row.tie_group)
        if ruling is None:
            status = "unresolved"
        else:
            current_refs = frozenset((r.matchup_id, r.official_version) for r in ladder.result_references)
            ruling_refs = frozenset((r.matchup_id, r.official_version) for r in ruling.result_references)
            status = "resolved" if current_refs == ruling_refs else "stale"
        open_ties.append(
            OpenTie(
                tie_group=row.tie_group,
                rank=row.rank,
                competition_points=row.competition_points,
                percentage=str(row.percentage),
                points_for=str(row.points_for),
                affects_finals_seeding=True,
                affects_wooden_spoon=row.tie_group == last_place_group,
                status=status,
                ruling=ruling,
            )
        )
    return {
        "season_id": season_id,
        "competition_id": competition_id,
        "through_round": through_round,
        "open_ties": open_ties,
    }


def resolve_full_ladder_order(database, ladder: LadderSnapshot) -> tuple[str, ...]:
    """Apply every resolvable tie ruling to `ladder.rows`' own best-to-worst
    order, returning a fully deterministic `season_entry_id` order across
    every row -- `app.finals._resolve_ladder_seed`'s use case, which needs a
    total order over all ten entries, not just one tie group. Raises
    `UnresolvedTieError` (from `resolve_tie`) on the first tie group with no
    fresh ruling, in ladder rank order, so the reported group is always the
    highest-ranked one still blocking a deterministic order."""
    order: list[str] = []
    handled: set[tuple[str, ...]] = set()
    for row in ladder.rows:
        if not row.tied:
            order.append(row.season_entry_id)
            continue
        if row.tie_group in handled:
            continue
        handled.add(row.tie_group)
        order.extend(resolve_tie(database, ladder, row.tie_group))
    return tuple(order)
