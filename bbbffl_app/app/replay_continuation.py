"""Issue #178: extend the verified 2026 Phase 1 replay season from its
intentional 9-round bootstrap boundary to the full 20-round BBBFFL
home-and-away season, without disturbing the completed Rounds 1-9 history
in any way.

## Why this cannot be an ordinary fixture edit

`app.fixtures.FixtureRepository` and the migration-0007/0009 database
triggers make a frozen fixture draw -- and the season length a frozen draw
fixes -- genuinely immutable at the schema level, not merely by
application-level convention:

- `season_fixture_matchup_frozen_insert`/`_update`/`_delete` (SQLite) and
  `enforce_fixture_draw_mutability()` (PostgreSQL) reject *any* INSERT,
  UPDATE, or DELETE against `season_fixture_matchup`/`season_fixture_number`
  once the owning draw is frozen.
- `fixture_draw_no_unfreeze` (both dialects) rejects *any* UPDATE at all to
  a frozen `season_fixture_draw` row, so a frozen draw can never even be
  transitioned back to `draft` to become mutable again.
- `season_length_frozen_update` (both dialects) rejects changing
  `bbbffl_season.regular_season_round_count` while a frozen draw exists for
  that season.
- `season_fixture_draw` also carries `uq_fixture_draw_season`, a UNIQUE
  constraint on `season_id` -- a season can never hold a second, independent
  fixture draw the way it might hold a second competition stream.

These are the correct invariants for ordinary operation: the 2026 replay's
first-half fixture draw must never again become editable through the normal
Admin/Scorer fixture surface. Issue #178 is the one narrow, pre-agreed
exception -- the first-half bootstrap deliberately configured the season as
a 9-round replay-harness boundary (see `app.replay_bootstrap.
FIRST_HALF_ROUNDS`), not the real 20-round 2026 BBBFFL season, and Phase 2
needs the *same* frozen draw extended with Rounds 10-20 rather than any kind
of rebuild.

This module is therefore a migration-style continuation, not a repository
method on `FixtureRepository`: inside one transaction, it drops exactly the
three enforcement triggers that would otherwise block the append, performs
the narrowly-scoped mutation (extend `regular_season_round_count`, bump the
draw's version, insert only the *new* Round 10-20 matchup rows), and
recreates the same triggers verbatim before committing. Nothing else in the
transaction is exempt from them, and no other caller gets a way to bypass
them: `continue_second_half_regular_season` is the only place these DROP/
CREATE TRIGGER statements exist outside the migrations themselves.

SQLite's pysqlite driver only auto-begins a real DBAPI transaction on a DML
statement (INSERT/UPDATE/DELETE), never on DDL (see
https://docs.sqlalchemy.org/en/20/dialects/sqlite.html
#serializable-isolation-savepoints-transactional-ddl) -- so if the DROP
TRIGGER calls were the very first statements issued, they would execute
outside any transaction and commit immediately regardless of what happens
afterward, wide open to both a concurrent writer observing the gap and a
later failure silently leaving the triggers dropped (both true of an
earlier version of this function -- see PR #179 review). This function
therefore issues one real, harmless DML statement (a self-referential
`UPDATE` on the season row's `updated_at`, touching no trigger-protected
column) *before* the first DROP TRIGGER, forcing pysqlite to open a real,
lock-holding transaction first. Every DDL and DML statement in this
function -- the drops, the mutation, and the recreates -- then participates
in that one real transaction on every dialect: a failure anywhere rolls
back atomically (no special recovery path is needed, or present), and no
other connection can observe the triggers as dropped at any point, exactly
like PostgreSQL's already-fully-transactional DDL.

## Preserving Rounds 1-9 exactly

Every Round 1-9 `season_fixture_matchup` row is left completely untouched --
this module only ever INSERTs new rows for Rounds 10-20, matching the exact
deterministic `fixture_matchup_id` convention `FixtureRepository.save_draft`
already uses (`uuid5(UUID(fixture_draw_id), f"round:{n}:match:{order}")`),
so a from-scratch 20-round draft of the same draw would derive identical
Round 10-20 identities. Before making any change, this module also
reconstructs the *existing* Round 1-9 pairings from the preserved fixture-
number assignments and `app.fixtures.fixture_number_rotation` and requires
them to match exactly -- a corrupted or hand-edited baseline is refused
before any mutation, not discovered afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4, uuid5

from app.audit import ActorContext, append_event, new_correlation_id
from app.db import DatabaseConnection, _for_update_suffix, transaction
from app.fixtures import ROTATION_VERSION, fixture_number_rotation

REPLAY_YEAR = 2026
SOURCE_ROUND_COUNT = 9
TARGET_ROUND_COUNT = 20
APPENDED_ROUND_NUMBERS = tuple(range(SOURCE_ROUND_COUNT + 1, TARGET_ROUND_COUNT + 1))

DEFAULT_ACTOR = ActorContext.anonymous_operator("replay_operator")
DEFAULT_REASON = (
    "2026 second-half replay: continued the verified Phase 1 season from its "
    "9-round replay-harness boundary to the full 20-round regular season"
)

SEASON_CONTINUED_ACTION = "replay.season.continued"
FIXTURE_DRAW_CONTINUED_ACTION = "fixture.draw.continued"


class ReplayContinuationError(ValueError):
    """The restored database is not in the expected verified Phase 1 (9-round)
    or already-continued (20-round) baseline; no mutation was attempted."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conflict(condition: bool, message: str) -> None:
    if condition:
        raise ReplayContinuationError(message)


