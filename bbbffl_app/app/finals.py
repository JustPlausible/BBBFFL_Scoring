"""Issue #190: finals bracket generation and lifecycle for the 2026 replay's
main finals series -- the first of six #170 follow-ups, built on #197's
`create_non_ordinary_round`/`create_stream_matchup` primitives.

Read `docs/2026-finals-superscore-design.md`'s "Confirmed bracket structure",
"Seed consumption" and "Match generation/representation" sections, and issue
#190's Steve-confirmed policy comment, before changing anything here -- both
are treated as authoritative and are not re-derived in this docstring.

## Confirmed bracket structure

Top 5 by frozen finals seed qualify; 6th-10th are eliminated at bracket
creation. Week 1: seed 1 bye, Qualifying Final (2 v 3), Elimination Final
(4 v 5) -- EF loser eliminated. Week 2: Second Semi-Final (1 v QF winner),
First Semi-Final (QF loser v EF winner) -- First Semi loser eliminated.
Week 3: Preliminary Final (Second Semi loser v First Semi winner) --
Preliminary Final loser eliminated. Week 4: Grand Final (Second Semi winner
v Preliminary Final winner).

## Two confirmed policies (Steve, issue #190 comment)

1. **Tie-break**: a tied finals match, including the Grand Final, is won by
   whichever side holds the *higher* frozen finals seed captured when this
   bracket was created -- never a live/recomputed ladder, never percentage
   or points-for. See `_winner_loser`.
2. **Correction/rewind**: a corrected prerequisite finals result never
   destructively overwrites the pairing/elimination history it already
   produced. `rewind_bracket` supersedes the *immediately* downstream
   pairing/elimination -- never recursing further -- and only when that
   downstream week has no play state (an authoritative lineup submission, a
   genuinely locked position, a ruling/adjudication/override, a persisted
   calculation, or a published official result). If any such play state
   exists, it fails closed and reports the affected artifacts for a human
   competition decision; it never invalidates/replays them itself.

## Seed consumption -- single read, frozen once

`create_bracket` never calls `app.finals_seeding.resolve_finals_seed_order`.
It applies that function's exact logic against one consistent read instead
(`FinalsSeedingRepository.get_snapshot`, or a single locked-and-recompared
`LadderRepository.snapshot` call), for the reasons
`docs/2026-finals-superscore-design.md`'s "Seed consumption" section
explains at length: two independent reads (resolve, then re-read for
provenance) can observe two different ladder states if a correction lands
between them, and even a single read is not enough on its own unless the
captured `(matchup_id, official_version)` references are re-verified under
`SELECT ... FOR UPDATE`, in deterministic order, inside the same transaction
that persists the bracket -- otherwise a correction landing in the gap
between that read and this transaction's commit freezes an already-stale
order. Every later decision (Week 1 pairing, a tie-break, an audit payload)
reads the frozen `finals_bracket_seed` rows this module writes once, never a
fresh call into `app.ladder`/`app.finals_seeding`.

## Safety boundaries

This module never writes to `app.ladder` or `app.finals_seeding` -- only
ever reads. It does not implement coach lineup submission, scoring, or
result publication (issue #191's job); the only "publish" it ever performs
is materialising a *pairing* into a real `bbbffl_matchup` row via
`open_finals_week` (built on #197's `create_stream_matchup`) -- an already-
published official result is always read via the exact same
`bbbffl_matchup`/`bbbffl_official_result` tables the ordinary competition
and #191's eventual finals publish command both use.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.audit import ActorContext, append_event
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.db import _for_update_suffix, transaction
from app.finals_seeding import FinalsSeedingRepository, UnresolvedLadderTieError
from app.ladder import LadderRepository

__all__ = [
    "DownstreamPlayStateError",
    "FinalsBracket",
    "FinalsBracketAdvanceStateError",
    "FinalsBracketContextError",
    "FinalsBracketError",
    "FinalsBracketRepository",
    "FinalsElimination",
    "FinalsPairing",
    "FinalsSeedRow",
    "IncompleteFinalsWeekError",
    "SLOT_LABELS",
    "StaleFinalsResultError",
    "StaleSeedOrderError",
    "UnresolvedLadderTieError",
    "WEEK_LABELS",
]

FINALS_BRACKET_CREATED = "finals.bracket.created"
FINALS_BRACKET_ADVANCED = "finals.bracket.advanced"
FINALS_BRACKET_REWOUND = "finals.bracket.rewound"
FINALS_ELIMINATION_RECORDED = "finals.elimination.recorded"
ENTITY_TYPE_FINALS_BRACKET = "finals.bracket"
ENTITY_TYPE_FINALS_PAIRING = "finals.pairing"

WEEK_LABELS: dict[int, str] = {1: "Finals Week 1", 2: "Finals Week 2", 3: "Preliminary Final", 4: "Grand Final"}
SLOT_LABELS: dict[str, str] = {
    "bye": "Bye",
    "qf": "Qualifying Final",
    "ef": "Elimination Final",
    "second_semi": "Second Semi-Final",
    "first_semi": "First Semi-Final",
    "preliminary": "Preliminary Final",
    "grand_final": "Grand Final",
}
# Deterministic matchup_order for the two matches a week can materialise --
# only meaningful for weeks with two slots (Weeks 1-2); a single-match week
# is always matchup_order 1.
_SLOT_ORDER: dict[str, int] = {"qf": 1, "ef": 2, "second_semi": 1, "first_semi": 2, "preliminary": 1, "grand_final": 1}
_ELIMINATION_GATING_SLOT: dict[str, str] = {
    "week1_elimination_final": "first_semi",
    "week2_first_semi_final": "preliminary",
    "week3_preliminary_final": "grand_final",
}


class FinalsBracketError(ValueError):
    """Base class for every refusal this module raises. No mutation is ever
    attempted once one of these is raised."""


class FinalsBracketContextError(FinalsBracketError):
    """The season/competition/round-completion context does not satisfy the
    fail-closed checks bracket creation requires."""


class FinalsBracketAdvanceStateError(FinalsBracketError):
    """`advance_bracket`/`open_finals_week` was asked to do something the
    bracket's current state does not support (already advanced, not yet
    reached, or the referenced week/slot does not exist)."""


class IncompleteFinalsWeekError(FinalsBracketError):
    """A prerequisite finals week/matchup has no published official result
    yet -- advancing or rewinding the bracket past it is not yet possible."""


class StaleSeedOrderError(RuntimeError):
    """A concurrent correction to one of the captured ladder result
    references was detected, under lock, inside the bracket-creation
    transaction -- the caller must retry bracket creation from scratch
    (a fresh seed read), never resume with the stale order."""


class StaleFinalsResultError(RuntimeError):
    """A concurrent correction to a prerequisite finals matchup's official
    result was detected, under lock, while advancing the bracket -- the
    caller must reload and retry, never resume with the stale result."""


class DownstreamPlayStateError(FinalsBracketError):
    """`rewind_bracket` found downstream play state attached to the pairing
    it was asked to supersede. Steve's confirmed policy requires failing
    closed here for a human competition decision -- see `report` for the
    affected artifacts. Nothing is mutated when this is raised."""

    def __init__(self, message: str, *, report: dict):
        super().__init__(message)
        self.report = report


def _id() -> str:
    return str(uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class FinalsBracket:
    bracket_id: str
    season_id: str
    competition_id: str
    ordinary_competition_id: str
    seed_source: str
    finals_seeding_snapshot_id: str | None
    through_round: int | None
    latest_included_round: int | None
    created_at: str


@dataclass(frozen=True)
class FinalsSeedRow:
    bracket_id: str
    seed_position: int
    season_entry_id: str
    qualified: bool


@dataclass(frozen=True)
class FinalsPairing:
    pairing_id: str
    bracket_id: str
    week_number: int
    slot: str
    matchup_id: str | None
    home_season_entry_id: str
    away_season_entry_id: str | None
    source_matchup_id_1: str | None
    source_official_version_1: int | None
    source_matchup_id_2: str | None
    source_official_version_2: int | None
    status: str
    superseded_by_pairing_id: str | None
    created_at: str
    reason: str | None


@dataclass(frozen=True)
class FinalsElimination:
    elimination_id: str
    bracket_id: str
    stage: str
    season_entry_id: str
    source_pairing_id: str | None
    status: str
    superseded_by_elimination_id: str | None
    created_at: str
    reason: str | None


@dataclass(frozen=True)
class _LockedResult:
    matchup_id: str
    version: int
    home_score: Any
    away_score: Any


@dataclass(frozen=True)
class _Derivation:
    """One `advance_bracket`/`rewind_bracket` step's fully-resolved output:
    the new pairing(s) it derives for `target_week`, and (except for the
    Week 3 -> Week 4 step) the single elimination it derives alongside them.
    `elimination_gate_slot` names which of `new_pairings`' slots the
    elimination's fate is tied to (see module docstring, policy 2) --
    `None` when there is no elimination for this step."""

    new_pairings: tuple[tuple[str, str, str, tuple[str, int], tuple[str, int] | None], ...]
    elimination: tuple[str, str, str] | None
    elimination_gate_slot: str | None


def _pairing(row) -> FinalsPairing:
    return FinalsPairing(**dict(row))


def _elimination(row) -> FinalsElimination:
    return FinalsElimination(**dict(row))


class FinalsBracketRepository:
    def __init__(self, database):
        self.database = database

    # -- Reads --------------------------------------------------------------

    def get_bracket(self, season_id: str, competition_id: str) -> FinalsBracket | None:
        row = self.database.execute(
            "SELECT * FROM finals_bracket WHERE season_id=? AND competition_id=?", (season_id, competition_id)
        ).fetchone()
        return FinalsBracket(**dict(row)) if row else None

    def get_bracket_by_id(self, bracket_id: str) -> FinalsBracket | None:
        row = self.database.execute("SELECT * FROM finals_bracket WHERE bracket_id=?", (bracket_id,)).fetchone()
        return FinalsBracket(**dict(row)) if row else None

    def list_seed_rows(self, bracket_id: str) -> tuple[FinalsSeedRow, ...]:
        rows = self.database.execute(
            "SELECT * FROM finals_bracket_seed WHERE bracket_id=? ORDER BY seed_position", (bracket_id,)
        ).fetchall()
        return tuple(
            FinalsSeedRow(row["bracket_id"], row["seed_position"], row["season_entry_id"], bool(row["qualified"]))
            for row in rows
        )

    def list_pairings(
        self, bracket_id: str, *, week_number: int | None = None, include_superseded: bool = False
    ) -> tuple[FinalsPairing, ...]:
        clauses, params = ["bracket_id=?"], [bracket_id]
        if week_number is not None:
            clauses.append("week_number=?")
            params.append(week_number)
        if not include_superseded:
            clauses.append("status='active'")
        rows = self.database.execute(
            f"SELECT * FROM finals_bracket_pairing WHERE {' AND '.join(clauses)} ORDER BY week_number, slot",
            tuple(params),
        ).fetchall()
        return tuple(_pairing(row) for row in rows)

    def list_eliminations(self, bracket_id: str, *, include_superseded: bool = False) -> tuple[FinalsElimination, ...]:
        clauses, params = ["bracket_id=?"], [bracket_id]
        if not include_superseded:
            clauses.append("status='active'")
        rows = self.database.execute(
            f"SELECT * FROM finals_bracket_elimination WHERE {' AND '.join(clauses)} ORDER BY created_at",
            tuple(params),
        ).fetchall()
        return tuple(_elimination(row) for row in rows)

    def get_week_round_id(self, bracket_id: str, week_number: int) -> str:
        row = self.database.execute(
            "SELECT bbbffl_round_id FROM finals_bracket_week WHERE bracket_id=? AND week_number=?",
            (bracket_id, week_number),
        ).fetchone()
        if row is None:
            raise KeyError((bracket_id, week_number))
        return row["bbbffl_round_id"]

    def describe(self, bracket_id: str) -> dict:
        """A full, JSON-friendly read model for the CLI and the finals-
        preflight route -- never used to derive a decision internally
        (every internal decision reads the persisted rows directly)."""
        bracket = self.get_bracket_by_id(bracket_id)
        if bracket is None:
            raise KeyError(bracket_id)
        weeks = self.database.execute(
            "SELECT * FROM finals_bracket_week WHERE bracket_id=? ORDER BY week_number", (bracket_id,)
        ).fetchall()
        lifecycle = CompetitionLifecycleRepository(self.database)
        return {
            "bracket": vars(bracket),
            "seed": [vars(row) for row in self.list_seed_rows(bracket_id)],
            "weeks": [
                {
                    **dict(week),
                    "round_state": (lifecycle.get_round(week["bbbffl_round_id"]) or _NoRound()).state,
                    "pairings": [vars(p) for p in self.list_pairings(bracket_id, week_number=week["week_number"])],
                }
                for week in weeks
            ],
            "eliminations": [vars(row) for row in self.list_eliminations(bracket_id)],
        }

    # -- Bracket creation -----------------------------------------------------

    def preview_create_bracket(self, season_id: str, competition_id: str, ordinary_competition_id: str) -> dict:
        """Read-only: never mutates, never takes a row lock. Reports the same
        fail-closed checks `create_bracket` performs, without acting on them."""
        report: dict = {
            "season_id": season_id,
            "competition_id": competition_id,
            "ordinary_competition_id": ordinary_competition_id,
            "context_ready": False,
            "diagnostic": None,
            "seed_source": None,
            "seed_order": None,
            "bracket_exists": False,
            "existing_bracket_id": None,
        }
        existing = self.get_bracket(season_id, competition_id)
        report["bracket_exists"] = existing is not None
        report["existing_bracket_id"] = existing.bracket_id if existing is not None else None
        try:
            seed_order, seed_source, _provenance = self._resolve_seed(
                season_id, competition_id, ordinary_competition_id
            )
        except FinalsBracketError as exc:
            report["diagnostic"] = str(exc)
            return report
        report["context_ready"] = True
        report["seed_source"] = seed_source
        report["seed_order"] = list(seed_order)
        return report

    def _resolve_seed(
        self, season_id: str, competition_id: str, ordinary_competition_id: str
    ) -> tuple[tuple[str, ...], str, dict]:
        """Applies `app.finals_seeding.resolve_finals_seed_order`'s exact
        logic against one consistent read -- see module docstring's "Seed
        consumption" section for why this is not a call to that function
        itself. Returns `(seed_order, seed_source, provenance)`, where
        `provenance` carries whichever fields `create_bracket` needs to
        freeze (`snapshot_id`, or `through_round`/`latest_included_round`/
        `result_references`)."""
        competition = self.database.execute(
            "SELECT * FROM competition_stream WHERE competition_id=?", (competition_id,)
        ).fetchone()
        if competition is None or competition["season_id"] != season_id or competition["stream_type"] != "finals":
            raise FinalsBracketContextError("competition_id must name this season's own finals competition stream")

        snapshot = FinalsSeedingRepository(self.database).get_snapshot(season_id)
        if snapshot is not None and snapshot.competition_id == ordinary_competition_id:
            seed_order = tuple(row.season_entry_id for row in snapshot.seed_rows)
            return seed_order, "snapshot", {"finals_seeding_snapshot_id": snapshot.snapshot_id}

        season = self.database.execute("SELECT * FROM bbbffl_season WHERE season_id=?", (season_id,)).fetchone()
        if season is None:
            raise KeyError(season_id)
        ordinary = self.database.execute(
            "SELECT * FROM competition_stream WHERE competition_id=?", (ordinary_competition_id,)
        ).fetchone()
        if ordinary is None or ordinary["season_id"] != season_id or ordinary["stream_type"] != "ordinary":
            raise FinalsBracketContextError(
                "ordinary_competition_id must name this season's own ordinary home-and-away competition"
            )
        round_count = season["regular_season_round_count"] if "regular_season_round_count" in season.keys() else 20
        lifecycle_rows = self.database.execute(
            "SELECT br.sequence, bl.state FROM bbbffl_round br "
            "LEFT JOIN bbbffl_round_lifecycle bl ON bl.bbbffl_round_id = br.bbbffl_round_id "
            "WHERE br.competition_id=? AND br.sequence<=?",
            (ordinary_competition_id, round_count),
        ).fetchall()
        present = {row["sequence"] for row in lifecycle_rows}
        missing = sorted(set(range(1, round_count + 1)) - present)
        not_final = sorted(row["sequence"] for row in lifecycle_rows if row["state"] != "final")
        if missing or not_final:
            raise FinalsBracketContextError(
                f"every regular-season round through {round_count} must be final before finals seeding "
                f"(missing: {missing}, not yet final: {not_final})"
            )
        ladder = LadderRepository(self.database).snapshot(ordinary_competition_id, round_count)
        if ladder.season_id != season_id:
            raise FinalsBracketContextError(
                f"ordinary_competition_id {ordinary_competition_id!r} belongs to season {ladder.season_id!r}, "
                f"not the requested season {season_id!r}"
            )
        tied_entries = [row.season_entry_id for row in ladder.rows if row.tied]
        if tied_entries:
            raise UnresolvedLadderTieError(
                f"cannot derive a deterministic finals seed order: the mathematical ladder has an unresolved tie "
                f"among {tied_entries} -- this requires an explicit, audited Scorer/competition-governance "
                "determination, never the ladder's own season_entry_id serialization order"
            )
        seed_order = tuple(row.season_entry_id for row in ladder.rows)
        return (
            seed_order,
            "ladder",
            {
                "through_round": round_count,
                "latest_included_round": ladder.latest_included_round,
                "result_references": ladder.result_references,
            },
        )

    def create_bracket(
        self, season_id: str, competition_id: str, ordinary_competition_id: str, *, actor: ActorContext, reason: str
    ) -> dict:
        """Create (or idempotently confirm) the one finals bracket for this
        `(season_id, competition_id)`. Fails closed, no mutation, on any
        context/tie/staleness problem. See module docstring."""
        if not reason or not reason.strip():
            raise FinalsBracketError("finals bracket creation requires an explicit, substantive reason")

        existing = self.get_bracket(season_id, competition_id)
        if existing is not None:
            if existing.ordinary_competition_id != ordinary_competition_id:
                raise FinalsBracketContextError(
                    "a finals bracket already exists for this season/competition against a different "
                    "ordinary_competition_id -- refusing to silently replace it"
                )
            return {"created": False, "bracket": existing, "audit_event_id": None}

        seed_order, seed_source, provenance = self._resolve_seed(season_id, competition_id, ordinary_competition_id)
        if len(seed_order) != 10:
            raise FinalsBracketContextError(
                f"finals bracket requires exactly 10 seeded entries; found {len(seed_order)}"
            )

        try:
            with transaction(self.database) as conn:
                if seed_source == "ladder":
                    for reference in sorted(provenance["result_references"], key=lambda r: r.matchup_id):
                        row = conn.execute(
                            "SELECT effective_official_version FROM bbbffl_matchup WHERE matchup_id=?"
                            + _for_update_suffix(self.database),
                            (reference.matchup_id,),
                        ).fetchone()
                        if row is None or row["effective_official_version"] != reference.official_version:
                            raise StaleSeedOrderError(
                                f"matchup {reference.matchup_id}'s official result changed since the ladder was "
                                "read for finals seeding; retry bracket creation from a fresh read"
                            )

                bracket_id = _id()
                now = _now()
                conn.execute(
                    "INSERT INTO finals_bracket VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        bracket_id,
                        season_id,
                        competition_id,
                        ordinary_competition_id,
                        seed_source,
                        provenance.get("finals_seeding_snapshot_id"),
                        provenance.get("through_round"),
                        provenance.get("latest_included_round"),
                        now,
                    ),
                )
                for position, entry_id in enumerate(seed_order, 1):
                    conn.execute(
                        "INSERT INTO finals_bracket_seed VALUES (?, ?, ?, ?)",
                        (bracket_id, position, entry_id, position <= 5),
                    )
                for reference in provenance.get("result_references", ()):
                    conn.execute(
                        "INSERT INTO finals_bracket_result_reference VALUES (?, ?, ?, ?)",
                        (_id(), bracket_id, reference.matchup_id, reference.official_version),
                    )
                round_ids: dict[int, str] = {}
                for week_number, label in WEEK_LABELS.items():
                    round_id = _id()
                    conn.execute(
                        "INSERT INTO bbbffl_round VALUES (?, ?, ?, ?, ?, ?)",
                        (round_id, competition_id, f"finals-week-{week_number}", label, week_number, now),
                    )
                    conn.execute(
                        "INSERT INTO finals_bracket_week VALUES (?, ?, ?, ?)",
                        (bracket_id, week_number, round_id, label),
                    )
                    round_ids[week_number] = round_id

                seed_by_position = dict(enumerate(seed_order, 1))
                week1 = (
                    ("bye", seed_by_position[1], None),
                    ("qf", seed_by_position[2], seed_by_position[3]),
                    ("ef", seed_by_position[4], seed_by_position[5]),
                )
                for slot, home, away in week1:
                    conn.execute(
                        "INSERT INTO finals_bracket_pairing VALUES "
                        "(?, ?, 1, ?, NULL, ?, ?, NULL, NULL, NULL, NULL, 'active', NULL, ?, ?)",
                        (_id(), bracket_id, slot, home, away, now, reason),
                    )
                for position in range(6, 11):
                    conn.execute(
                        "INSERT INTO finals_bracket_elimination VALUES "
                        "(?, ?, 'pre_finals', ?, NULL, 'active', NULL, ?, ?)",
                        (_id(), bracket_id, seed_by_position[position], now, reason),
                    )

                event = append_event(
                    conn,
                    actor=actor,
                    action=FINALS_BRACKET_CREATED,
                    entity_type=ENTITY_TYPE_FINALS_BRACKET,
                    entity_id=bracket_id,
                    reason=reason,
                    after_state={
                        "seed_source": seed_source,
                        "seed_order": list(seed_order),
                        **{k: v for k, v in provenance.items() if k != "result_references"},
                    },
                    payload={
                        "season_id": season_id,
                        "competition_id": competition_id,
                        "ordinary_competition_id": ordinary_competition_id,
                    },
                )
        except IntegrityError:
            # A concurrent caller created the same (season_id, competition_id)
            # bracket first (uq_finals_bracket_season_competition) -- resolve
            # exactly like the pre-check above, never a raw database error.
            existing = self.get_bracket(season_id, competition_id)
            if existing is not None and existing.ordinary_competition_id == ordinary_competition_id:
                return {"created": False, "bracket": existing, "audit_event_id": None}
            raise

        return {"created": True, "bracket": self.get_bracket_by_id(bracket_id), "audit_event_id": event.event_id}

    # -- Opening a finals week ------------------------------------------------

    def open_finals_week(
        self, bracket_id: str, week_number: int, *, actor: ActorContext, reason: str | None = None
    ) -> dict:
        """Stream-aware equivalent of `app.round_preflight.open_preflight_round`
        for one finals week: creates the round's lifecycle row if needed
        (requires an accepted AFL mapping, exactly like an ordinary round),
        materialises every active pairing that has not yet been realised
        into a real `bbbffl_matchup`, and transitions the round to `open`.
        Idempotent against a round that is already open (returns its
        current state rather than raising) but never re-opens a round that
        has moved past `open`."""
        bracket = self.get_bracket_by_id(bracket_id)
        if bracket is None:
            raise KeyError(bracket_id)
        round_id = self.get_week_round_id(bracket_id, week_number)
        pairings = self.list_pairings(bracket_id, week_number=week_number)
        if not pairings:
            raise FinalsBracketAdvanceStateError(
                f"finals week {week_number} has no pairing yet; run advance_bracket first"
            )
        lifecycle = CompetitionLifecycleRepository(self.database)
        default_reason = reason or f"Finals {WEEK_LABELS[week_number]} round context frozen after operator preflight"
        if lifecycle.get_round(round_id) is None:
            lifecycle.create_non_ordinary_round(round_id, actor=actor, reason=default_reason)
        for pairing in pairings:
            if pairing.slot == "bye" or pairing.matchup_id is not None:
                continue
            self._materialise_pairing(round_id, pairing, actor=actor, reason=default_reason)
        current = lifecycle.get_round(round_id)
        if current.state != "upcoming":
            return {"round": current, "already_open": True}
        opened = lifecycle.transition(
            round_id, "open", actor=actor, reason=reason or f"Explicit finals {WEEK_LABELS[week_number]} open action"
        )
        return {"round": opened, "already_open": False}

    def _materialise_pairing(self, round_id: str, pairing: FinalsPairing, *, actor: ActorContext, reason: str) -> None:
        with transaction(self.database) as conn:
            current = conn.execute(
                "SELECT matchup_id FROM finals_bracket_pairing WHERE pairing_id=?" + _for_update_suffix(self.database),
                (pairing.pairing_id,),
            ).fetchone()
            if current is None:
                raise KeyError(pairing.pairing_id)
            if current["matchup_id"] is not None:
                return  # already materialised by a concurrent caller -- idempotent no-op
            round_row = conn.execute(
                "SELECT l.*, c.stream_type FROM bbbffl_round_lifecycle l "
                "JOIN competition_stream c ON c.competition_id = l.competition_id "
                "WHERE l.bbbffl_round_id=?" + _for_update_suffix(self.database),
                (round_id,),
            ).fetchone()
            if round_row is None or round_row["stream_type"] != "finals":
                raise FinalsBracketAdvanceStateError("finals week round does not have a finals lifecycle to attach to")
            matchup_id = _id()
            conn.execute(
                "INSERT INTO bbbffl_matchup VALUES (?, ?, NULL, ?, ?, ?, NULL, 1)",
                (
                    matchup_id,
                    round_id,
                    _SLOT_ORDER[pairing.slot],
                    pairing.home_season_entry_id,
                    pairing.away_season_entry_id,
                ),
            )
            conn.execute(
                "UPDATE finals_bracket_pairing SET matchup_id=? WHERE pairing_id=? AND matchup_id IS NULL",
                (matchup_id, pairing.pairing_id),
            )
            append_event(
                conn,
                actor=actor,
                action="competition.matchup.created",
                entity_type="competition.matchup",
                entity_id=matchup_id,
                entity_version="1",
                reason=reason,
                after_state={
                    "bbbffl_round_id": round_id,
                    "slot": pairing.slot,
                    "home_season_entry_id": pairing.home_season_entry_id,
                    "away_season_entry_id": pairing.away_season_entry_id,
                },
                payload={"finals_pairing_id": pairing.pairing_id},
            )

    # -- Advancing the bracket ------------------------------------------------

    def preview_advance_bracket(self, bracket_id: str, from_week: int) -> dict:
        """Read-only report of what `advance_bracket(from_week=...)` would
        derive right now, including the exact matchup versions to pass back
        as `expected_versions` -- never mutates, never holds a lock past
        this call."""
        seed_rank = self._seed_rank(bracket_id)
        source_ids = self._source_matchup_ids(bracket_id, from_week)
        locked = {}
        for matchup_id in sorted(source_ids):
            row = self.database.execute(
                "SELECT effective_official_version FROM bbbffl_matchup WHERE matchup_id=?", (matchup_id,)
            ).fetchone()
            if row is None:
                raise KeyError(matchup_id)
            if row["effective_official_version"] is None:
                raise IncompleteFinalsWeekError(f"matchup {matchup_id} has no published official result yet")
            locked[matchup_id] = row["effective_official_version"]
        derivation = self._derive(self.database, bracket_id, from_week, seed_rank, locked)
        return {
            "bracket_id": bracket_id,
            "from_week": from_week,
            "target_week": from_week + 1,
            "expected_versions": locked,
            "new_pairings": [
                {"slot": slot, "home_season_entry_id": home, "away_season_entry_id": away}
                for slot, home, away, _s1, _s2 in derivation.new_pairings
            ],
            "elimination": (
                {"stage": derivation.elimination[0], "season_entry_id": derivation.elimination[1]}
                if derivation.elimination
                else None
            ),
        }

    def advance_bracket(
        self,
        bracket_id: str,
        from_week: int,
        *,
        actor: ActorContext,
        reason: str,
        expected_versions: dict[str, int] | None = None,
    ) -> dict:
        """Compute and persist `from_week + 1`'s pairing(s) (and, for Weeks
        1-3, the elimination that week's result determines) from `from_week`'s
        official result(s) and this bracket's frozen seed. Locks every
        prerequisite matchup row `FOR UPDATE`, in deterministic (sorted)
        order, inside this transaction before deriving or persisting
        anything -- a concurrent correction to one of those exact matchups
        cannot land in the gap, and `expected_versions` (from a prior
        `preview_advance_bracket` call) is re-checked under that same lock,
        raising `StaleFinalsResultError` rather than silently advancing from
        a superseded result."""
        if from_week not in (1, 2, 3):
            raise ValueError("from_week must be 1, 2, or 3")
        if not reason or not reason.strip():
            raise FinalsBracketError("advancing the bracket requires an explicit, substantive reason")
        if self.get_bracket_by_id(bracket_id) is None:
            raise KeyError(bracket_id)
        target_week = from_week + 1
        seed_rank = self._seed_rank(bracket_id)
        source_ids = self._source_matchup_ids(bracket_id, from_week)

        with transaction(self.database) as conn:
            for slot in self._target_slots(from_week):
                if self._active_pairing_row(conn, bracket_id, target_week, slot) is not None:
                    raise FinalsBracketAdvanceStateError(
                        f"finals week {target_week} slot {slot!r} has already been advanced to; "
                        "use rewind_bracket to supersede an existing derivation"
                    )
            locked = {mid: self._lock_matchup_version(conn, mid, expected_versions) for mid in sorted(source_ids)}
            derivation = self._derive(conn, bracket_id, from_week, seed_rank, locked)

            for slot, home, away, source1, source2 in derivation.new_pairings:
                pairing_id = _id()
                self._insert_pairing(
                    conn,
                    pairing_id=pairing_id,
                    bracket_id=bracket_id,
                    week_number=target_week,
                    slot=slot,
                    home=home,
                    away=away,
                    source1=source1,
                    source2=source2,
                    reason=reason,
                )
            if derivation.elimination is not None:
                stage, eliminated_entry, source_pairing_id = derivation.elimination
                conn.execute(
                    "INSERT INTO finals_bracket_elimination VALUES (?, ?, ?, ?, ?, 'active', NULL, ?, ?)",
                    (_id(), bracket_id, stage, eliminated_entry, source_pairing_id, _now(), reason),
                )
            event = append_event(
                conn,
                actor=actor,
                action=FINALS_BRACKET_ADVANCED,
                entity_type=ENTITY_TYPE_FINALS_BRACKET,
                entity_id=bracket_id,
                reason=reason,
                after_state={
                    "from_week": from_week,
                    "target_week": target_week,
                    "pairings": [
                        {"slot": slot, "home_season_entry_id": home, "away_season_entry_id": away}
                        for slot, home, away, _s1, _s2 in derivation.new_pairings
                    ],
                    "elimination": (
                        {"stage": derivation.elimination[0], "season_entry_id": derivation.elimination[1]}
                        if derivation.elimination
                        else None
                    ),
                },
            )
        return {"bracket_id": bracket_id, "target_week": target_week, "audit_event_id": event.event_id}

    # -- Correction-triggered rewind -------------------------------------------

    def rewind_bracket(
        self, bracket_id: str, from_week: int, *, actor: ActorContext, reason: str, apply: bool = False
    ) -> dict:
        """Steve's confirmed correction/rewind policy: re-derive `from_week +
        1`'s already-persisted pairing(s)/elimination from `from_week`'s
        *current* (possibly since-corrected) result(s), superseding them
        only if the derivation actually changed and only if the pairing
        being superseded has no downstream play state. `apply=False`
        (default) is a read-only preview -- it reports what would change and
        what would block, but never mutates and never raises
        `DownstreamPlayStateError`. `apply=True` performs the same
        computation and either commits the supersede or raises
        `DownstreamPlayStateError` if anything is blocked, atomically: never
        a partial supersede of only the pairing or only the elimination."""
        if from_week not in (1, 2, 3):
            raise ValueError("from_week must be 1, 2, or 3")
        if apply and (not reason or not reason.strip()):
            raise FinalsBracketError("applying a bracket rewind requires an explicit, substantive reason")
        if self.get_bracket_by_id(bracket_id) is None:
            raise KeyError(bracket_id)
        target_week = from_week + 1
        seed_rank = self._seed_rank(bracket_id)
        source_ids = self._source_matchup_ids(bracket_id, from_week)

        with transaction(self.database) as conn:
            locked = {mid: self._lock_matchup_version(conn, mid, None) for mid in sorted(source_ids)}
            derivation = self._derive(conn, bracket_id, from_week, seed_rank, locked)

            pairing_changes: list[dict] = []
            blocked_slots: set[str] = set()
            for slot, home, away, source1, source2 in derivation.new_pairings:
                existing = self._active_pairing_row(conn, bracket_id, target_week, slot)
                if existing is None:
                    continue  # never advanced to this slot yet -- advance_bracket's job, not rewind's
                unchanged = (
                    existing["home_season_entry_id"] == home
                    and existing["away_season_entry_id"] == away
                    and existing["source_official_version_1"] == source1[1]
                    and existing["source_official_version_2"] == (source2[1] if source2 else None)
                )
                if unchanged:
                    continue
                artifacts = self._downstream_play_state(conn, existing)
                if artifacts:
                    blocked_slots.add(slot)
                    pairing_changes.append(
                        {"slot": slot, "pairing_id": existing["pairing_id"], "blocked": True, "artifacts": artifacts}
                    )
                else:
                    pairing_changes.append(
                        {
                            "slot": slot,
                            "pairing_id": existing["pairing_id"],
                            "blocked": False,
                            "new_home_season_entry_id": home,
                            "new_away_season_entry_id": away,
                            "source1": source1,
                            "source2": source2,
                        }
                    )

            elimination_change: dict | None = None
            if derivation.elimination is not None:
                stage, new_entry, source_pairing_id = derivation.elimination
                existing_elim = self._active_elimination_row(conn, bracket_id, stage)
                gate_slot = derivation.elimination_gate_slot
                if existing_elim is not None and existing_elim["season_entry_id"] != new_entry:
                    if gate_slot in blocked_slots:
                        elimination_change = {
                            "stage": stage,
                            "elimination_id": existing_elim["elimination_id"],
                            "blocked": True,
                        }
                    else:
                        elimination_change = {
                            "stage": stage,
                            "elimination_id": existing_elim["elimination_id"],
                            "blocked": False,
                            "new_season_entry_id": new_entry,
                            "source_pairing_id": source_pairing_id,
                        }

            any_blocked = any(change["blocked"] for change in pairing_changes) or bool(
                elimination_change and elimination_change["blocked"]
            )
            report = {
                "bracket_id": bracket_id,
                "from_week": from_week,
                "target_week": target_week,
                "pairing_changes": pairing_changes,
                "elimination_change": elimination_change,
                "no_change_needed": not pairing_changes and elimination_change is None,
                "blocked": any_blocked,
            }
            if not apply:
                return report
            if any_blocked:
                raise DownstreamPlayStateError(
                    "cannot rewind: downstream play state exists for the affected pairing(s); "
                    "a human competition decision is required",
                    report=report,
                )
            if report["no_change_needed"]:
                return report

            # Three steps, not two, per write -- `superseded_by_*_id` is a
            # foreign key into the very row being inserted, and the partial
            # unique index only allows one *active* row per slot/entry at a
            # time. Flipping the old row to 'superseded' (pointer left NULL)
            # first vacates the active slot without yet referencing a row
            # that doesn't exist; only once the new row is inserted can the
            # old row's pointer be completed. All three statements share
            # this one transaction, so the old row is never observably
            # 'superseded' with no pointer, or the slot never observably
            # without an active row, from outside this transaction.
            now = _now()
            for change in pairing_changes:
                new_pairing_id = _id()
                conn.execute(
                    "UPDATE finals_bracket_pairing SET status='superseded' WHERE pairing_id=?",
                    (change["pairing_id"],),
                )
                self._insert_pairing(
                    conn,
                    pairing_id=new_pairing_id,
                    bracket_id=bracket_id,
                    week_number=target_week,
                    slot=change["slot"],
                    home=change["new_home_season_entry_id"],
                    away=change["new_away_season_entry_id"],
                    source1=change["source1"],
                    source2=change["source2"],
                    reason=reason,
                    created_at=now,
                )
                conn.execute(
                    "UPDATE finals_bracket_pairing SET superseded_by_pairing_id=? WHERE pairing_id=?",
                    (new_pairing_id, change["pairing_id"]),
                )
            if elimination_change is not None:
                new_elimination_id = _id()
                conn.execute(
                    "UPDATE finals_bracket_elimination SET status='superseded' WHERE elimination_id=?",
                    (elimination_change["elimination_id"],),
                )
                conn.execute(
                    "INSERT INTO finals_bracket_elimination VALUES (?, ?, ?, ?, ?, 'active', NULL, ?, ?)",
                    (
                        new_elimination_id,
                        bracket_id,
                        elimination_change["stage"],
                        elimination_change["new_season_entry_id"],
                        elimination_change["source_pairing_id"],
                        now,
                        reason,
                    ),
                )
                conn.execute(
                    "UPDATE finals_bracket_elimination SET superseded_by_elimination_id=? WHERE elimination_id=?",
                    (new_elimination_id, elimination_change["elimination_id"]),
                )
            event = append_event(
                conn,
                actor=actor,
                action=FINALS_BRACKET_REWOUND,
                entity_type=ENTITY_TYPE_FINALS_BRACKET,
                entity_id=bracket_id,
                reason=reason,
                after_state=report,
            )
            report["audit_event_id"] = event.event_id
            return report

    # -- Internal derivation helpers -------------------------------------------

    def _seed_rank(self, bracket_id: str) -> dict[str, int]:
        return {row.season_entry_id: row.seed_position for row in self.list_seed_rows(bracket_id)}

    def _seed_entry(self, bracket_id: str, position: int) -> str:
        row = self.database.execute(
            "SELECT season_entry_id FROM finals_bracket_seed WHERE bracket_id=? AND seed_position=?",
            (bracket_id, position),
        ).fetchone()
        if row is None:
            raise KeyError((bracket_id, position))
        return row["season_entry_id"]

    @staticmethod
    def _target_slots(from_week: int) -> tuple[str, ...]:
        return {1: ("second_semi", "first_semi"), 2: ("preliminary",), 3: ("grand_final",)}[from_week]

    def _source_matchup_ids(self, bracket_id: str, from_week: int) -> set[str]:
        if from_week == 1:
            slots = (1, "qf"), (1, "ef")
        elif from_week == 2:
            slots = (2, "second_semi"), (2, "first_semi")
        else:
            slots = (2, "second_semi"), (3, "preliminary")
        ids = set()
        for week_number, slot in slots:
            row = self.database.execute(
                "SELECT matchup_id FROM finals_bracket_pairing WHERE bracket_id=? AND week_number=? AND slot=? AND status='active'",
                (bracket_id, week_number, slot),
            ).fetchone()
            if row is None:
                raise IncompleteFinalsWeekError(
                    f"finals week {week_number} slot {slot!r} has no active pairing yet -- has the bracket reached "
                    "that week?"
                )
            if row["matchup_id"] is None:
                raise IncompleteFinalsWeekError(
                    f"finals week {week_number} slot {slot!r} has not been opened/played yet"
                )
            ids.add(row["matchup_id"])
        return ids

    def _lock_matchup_version(self, conn, matchup_id: str, expected_versions: dict[str, int] | None) -> int:
        row = conn.execute(
            "SELECT effective_official_version FROM bbbffl_matchup WHERE matchup_id=?"
            + _for_update_suffix(self.database),
            (matchup_id,),
        ).fetchone()
        if row is None:
            raise KeyError(matchup_id)
        current = row["effective_official_version"]
        if expected_versions is not None:
            expected = expected_versions.get(matchup_id)
            if expected is not None and current != expected:
                raise StaleFinalsResultError(
                    f"matchup {matchup_id}'s official result changed since it was last read "
                    f"(now version {current!r}, expected {expected!r}); reload and retry"
                )
        if current is None:
            raise IncompleteFinalsWeekError(f"matchup {matchup_id} has no published official result yet")
        return current

    def _pairing_row_by_slot(self, conn, bracket_id: str, week_number: int, slot: str):
        return conn.execute(
            "SELECT * FROM finals_bracket_pairing WHERE bracket_id=? AND week_number=? AND slot=? AND status='active'",
            (bracket_id, week_number, slot),
        ).fetchone()

    def _active_pairing_row(self, conn, bracket_id: str, week_number: int, slot: str):
        return self._pairing_row_by_slot(conn, bracket_id, week_number, slot)

    def _active_elimination_row(self, conn, bracket_id: str, stage: str):
        return conn.execute(
            "SELECT * FROM finals_bracket_elimination WHERE bracket_id=? AND stage=? AND status='active'",
            (bracket_id, stage),
        ).fetchone()

    def _winner_loser(self, pairing_row, result: _LockedResult, seed_rank: dict[str, int]) -> tuple[str, str]:
        """Steve's confirmed tie-break policy: the higher frozen finals seed
        wins a tied finals match, at every stage including the Grand Final.
        Never re-reads the ladder; `seed_rank` is this bracket's own frozen
        `finals_bracket_seed` order."""
        home, away = pairing_row["home_season_entry_id"], pairing_row["away_season_entry_id"]
        if result.home_score > result.away_score:
            return home, away
        if result.away_score > result.home_score:
            return away, home
        return (home, away) if seed_rank[home] < seed_rank[away] else (away, home)

    def _read_result(self, conn, matchup_id: str, version: int) -> _LockedResult:
        row = conn.execute(
            "SELECT home_score, away_score FROM bbbffl_official_result WHERE matchup_id=? AND version=?",
            (matchup_id, version),
        ).fetchone()
        if row is None:
            raise KeyError((matchup_id, version))
        return _LockedResult(matchup_id, version, row["home_score"], row["away_score"])

    def _derive(
        self, conn, bracket_id: str, from_week: int, seed_rank: dict[str, int], locked_versions: dict[str, int]
    ) -> _Derivation:
        if from_week == 1:
            qf = self._pairing_row_by_slot(conn, bracket_id, 1, "qf")
            ef = self._pairing_row_by_slot(conn, bracket_id, 1, "ef")
            qf_result = self._read_result(conn, qf["matchup_id"], locked_versions[qf["matchup_id"]])
            ef_result = self._read_result(conn, ef["matchup_id"], locked_versions[ef["matchup_id"]])
            qf_winner, qf_loser = self._winner_loser(qf, qf_result, seed_rank)
            ef_winner, ef_loser = self._winner_loser(ef, ef_result, seed_rank)
            seed1 = self._seed_entry(bracket_id, 1)
            return _Derivation(
                new_pairings=(
                    ("second_semi", seed1, qf_winner, (qf["matchup_id"], qf_result.version), None),
                    (
                        "first_semi",
                        qf_loser,
                        ef_winner,
                        (qf["matchup_id"], qf_result.version),
                        (ef["matchup_id"], ef_result.version),
                    ),
                ),
                elimination=("week1_elimination_final", ef_loser, ef["pairing_id"]),
                elimination_gate_slot="first_semi",
            )
        if from_week == 2:
            ss2 = self._pairing_row_by_slot(conn, bracket_id, 2, "second_semi")
            fs = self._pairing_row_by_slot(conn, bracket_id, 2, "first_semi")
            ss2_result = self._read_result(conn, ss2["matchup_id"], locked_versions[ss2["matchup_id"]])
            fs_result = self._read_result(conn, fs["matchup_id"], locked_versions[fs["matchup_id"]])
            ss2_winner, ss2_loser = self._winner_loser(ss2, ss2_result, seed_rank)
            fs_winner, fs_loser = self._winner_loser(fs, fs_result, seed_rank)
            return _Derivation(
                new_pairings=(
                    (
                        "preliminary",
                        ss2_loser,
                        fs_winner,
                        (ss2["matchup_id"], ss2_result.version),
                        (fs["matchup_id"], fs_result.version),
                    ),
                ),
                elimination=("week2_first_semi_final", fs_loser, fs["pairing_id"]),
                elimination_gate_slot="preliminary",
            )
        # from_week == 3
        ss2 = self._pairing_row_by_slot(conn, bracket_id, 2, "second_semi")
        pf = self._pairing_row_by_slot(conn, bracket_id, 3, "preliminary")
        ss2_result = self._read_result(conn, ss2["matchup_id"], locked_versions[ss2["matchup_id"]])
        pf_result = self._read_result(conn, pf["matchup_id"], locked_versions[pf["matchup_id"]])
        ss2_winner, _ss2_loser = self._winner_loser(ss2, ss2_result, seed_rank)
        pf_winner, pf_loser = self._winner_loser(pf, pf_result, seed_rank)
        return _Derivation(
            new_pairings=(
                (
                    "grand_final",
                    ss2_winner,
                    pf_winner,
                    (ss2["matchup_id"], ss2_result.version),
                    (pf["matchup_id"], pf_result.version),
                ),
            ),
            elimination=("week3_preliminary_final", pf_loser, pf["pairing_id"]),
            elimination_gate_slot="grand_final",
        )

    def _insert_pairing(
        self, conn, *, pairing_id, bracket_id, week_number, slot, home, away, source1, source2, reason, created_at=None
    ) -> None:
        conn.execute(
            "INSERT INTO finals_bracket_pairing VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?)",
            (
                pairing_id,
                bracket_id,
                week_number,
                slot,
                home,
                away,
                source1[0],
                source1[1],
                source2[0] if source2 else None,
                source2[1] if source2 else None,
                created_at or _now(),
                reason,
            ),
        )

    def _downstream_play_state(self, conn, pairing_row) -> list[dict]:
        """Non-empty iff Steve's confirmed policy requires failing closed:
        an authoritative lineup submission, a genuinely locked position, a
        ruling/adjudication/override, a persisted calculation, or a
        published official result already exists for the pairing being
        superseded. Locks the pairing's own `bbbffl_matchup` row (if
        realised) `FOR UPDATE` as part of this check, inside the caller's
        transaction, so nothing about it can change between this check and
        the supersede that follows it. Never mutates anything itself."""
        matchup_id = pairing_row["matchup_id"]
        artifacts: list[dict] = []
        entries = [
            e for e in (pairing_row["home_season_entry_id"], pairing_row["away_season_entry_id"]) if e is not None
        ]
        round_row = conn.execute(
            "SELECT bbbffl_round_id FROM finals_bracket_pairing fp "
            "JOIN finals_bracket_week fw ON fw.bracket_id = fp.bracket_id AND fw.week_number = fp.week_number "
            "WHERE fp.pairing_id=?",
            (pairing_row["pairing_id"],),
        ).fetchone()
        round_id = round_row["bbbffl_round_id"] if round_row else None
        if round_id and entries:
            placeholders = ",".join("?" for _ in entries)
            lineup_rows = conn.execute(
                f"SELECT season_entry_id FROM weekly_lineup WHERE bbbffl_round_id=? "
                f"AND season_entry_id IN ({placeholders}) AND effective_submission_version IS NOT NULL",
                (round_id, *entries),
            ).fetchall()
            if lineup_rows:
                artifacts.append(
                    {"type": "lineup_submission", "season_entry_ids": [row["season_entry_id"] for row in lineup_rows]}
                )
        if round_id:
            activation = conn.execute(
                "SELECT 1 FROM bbbffl_round_lockout_trigger_activation a "
                "JOIN bbbffl_round_lockout_trigger t ON t.trigger_id = a.trigger_id "
                "WHERE t.bbbffl_round_id=? LIMIT 1",
                (round_id,),
            ).fetchone()
            if activation:
                artifacts.append({"type": "position_lock", "bbbffl_round_id": round_id})
        if matchup_id is not None:
            locked_matchup = conn.execute(
                "SELECT effective_official_version FROM bbbffl_matchup WHERE matchup_id=?"
                + _for_update_suffix(self.database),
                (matchup_id,),
            ).fetchone()
            ruling = conn.execute(
                "SELECT 1 FROM bbbffl_matchup_slot_ruling WHERE matchup_id=? "
                "UNION SELECT 1 FROM bbbffl_matchup_interchange_ruling WHERE matchup_id=? "
                "UNION SELECT 1 FROM bbbffl_matchup_override WHERE matchup_id=?",
                (matchup_id, matchup_id, matchup_id),
            ).fetchone()
            if ruling:
                artifacts.append({"type": "ruling", "matchup_id": matchup_id})
            calculation = conn.execute(
                "SELECT 1 FROM bbbffl_matchup_calculation WHERE matchup_id=?", (matchup_id,)
            ).fetchone()
            if calculation:
                artifacts.append({"type": "calculation", "matchup_id": matchup_id})
            if locked_matchup is not None and locked_matchup["effective_official_version"] is not None:
                artifacts.append(
                    {
                        "type": "official_result",
                        "matchup_id": matchup_id,
                        "version": locked_matchup["effective_official_version"],
                    }
                )
        return artifacts


class _NoRound:
    state = "not_created"
