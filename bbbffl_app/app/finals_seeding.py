"""Issue #187: an explicit, replay-only historical finals-seeding snapshot
for the 2026 second-half replay.

## Why this exists

The 2026 second-half replay reconstructs Rounds 10-20 against the current
scoring engine and reconstructed authoritative AFL evidence. Two historical
Scorer-error outcomes (Round 12 Evil Absolutes v Running Hots, Round 13
Motherruckers v Evil Absolutes -- see `HISTORICAL_DISCREPANCY_RATIONALE`
below) mean the historical 2026 competition's finals order differs from the
mathematically reconstructed Round 20 ladder for three teams (Running Hots,
Evil Absolutes, Motherruckers). Repairing those two match results merely to
make the mathematical ladder agree with the old spreadsheet is explicitly
out of scope (issue #169/#187): the mathematical reconstruction and the
historical competition outcome are deliberately preserved as two separate
pieces of evidence.

This module is the narrow bridge between them: a single, immutable,
audited snapshot recording the historical finals-seeding order the 2026
replay's finals/SuperScore phase (issue #170) must consume, without ever
mutating the mathematical Round 20 ladder (`app.ladder`) that produced a
different order.

## What this deliberately is not

- **Not a generic ladder editor.** `FinalsSeedingRepository.apply` accepts
  no caller-supplied order. The only order it can ever write is the fixed
  historical order in `HISTORICAL_FINALS_SEED_TEAM_NAMES`, resolved to this
  season's own `season_entry_id` values by current display name. There is
  no override method, unlike `app.midseason_draft.override_draft_order` --
  a fixed historical fact has nothing left for an audited correction to
  legitimately rewrite (see the module docstring on immutability in
  `migrations/versions/0028_finals_seeding.py`).
- **Not a live/2027 capability.** `_require_replay_context` refuses outright
  (`FinalsSeedingContextError`, no mutation) unless the season's `year` is
  exactly `REPLAY_YEAR` (2026) -- a 2027 (or any other) season can never
  satisfy this gate, structurally, not merely by convention. There is no
  season/environment flag to opt a live season into this mechanism.
- **Not a coach-facing operation.** There is no HTTP route here at all --
  only this repository and the Docker-friendly operator CLI
  (`scripts/finals_seeding_2026.py`), matching `app.replay_continuation`'s
  own CLI-only precedent for issue #178's narrow replay-only mechanism.

## Consumption

`resolve_finals_seed_order` is the one integration seam a future finals/
SuperScore implementation (issue #170) should call for its seed order. It
returns this snapshot's historical order when one exists for the requested
season/competition; otherwise it falls back to the ordinary mathematical
ladder order unchanged. Because a snapshot can only ever be created for the
2026 replay season, every other season -- 2027 and beyond included --
always takes the second, ordinary path with no special-casing required at
the call site.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.audit import ActorContext, append_event
from app.db import _for_update_suffix, transaction
from app.ladder import LadderRepository

REPLAY_YEAR = 2026
REQUIRED_THROUGH_ROUND = 20

FINALS_SEEDING_SNAPSHOT_CREATED = "finals_seeding.snapshot.created"
ENTITY_TYPE_FINALS_SEEDING_SNAPSHOT = "finals_seeding.snapshot"

DEFAULT_ACTOR = ActorContext.anonymous_operator("replay_operator")

# The ten 2026 BBBFFL season entries, by current display name, in the exact
# order the historical 2026 competition used to seed finals after Round 20
# (issue #187). This is the *only* order `apply_finals_seeding_snapshot` can
# ever write -- there is no caller-supplied-order code path. Some historical
# records spell the ninth team "Pommie Rules" rather than "Pommy Rules";
# both spellings resolve to the same fixed seed position.
HISTORICAL_FINALS_SEED_TEAM_NAMES: tuple[tuple[str, ...], ...] = (
    ("Running Hots",),
    ("Bridesmaids",),
    ("JHAS",),
    ("Wolverines",),
    ("Evil Absolutes",),
    ("The Crabs",),
    ("One Percenters",),
    ("Motherruckers",),
    ("Pommy Rules", "Pommie Rules"),
    ("The Plague",),
)

# Historical W/L/competition-points for the three teams whose historical
# finals placement differs materially from the mathematical Round 20
# ladder -- purely informational (preview/audit display), never a value
# this module writes into `app.ladder`'s official-result inputs.
KNOWN_HISTORICAL_STATS_BY_TEAM_NAME: dict[str, dict[str, int]] = {
    "Running Hots": {"wins": 13, "losses": 7, "competition_points": 52},
    "Evil Absolutes": {"wins": 10, "losses": 10, "competition_points": 40},
    "Motherruckers": {"wins": 9, "losses": 11, "competition_points": 36},
}

HISTORICAL_DISCREPANCY_RATIONALE = (
    "The 2026 historical finals order differs from the mathematically reconstructed Round 20 ladder because of "
    "two known historical Scorer-error outcomes that changed match winners. "
    "Round 12 -- Evil Absolutes v Running Hots: the reconstructed/current application recorded Evil Absolutes 161, "
    "Running Hots 149 (application winner: Evil Absolutes); the historical 2026 competition recorded Evil "
    "Absolutes 159, Running Hots 163 (historical winner: Running Hots). "
    "Round 13 -- Motherruckers v Evil Absolutes: the reconstructed/current application recorded Motherruckers 176, "
    "Evil Absolutes 182 (application winner: Evil Absolutes); the historical 2026 competition recorded "
    "Motherruckers 182, Evil Absolutes 181 (historical winner: Motherruckers). "
    "Together these two outcome differences exactly explain the material W/L/competition-points divergence for "
    "Running Hots (12-8/48 mathematically, 13-7/52 historically), Evil Absolutes (12-8/48 mathematically, "
    "10-10/40 historically) and Motherruckers (8-12/32 mathematically, 9-11/36 historically). No other historical "
    "score/PF/PA discrepancy is reconciled or reconstructed by this snapshot."
)


class FinalsSeedingError(ValueError):
    """Base class for every refusal this module raises. No mutation is ever
    attempted once one of these is raised."""


class FinalsSeedingContextError(FinalsSeedingError):
    """The season/competition/round-completion context is not the intended
    2026 historical replay context this mechanism is narrowly scoped to."""


class FinalsSeedingResolutionError(FinalsSeedingError):
    """The fixed historical team names could not be resolved to exactly this
    season's ten entries (missing, duplicate, or ambiguous)."""