@dataclass(frozen=True)
class _Baseline:
    season_id: str
    competition_id: str
    round_count: int
    draw_id: str
    draw_version: int
    entries_by_fixture_number: list[str]


def _round_labels_match(rows_by_sequence: dict[int, Any], sequences) -> bool:
    return all(
        rows_by_sequence[n]["round_key"] == f"round-{n}" and rows_by_sequence[n]["label"] == f"Round {n}"
        for n in sequences
    )


def _expected_pairings(
    entries_by_fixture_number: list[str], round_count: int
) -> dict[tuple[int, int], tuple[str, str]]:
    expected: dict[tuple[int, int], tuple[str, str]] = {}
    for round_number, pairings in enumerate(fixture_number_rotation(round_count), 1):
        for order, (home, away) in enumerate(pairings, 1):
            expected[(round_number, order)] = (
                entries_by_fixture_number[home - 1],
                entries_by_fixture_number[away - 1],
            )
    return expected


def _matchup_id(draw_id: str, round_number: int, order: int) -> str:
    """The exact deterministic convention `FixtureRepository.save_draft` uses,
    reproduced here so an appended Round 10-20 matchup carries the identity a
    from-scratch 20-round draft of this same draw would have derived."""
    return str(uuid5(UUID(draw_id), f"round:{round_number}:match:{order}"))


def _gather_facts(read, database: DatabaseConnection, *, locked: bool) -> _Baseline:
    """Read every fact this operation needs to reason about, and validate
    everything that must hold regardless of whether the database is at the
    pre-continuation (9-round) or already-continued (20-round) baseline.
    Raises `ReplayContinuationError` with a concrete diagnostic on the first
    deviation found -- this always runs before any mutation is attempted.
    """
    suffix = _for_update_suffix(database) if locked else ""

    seasons = read(f"SELECT * FROM bbbffl_season WHERE year=?{suffix}", (REPLAY_YEAR,)).fetchall()
    _conflict(len(seasons) != 1, f"expected exactly one {REPLAY_YEAR} season, found {len(seasons)}")
    season = seasons[0]
    season_id = season["season_id"]
    round_count = season["regular_season_round_count"] if "regular_season_round_count" in season.keys() else 20
    _conflict(
        round_count not in (SOURCE_ROUND_COUNT, TARGET_ROUND_COUNT),
        f"season {season_id} regular_season_round_count is {round_count}; expected {SOURCE_ROUND_COUNT} "
        f"(pre-continuation) or {TARGET_ROUND_COUNT} (already continued)",
    )

    competitions = read(
        "SELECT * FROM competition_stream WHERE season_id=? AND stream_type='ordinary'", (season_id,)
    ).fetchall()
    _conflict(len(competitions) != 1, f"expected exactly one ordinary competition stream, found {len(competitions)}")
    competition_id = competitions[0]["competition_id"]

    rounds = read("SELECT * FROM bbbffl_round WHERE competition_id=? ORDER BY sequence", (competition_id,)).fetchall()
    rows_by_sequence = {row["sequence"]: row for row in rounds}
    actual_sequences = set(rows_by_sequence)
    expected_sequences = set(range(1, round_count + 1))
    _conflict(
        actual_sequences != expected_sequences,
        f"expected logical rounds {sorted(expected_sequences)} to exist for a {round_count}-round season; "
        f"found {sorted(actual_sequences)}",
    )
    _conflict(
        not _round_labels_match(rows_by_sequence, expected_sequences),
        "one or more logical round definitions do not match the expected "
        "round-<n>/'Round <n>' round_key/label convention",
    )

    draw = read(f"SELECT * FROM season_fixture_draw WHERE season_id=?{suffix}", (season_id,)).fetchone()
    _conflict(draw is None, f"no fixture draw exists for season {season_id}; nothing to continue")
    _conflict(draw["state"] != "frozen", "fixture draw must be frozen before continuation is possible")

    fixture_numbers = read(
        "SELECT fixture_number, season_entry_id FROM season_fixture_number WHERE fixture_draw_id=? "
        "ORDER BY fixture_number",
        (draw["fixture_draw_id"],),
    ).fetchall()
    _conflict(
        len(fixture_numbers) != 10 or [row["fixture_number"] for row in fixture_numbers] != list(range(1, 11)),
        f"fixture-number assignments must cover exactly ten entries numbered 1-10; found {len(fixture_numbers)}",
    )
    entries_by_fixture_number = [row["season_entry_id"] for row in fixture_numbers]
    _conflict(
        len(set(entries_by_fixture_number)) != 10,
        "fixture-number assignments must reference ten distinct season entries",
    )

    matchup_rows = read(
        "SELECT bbbffl_round_number, matchup_order, home_season_entry_id, away_season_entry_id, fixture_matchup_id "
        "FROM season_fixture_matchup WHERE fixture_draw_id=? ORDER BY bbbffl_round_number, matchup_order",
        (draw["fixture_draw_id"],),
    ).fetchall()
    matchup_rounds: dict[int, int] = {}
    actual_pairings: dict[tuple[int, int], tuple[str, str]] = {}
    actual_matchup_ids: dict[tuple[int, int], str] = {}
    for row in matchup_rows:
        key = (row["bbbffl_round_number"], row["matchup_order"])
        matchup_rounds[row["bbbffl_round_number"]] = matchup_rounds.get(row["bbbffl_round_number"], 0) + 1
        actual_pairings[key] = (row["home_season_entry_id"], row["away_season_entry_id"])
        actual_matchup_ids[key] = row["fixture_matchup_id"]
    _conflict(
        set(matchup_rounds) != expected_sequences or any(matchup_rounds[n] != 5 for n in expected_sequences),
        f"frozen draw must contain exactly five matchups for each of rounds 1-{round_count} and none beyond; "
        f"found {dict(sorted(matchup_rounds.items()))}",
    )
    expected_pairings = _expected_pairings(entries_by_fixture_number, round_count)
    _conflict(
        actual_pairings != expected_pairings,
        f"persisted Round 1-{round_count} pairings do not match {ROTATION_VERSION} "
        "fixture_number_rotation for the preserved fixture-number assignments; refusing to extend a fixture "
        "that does not match its own documented rotation",
    )
    _conflict(
        any(actual_matchup_ids[key] != _matchup_id(draw["fixture_draw_id"], *key) for key in actual_pairings),
        "persisted fixture matchup identities do not match the expected deterministic uuid5 convention",
    )

    lifecycle_rows = read(
        "SELECT br.sequence, bl.state FROM bbbffl_round br "
        "LEFT JOIN bbbffl_round_lifecycle bl ON bl.bbbffl_round_id = br.bbbffl_round_id "
        "WHERE br.competition_id=? AND br.sequence <= ?",
        (competition_id, SOURCE_ROUND_COUNT),
    ).fetchall()
    _conflict(len(lifecycle_rows) != SOURCE_ROUND_COUNT, "expected a lifecycle row for every Round 1-9")
    not_final = sorted(row["sequence"] for row in lifecycle_rows if row["state"] != "final")
    _conflict(
        bool(not_final),
        f"Round 1-9 must all be final before continuation; round(s) {not_final} are not",
    )

    if round_count == SOURCE_ROUND_COUNT:
        midseason = read("SELECT 1 FROM midseason_draft WHERE season_id=?", (season_id,)).fetchone()
        _conflict(
            midseason is not None,
            "a mid-season draft already exists for this season; continuation expects Round 10 not yet reached",
        )

    return _Baseline(
        season_id=season_id,
        competition_id=competition_id,
        round_count=round_count,
        draw_id=draw["fixture_draw_id"],
        draw_version=draw["version"],
        entries_by_fixture_number=entries_by_fixture_number,
    )