class FinalsSeedingConflictError(FinalsSeedingError):
    """A finals-seeding snapshot already exists for this season and the
    freshly resolved historical seed (or competition_id) no longer matches
    it -- refusing to silently replace an existing immutable snapshot."""


def _id() -> str:
    return str(uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class MathematicalLadderRow:
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
class FinalsSeedRow:
    seed_position: int
    season_entry_id: str


@dataclass(frozen=True)
class FinalsSeedingSnapshot:
    snapshot_id: str
    season_id: str
    competition_id: str
    through_round: int
    created_at: str
    mathematical_rows: tuple[MathematicalLadderRow, ...]
    seed_rows: tuple[FinalsSeedRow, ...]


def _require_replay_context(read, database, season_id, competition_id, *, locked):
    """Read (and validate) every fact `FinalsSeedingRepository.apply` and
    `FinalsSeedingRepository.preview` must agree on: raises
    `FinalsSeedingContextError` on the first deviation found, before
    anything is written. Mirrors
    `app.replay_continuation._gather_facts`'s read/validate-before-write
    shape for the same reason: a caller must get the identical diagnostic
    whether it is only previewing or about to mutate."""
    suffix = _for_update_suffix(database) if locked else ""
    season = read(f"SELECT * FROM bbbffl_season WHERE season_id=?{suffix}", (season_id,)).fetchone()
    if season is None:
        raise KeyError(season_id)
    if season["year"] != REPLAY_YEAR:
        raise FinalsSeedingContextError(
            f"a finals-seeding snapshot is only permitted for the {REPLAY_YEAR} historical replay season; "
            f"season {season_id} is year {season['year']}"
        )
    competition = read("SELECT * FROM competition_stream WHERE competition_id=?", (competition_id,)).fetchone()
    if competition is None or competition["season_id"] != season_id or competition["stream_type"] != "ordinary":
        raise FinalsSeedingContextError("competition_id must name this season's own ordinary home-and-away competition")
    round_count = season["regular_season_round_count"] if "regular_season_round_count" in season.keys() else 20
    if round_count != REQUIRED_THROUGH_ROUND:
        raise FinalsSeedingContextError(
            f"season must be configured as a {REQUIRED_THROUGH_ROUND}-round home-and-away season to take a "
            f"finals-seeding snapshot; found {round_count}"
        )
    lifecycle_rows = read(
        "SELECT br.sequence, bl.state FROM bbbffl_round br "
        "LEFT JOIN bbbffl_round_lifecycle bl ON bl.bbbffl_round_id = br.bbbffl_round_id "
        "WHERE br.competition_id=? AND br.sequence<=?",
        (competition_id, REQUIRED_THROUGH_ROUND),
    ).fetchall()
    present = {row["sequence"] for row in lifecycle_rows}
    missing = sorted(set(range(1, REQUIRED_THROUGH_ROUND + 1)) - present)
    not_final = sorted(row["sequence"] for row in lifecycle_rows if row["state"] != "final")
    if missing or not_final:
        raise FinalsSeedingContextError(
            f"every round through {REQUIRED_THROUGH_ROUND} must be final before a finals-seeding snapshot can be "
            f"created (missing: {missing}, not yet final: {not_final})"
        )
    return season


def _resolve_historical_seed(read, season_id) -> list[tuple[int, str]]:
    """Resolve `HISTORICAL_FINALS_SEED_TEAM_NAMES` to this season's actual
    `season_entry_id` values from current display names only. Fails closed
    (`FinalsSeedingResolutionError`) if the season does not have exactly ten
    entries, if any required position has no matching current team name, or
    if two positions would resolve to the same entry -- there is no
    fallback to a partially-resolved or best-guess order."""
    rows = read(
        "SELECT e.season_entry_id, n.team_name FROM season_entry e "
        "JOIN season_entry_team_name_history n ON n.season_entry_id = e.season_entry_id AND n.ended_at IS NULL "
        "WHERE e.season_id=?",
        (season_id,),
    ).fetchall()
    if len(rows) != len(HISTORICAL_FINALS_SEED_TEAM_NAMES):
        raise FinalsSeedingResolutionError(
            f"season {season_id} has {len(rows)} current season entries; the historical 2026 finals seed "
            f"requires exactly {len(HISTORICAL_FINALS_SEED_TEAM_NAMES)}"
        )
    by_name: dict[str, str] = {}
    for row in rows:
        key = row["team_name"].strip().casefold()
        if key in by_name:
            raise FinalsSeedingResolutionError(f"more than one current season entry is named {row['team_name']!r}")
        by_name[key] = row["season_entry_id"]

    resolved: list[tuple[int, str]] = []
    used: set[str] = set()
    for position, aliases in enumerate(HISTORICAL_FINALS_SEED_TEAM_NAMES, 1):
        match = next(
            (by_name[alias.strip().casefold()] for alias in aliases if alias.strip().casefold() in by_name), None
        )
        if match is None:
            raise FinalsSeedingResolutionError(
                f"no current season entry is named any of {list(aliases)!r} (historical seed position {position})"
            )
        if match in used:
            raise FinalsSeedingResolutionError(
                f"season entry {match} would occupy more than one historical finals-seeding position"
            )
        used.add(match)
        resolved.append((position, match))
    return resolved


class FinalsSeedingRepository:
    def __init__(self, database):
        self.database = database

    # -- Reads ------------------------------------------------------------

    def get_snapshot(self, season_id: str) -> FinalsSeedingSnapshot | None:
        snapshot = self.database.execute(
            "SELECT * FROM finals_seeding_snapshot WHERE season_id=?", (season_id,)
        ).fetchone()
        return self._load(snapshot) if snapshot is not None else None

    def _load(self, snapshot) -> FinalsSeedingSnapshot:
        math_rows = self.database.execute(
            "SELECT * FROM finals_seeding_snapshot_mathematical_row WHERE snapshot_id=? ORDER BY rank, season_entry_id",
            (snapshot["snapshot_id"],),
        ).fetchall()
        seed_rows = self.database.execute(
            "SELECT * FROM finals_seeding_snapshot_seed_row WHERE snapshot_id=? ORDER BY seed_position",
            (snapshot["snapshot_id"],),
        ).fetchall()
        return FinalsSeedingSnapshot(
            snapshot_id=snapshot["snapshot_id"],
            season_id=snapshot["season_id"],
            competition_id=snapshot["competition_id"],
            through_round=snapshot["through_round"],
            created_at=snapshot["created_at"],
            mathematical_rows=tuple(
                MathematicalLadderRow(
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
                for row in math_rows
            ),
            seed_rows=tuple(FinalsSeedRow(row["seed_position"], row["season_entry_id"]) for row in seed_rows),
        )

    def preview(self, season_id: str, competition_id: str) -> dict:
        """Read-only status/preview: never mutates, never takes a row lock.
        Safe to call at any time. Reports the same context/resolution checks
        `apply` performs, without acting on them."""
        existing = self.get_snapshot(season_id)
        report: dict = {
            "season_id": season_id,
            "competition_id": competition_id,
            "replay_context_ready": False,
            "diagnostic": None,
            "mathematical_order": None,
            "historical_seed_order": None,
            "material_differences": None,
            "historical_rationale": HISTORICAL_DISCREPANCY_RATIONALE,
            "snapshot_exists": existing is not None,
            "existing_snapshot_id": existing.snapshot_id if existing is not None else None,
            "apply_permitted": False,
        }
        try:
            _require_replay_context(self.database.execute, self.database, season_id, competition_id, locked=False)
        except (FinalsSeedingContextError, KeyError) as exc:
            report["diagnostic"] = str(exc)
            return report

        ladder = LadderRepository(self.database).snapshot(competition_id, REQUIRED_THROUGH_ROUND)
        math_by_entry = {row.season_entry_id: row for row in ladder.rows}
        report["mathematical_order"] = [
            {
                "season_entry_id": row.season_entry_id,
                "rank": row.rank,
                "wins": row.wins,
                "losses": row.losses,
                "competition_points": row.competition_points,
            }
            for row in ladder.rows
        ]

        try:
            resolved_seed = _resolve_historical_seed(self.database.execute, season_id)
        except FinalsSeedingResolutionError as exc:
            report["diagnostic"] = str(exc)
            return report

        name_by_entry = {
            entry_id: aliases[0]
            for aliases, entry_id in zip(HISTORICAL_FINALS_SEED_TEAM_NAMES, (e for _, e in resolved_seed))
        }
        report["historical_seed_order"] = [
            {"seed_position": position, "season_entry_id": entry_id, "team_name": name_by_entry[entry_id]}
            for position, entry_id in resolved_seed
        ]
        entry_by_name = {name: entry_id for entry_id, name in name_by_entry.items()}
        report["material_differences"] = [
            {
                "team_name": team_name,
                "season_entry_id": entry_by_name[team_name],
                "mathematical": {
                    "wins": math_by_entry[entry_by_name[team_name]].wins,
                    "losses": math_by_entry[entry_by_name[team_name]].losses,
                    "competition_points": math_by_entry[entry_by_name[team_name]].competition_points,
                },
                "historical": historical_stats,
            }
            for team_name, historical_stats in KNOWN_HISTORICAL_STATS_BY_TEAM_NAME.items()
        ]
        report["replay_context_ready"] = True
        if existing is None:
            report["apply_permitted"] = True
        else:
            existing_pairs = [(row.seed_position, row.season_entry_id) for row in existing.seed_rows]
            report["apply_permitted"] = existing_pairs == resolved_seed and existing.competition_id == competition_id
            if not report["apply_permitted"]:
                report["diagnostic"] = (
                    "a finals-seeding snapshot already exists for this season and no longer matches the freshly "
                    "resolved historical seed/competition -- apply would fail closed"
                )
        return report

    def apply(self, season_id: str, competition_id: str, *, actor: ActorContext = DEFAULT_ACTOR, reason: str) -> dict:
        """Create (or idempotently confirm) the one finals-seeding snapshot
        for this season. Requires an explicit, substantive `reason`; accepts
        no caller-supplied seed order -- the only order ever written is the
        fixed historical order in `HISTORICAL_FINALS_SEED_TEAM_NAMES`.

        Fails closed (`FinalsSeedingContextError`/`FinalsSeedingResolutionError`
        /`FinalsSeedingConflictError`, no mutation) unless the season is the
        2026 replay season with Round 20 complete and the ten historical team
        names all resolve unambiguously. Idempotent: calling this again with
        an unchanged resolution returns the existing snapshot without writing
        a duplicate row or a second audit event.
        """
        if not reason or not reason.strip():
            raise FinalsSeedingError("creating a finals-seeding snapshot requires an explicit, substantive reason")

        with transaction(self.database) as conn:
            _require_replay_context(conn.execute, self.database, season_id, competition_id, locked=True)
            resolved_seed = _resolve_historical_seed(conn.execute, season_id)

            existing = conn.execute(
                "SELECT * FROM finals_seeding_snapshot WHERE season_id=?" + _for_update_suffix(self.database),
                (season_id,),
            ).fetchone()
            if existing is not None:
                if existing["competition_id"] != competition_id:
                    raise FinalsSeedingConflictError(
                        "a finals-seeding snapshot already exists for this season against a different "
                        "competition_id -- refusing to silently replace it"
                    )
                existing_seed = conn.execute(
                    "SELECT seed_position, season_entry_id FROM finals_seeding_snapshot_seed_row "
                    "WHERE snapshot_id=? ORDER BY seed_position",
                    (existing["snapshot_id"],),
                ).fetchall()
                existing_pairs = [(row["seed_position"], row["season_entry_id"]) for row in existing_seed]
                if existing_pairs != resolved_seed:
                    raise FinalsSeedingConflictError(
                        "a finals-seeding snapshot already exists for this season with a different resolved "
                        "historical seed order -- refusing to silently replace an existing immutable snapshot"
                    )
                return {
                    "created": False,
                    "snapshot_id": existing["snapshot_id"],
                    "season_id": season_id,
                    "competition_id": competition_id,
                    "seed_positions": resolved_seed,
                    "audit_event_id": None,
                }

            ladder = LadderRepository(self.database).snapshot(competition_id, REQUIRED_THROUGH_ROUND)
            snapshot_id = _id()
            now = _now()
            conn.execute(
                "INSERT INTO finals_seeding_snapshot VALUES (?, ?, ?, ?, ?)",
                (snapshot_id, season_id, competition_id, REQUIRED_THROUGH_ROUND, now),
            )
            for row in ladder.rows:
                conn.execute(
                    "INSERT INTO finals_seeding_snapshot_mathematical_row VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    "INSERT INTO finals_seeding_snapshot_reference VALUES (?, ?, ?, ?)",
                    (_id(), snapshot_id, reference.matchup_id, reference.official_version),
                )
            for position, entry_id in resolved_seed:
                conn.execute(
                    "INSERT INTO finals_seeding_snapshot_seed_row VALUES (?, ?, ?, ?)",
                    (_id(), snapshot_id, position, entry_id),
                )

            event = append_event(
                conn,
                actor=actor,
                action=FINALS_SEEDING_SNAPSHOT_CREATED,
                entity_type=ENTITY_TYPE_FINALS_SEEDING_SNAPSHOT,
                entity_id=snapshot_id,
                reason=reason,
                before_state={
                    "mathematical_order": [
                        {
                            "season_entry_id": row.season_entry_id,
                            "rank": row.rank,
                            "wins": row.wins,
                            "losses": row.losses,
                            "competition_points": row.competition_points,
                        }
                        for row in ladder.rows
                    ],
                },
                after_state={
                    "historical_seed_order": [
                        {"seed_position": position, "season_entry_id": entry_id} for position, entry_id in resolved_seed
                    ],
                },
                payload={
                    "season_id": season_id,
                    "competition_id": competition_id,
                    "through_round": REQUIRED_THROUGH_ROUND,
                    "historical_rationale": HISTORICAL_DISCREPANCY_RATIONALE,
                },
            )
            return {
                "created": True,
                "snapshot_id": snapshot_id,
                "season_id": season_id,
                "competition_id": competition_id,
                "seed_positions": resolved_seed,
                "audit_event_id": event.event_id,
            }


def resolve_finals_seed_order(
    database, season_id: str, competition_id: str, through_round: int = REQUIRED_THROUGH_ROUND
) -> tuple[str, ...]:
    """The one integration seam a future finals/SuperScore progression
    (issue #170) should call for its seed order.

    Returns this season's audited historical finals-seeding snapshot's order
    when one exists and is scoped to `competition_id`; otherwise falls back
    unchanged to the ordinary mathematical ladder order
    (`app.ladder.LadderRepository.snapshot(...).rows`, already sorted by
    competition points, percentage, then points for -- see `app.ladder`'s
    module docstring). A snapshot can only ever exist for the 2026 replay
    season (see `_require_replay_context`), so every other season -- 2027
    and beyond included -- always takes this second, ordinary path with no
    special-casing required at the call site.
    """
    snapshot = FinalsSeedingRepository(database).get_snapshot(season_id)
    if snapshot is not None and snapshot.competition_id == competition_id:
        return tuple(row.season_entry_id for row in snapshot.seed_rows)
    ladder = LadderRepository(database).snapshot(competition_id, through_round)
    return tuple(row.season_entry_id for row in ladder.rows)