def describe_second_half_continuation(database: DatabaseConnection) -> dict:
    """Read-only status report: never mutates, never takes a row lock.

    Safe to call at any time (including production) to check whether the
    2026 season is at the pre-continuation baseline, already continued, or
    in an unexpected/partially-modified state -- the same classification
    `continue_second_half_regular_season` uses, without acting on it.
    """
    try:
        baseline = _gather_facts(database.execute, database, locked=False)
    except ReplayContinuationError as exc:
        return {"ready": False, "already_continued": False, "diagnostic": str(exc)}
    already_continued = baseline.round_count == TARGET_ROUND_COUNT
    return {
        "ready": True,
        "already_continued": already_continued,
        "season_id": baseline.season_id,
        "regular_season_round_count": baseline.round_count,
        "fixture_draw_id": baseline.draw_id,
        "fixture_draw_version": baseline.draw_version,
        "diagnostic": None,
    }


# -- Dialect-specific enforcement-trigger DDL --------------------------------
#
# These strings must stay byte-identical to the CREATE TRIGGER/FUNCTION
# statements in migrations/versions/0007_fixture_draw.py and
# 0009_season_length.py -- they are dropped and recreated verbatim, never
# altered, so the schema this operation leaves behind is indistinguishable
# from one that never had them dropped at all.

_SQLITE_TRIGGERS = {
    "season_fixture_matchup_frozen_insert": (
        "season_fixture_matchup",
        """
        CREATE TRIGGER season_fixture_matchup_frozen_insert BEFORE INSERT ON season_fixture_matchup
        WHEN (SELECT state FROM season_fixture_draw WHERE fixture_draw_id=NEW.fixture_draw_id)='frozen'
        BEGIN SELECT RAISE(ABORT, 'frozen fixture draw is immutable'); END
        """,
    ),
    "fixture_draw_no_unfreeze": (
        "season_fixture_draw",
        """
        CREATE TRIGGER fixture_draw_no_unfreeze BEFORE UPDATE ON season_fixture_draw
        WHEN OLD.state='frozen'
        BEGIN SELECT RAISE(ABORT, 'frozen fixture draw is immutable'); END
        """,
    ),
    "season_length_frozen_update": (
        "bbbffl_season",
        """
        CREATE TRIGGER season_length_frozen_update
        BEFORE UPDATE OF regular_season_round_count ON bbbffl_season
        WHEN OLD.regular_season_round_count <> NEW.regular_season_round_count
          AND EXISTS (
            SELECT 1 FROM season_fixture_draw
            WHERE season_id=OLD.season_id AND state='frozen'
          )
        BEGIN SELECT RAISE(ABORT, 'frozen fixture draw fixes season length'); END
        """,
    ),
}

_POSTGRESQL_TRIGGERS = {
    "season_fixture_matchup_mutable": (
        "season_fixture_matchup",
        "CREATE TRIGGER season_fixture_matchup_mutable BEFORE INSERT OR UPDATE OR DELETE ON season_fixture_matchup "
        "FOR EACH ROW EXECUTE FUNCTION enforce_fixture_draw_mutability()",
    ),
    "fixture_draw_no_unfreeze": (
        "season_fixture_draw",
        "CREATE TRIGGER fixture_draw_no_unfreeze BEFORE UPDATE ON season_fixture_draw "
        "FOR EACH ROW EXECUTE FUNCTION enforce_fixture_draw_state()",
    ),
    "season_length_frozen_update": (
        "bbbffl_season",
        "CREATE TRIGGER season_length_frozen_update BEFORE UPDATE OF regular_season_round_count ON bbbffl_season "
        "FOR EACH ROW EXECUTE FUNCTION enforce_frozen_fixture_season_length()",
    ),
}


def _triggers_for(dialect: str) -> dict:
    if dialect == "postgresql":
        return _POSTGRESQL_TRIGGERS
    if dialect == "sqlite":
        return _SQLITE_TRIGGERS
    raise ReplayContinuationError(f"unsupported database dialect {dialect!r} for replay continuation")


def _drop_frozen_fixture_triggers(conn, dialect: str) -> None:
    for name, (table, _ddl) in _triggers_for(dialect).items():
        if dialect == "postgresql":
            conn.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
        else:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def _recreate_frozen_fixture_triggers(conn, dialect: str) -> None:
    for _name, (_table, ddl) in _triggers_for(dialect).items():
        conn.execute(ddl)


def continue_second_half_regular_season(
    database: DatabaseConnection,
    *,
    actor: ActorContext = DEFAULT_ACTOR,
    reason: str | None = None,
) -> dict:
    """Extend the verified 2026 Phase 1 season from 9 to 20 regular-season
    rounds: preserve Rounds 1-9 exactly, append frozen Round 10-20 fixture
    matchups from the preserved fixture-number assignments and
    `fixture_number_rotation`, and create the missing logical Round 10-20
    definitions. Leaves Round 10 unopened -- no `bbbffl_round_lifecycle` row
    is created here; the ordinary Round Preflight workflow does that.

    Fails closed (`ReplayContinuationError`, no mutation) if the database is
    not at the expected 9-round baseline or a fully, correctly continued
    20-round state. Idempotent: a database already correctly continued
    returns success without creating any duplicate rows.
    """
    reason = reason or DEFAULT_REASON
    with transaction(database) as conn:
        baseline = _gather_facts(conn.execute, database, locked=True)

        if baseline.round_count == TARGET_ROUND_COUNT:
            return {
                "already_continued": True,
                "mutated": False,
                "season_id": baseline.season_id,
                "fixture_draw_id": baseline.draw_id,
                "regular_season_round_count": TARGET_ROUND_COUNT,
                "fixture_draw_version": baseline.draw_version,
                "preserved_rounds": [1, SOURCE_ROUND_COUNT],
                "appended_rounds": [SOURCE_ROUND_COUNT + 1, TARGET_ROUND_COUNT],
            }

        dialect = database.engine.dialect.name
        now = _now()
        correlation_id = new_correlation_id()

        if dialect == "sqlite":
            # Force pysqlite to open a real, lock-holding transaction before
            # any DDL -- see this module's docstring and PR #179 review. A
            # genuine DML statement (not a no-op SELECT), but one that
            # touches no trigger-protected column, so it is safe to issue
            # before the frozen-fixture triggers are dropped below.
            conn.execute(
                "UPDATE bbbffl_season SET updated_at=updated_at WHERE season_id=?",
                (baseline.season_id,),
            )

        _drop_frozen_fixture_triggers(conn, dialect)

        conn.execute(
            "UPDATE bbbffl_season SET regular_season_round_count=?, updated_at=? WHERE season_id=?",
            (TARGET_ROUND_COUNT, now, baseline.season_id),
        )
        new_draw_version = baseline.draw_version + 1
        conn.execute(
            "UPDATE season_fixture_draw SET version=?, updated_at=? WHERE fixture_draw_id=?",
            (new_draw_version, now, baseline.draw_id),
        )

        rotation = fixture_number_rotation(TARGET_ROUND_COUNT)
        entries = baseline.entries_by_fixture_number
        for round_number in APPENDED_ROUND_NUMBERS:
            for order, (home, away) in enumerate(rotation[round_number - 1], 1):
                conn.execute(
                    "INSERT INTO season_fixture_matchup VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        _matchup_id(baseline.draw_id, round_number, order),
                        baseline.draw_id,
                        baseline.season_id,
                        round_number,
                        order,
                        entries[home - 1],
                        entries[away - 1],
                    ),
                )

        created_round_keys = []
        for number in APPENDED_ROUND_NUMBERS:
            round_id = str(uuid4())
            round_key, label = f"round-{number}", f"Round {number}"
            conn.execute(
                "INSERT INTO bbbffl_round VALUES (?, ?, ?, ?, ?, ?)",
                (round_id, baseline.competition_id, round_key, label, number, now),
            )
            created_round_keys.append(round_key)

        _recreate_frozen_fixture_triggers(conn, dialect)

        append_event(
            conn,
            actor=actor,
            action=SEASON_CONTINUED_ACTION,
            entity_type="season",
            entity_id=baseline.season_id,
            entity_version=str(TARGET_ROUND_COUNT),
            correlation_id=correlation_id,
            reason=reason,
            before_state={"regular_season_round_count": SOURCE_ROUND_COUNT},
            after_state={"regular_season_round_count": TARGET_ROUND_COUNT},
            payload={
                "fixture_draw_id": baseline.draw_id,
                "fixture_draw_version": new_draw_version,
                "preserved_rounds": [1, SOURCE_ROUND_COUNT],
                "appended_rounds": [SOURCE_ROUND_COUNT + 1, TARGET_ROUND_COUNT],
                "logical_rounds_created": created_round_keys,
                "rotation_version": ROTATION_VERSION,
            },
        )
        append_event(
            conn,
            actor=actor,
            action=FIXTURE_DRAW_CONTINUED_ACTION,
            entity_type="fixture_draw",
            entity_id=baseline.draw_id,
            entity_version=str(new_draw_version),
            correlation_id=correlation_id,
            reason=reason,
            before_state={"version": baseline.draw_version, "rounds": SOURCE_ROUND_COUNT},
            after_state={"version": new_draw_version, "rounds": TARGET_ROUND_COUNT},
            payload={
                "rotation_version": ROTATION_VERSION,
                "appended_round_numbers": list(APPENDED_ROUND_NUMBERS),
            },
        )

        return {
            "already_continued": False,
            "mutated": True,
            "season_id": baseline.season_id,
            "fixture_draw_id": baseline.draw_id,
            "regular_season_round_count": TARGET_ROUND_COUNT,
            "fixture_draw_version": new_draw_version,
            "preserved_rounds": [1, SOURCE_ROUND_COUNT],
            "appended_rounds": [SOURCE_ROUND_COUNT + 1, TARGET_ROUND_COUNT],
            "logical_rounds_created": created_round_keys,
        }
