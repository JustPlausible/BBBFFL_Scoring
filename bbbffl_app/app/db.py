"""SQLite storage for scorer decisions.

Scorer decisions (DNP, interchange assignment, direct score overrides, and
matchup finalisation) are kept entirely separate from the coach-declared
team configuration (see teams.py) and are never used to mutate AFL source
statistics -- they only affect the calculated BBBFFL score. This store must
survive a container restart, so the database file lives on a mounted volume
(see Dockerfile / README).

Every row is scoped by `competition_key`. This is what lets a single coach
have both a Grand Final entry and a SuperScore entry in the same round
without their DNP/interchange/override/finalisation state ever colliding --
even if the two competitions happened to reuse the same team_key, they are
still distinct rows because the primary key includes competition_key. The
Grand Final continues to use the fixed key "grand_final" (the historical
default for every method below), so its behaviour is unchanged. SuperScore
uses a key derived from season/round (see superscore.py), which also means
each SuperScore round's decisions and result remain distinct and retained
independently for future historical reporting -- no separate SuperScore
table is required.
"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

GRAND_FINAL_COMPETITION_KEY = "grand_final"

SCHEMA = """
CREATE TABLE IF NOT EXISTS slot_dnp (
    competition_key TEXT NOT NULL DEFAULT 'grand_final',
    team_key TEXT NOT NULL,
    slot TEXT NOT NULL,
    dnp INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (competition_key, team_key, slot)
);

CREATE TABLE IF NOT EXISTS interchange_assignment (
    competition_key TEXT NOT NULL DEFAULT 'grand_final',
    team_key TEXT NOT NULL,
    target_position TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (competition_key, team_key)
);

CREATE TABLE IF NOT EXISTS score_override (
    competition_key TEXT NOT NULL DEFAULT 'grand_final',
    team_key TEXT NOT NULL,
    position TEXT NOT NULL,
    override_score REAL,
    reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (competition_key, team_key, position)
);

CREATE TABLE IF NOT EXISTS matchup_state (
    competition_key TEXT PRIMARY KEY,
    finalized INTEGER NOT NULL DEFAULT 0,
    finalized_at TEXT,
    finalized_note TEXT,
    finalized_snapshot TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(database_path: str) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI dispatches sync route handlers onto a
    # threadpool, so requests may not share the thread this connection was
    # opened on. SQLite still serialises access internally, which is
    # sufficient for this prototype's single-scorer write volume.
    conn = sqlite3.connect(database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_legacy_schema(conn: sqlite3.Connection) -> None:
    """Upgrades a database created before competition_key existed, so a
    scorer decision recorded before this change is preserved under the
    Grand Final's competition_key rather than lost or (worse) mixed up with
    a later SuperScore round that happens to reuse a team_key.

    Each legacy per-team-key table is rebuilt with competition_key folded
    into its primary key; matchup_state's old `id = 1` singleton row becomes
    the "grand_final" row of a table keyed by competition_key instead.
    """
    if _table_exists(conn, "slot_dnp") and "competition_key" not in _table_columns(conn, "slot_dnp"):
        conn.executescript(
            """
            ALTER TABLE slot_dnp RENAME TO slot_dnp_legacy;
            CREATE TABLE slot_dnp (
                competition_key TEXT NOT NULL DEFAULT 'grand_final',
                team_key TEXT NOT NULL,
                slot TEXT NOT NULL,
                dnp INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (competition_key, team_key, slot)
            );
            INSERT INTO slot_dnp (competition_key, team_key, slot, dnp, updated_at)
                SELECT 'grand_final', team_key, slot, dnp, updated_at FROM slot_dnp_legacy;
            DROP TABLE slot_dnp_legacy;
            """
        )

    if _table_exists(conn, "interchange_assignment") and "competition_key" not in _table_columns(
        conn, "interchange_assignment"
    ):
        conn.executescript(
            """
            ALTER TABLE interchange_assignment RENAME TO interchange_assignment_legacy;
            CREATE TABLE interchange_assignment (
                competition_key TEXT NOT NULL DEFAULT 'grand_final',
                team_key TEXT NOT NULL,
                target_position TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (competition_key, team_key)
            );
            INSERT INTO interchange_assignment (competition_key, team_key, target_position, updated_at)
                SELECT 'grand_final', team_key, target_position, updated_at
                FROM interchange_assignment_legacy;
            DROP TABLE interchange_assignment_legacy;
            """
        )

    if _table_exists(conn, "score_override") and "competition_key" not in _table_columns(
        conn, "score_override"
    ):
        conn.executescript(
            """
            ALTER TABLE score_override RENAME TO score_override_legacy;
            CREATE TABLE score_override (
                competition_key TEXT NOT NULL DEFAULT 'grand_final',
                team_key TEXT NOT NULL,
                position TEXT NOT NULL,
                override_score REAL,
                reason TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (competition_key, team_key, position)
            );
            INSERT INTO score_override
                (competition_key, team_key, position, override_score, reason, updated_at)
                SELECT 'grand_final', team_key, position, override_score, reason, updated_at
                FROM score_override_legacy;
            DROP TABLE score_override_legacy;
            """
        )

    if _table_exists(conn, "matchup_state") and "competition_key" not in _table_columns(
        conn, "matchup_state"
    ):
        conn.executescript(
            """
            ALTER TABLE matchup_state RENAME TO matchup_state_legacy;
            CREATE TABLE matchup_state (
                competition_key TEXT PRIMARY KEY,
                finalized INTEGER NOT NULL DEFAULT 0,
                finalized_at TEXT,
                finalized_note TEXT,
                finalized_snapshot TEXT
            );
            INSERT INTO matchup_state
                (competition_key, finalized, finalized_at, finalized_note, finalized_snapshot)
                SELECT 'grand_final', finalized, finalized_at, finalized_note, finalized_snapshot
                FROM matchup_state_legacy WHERE id = 1;
            DROP TABLE matchup_state_legacy;
            """
        )


def init_db(conn: sqlite3.Connection) -> None:
    _migrate_legacy_schema(conn)
    conn.executescript(SCHEMA)
    # CREATE TABLE IF NOT EXISTS won't add columns to a table that already
    # existed before finalized_snapshot was introduced -- add it if missing
    # so an in-place upgrade doesn't lose the ability to store a snapshot.
    existing_columns = _table_columns(conn, "matchup_state")
    if "finalized_snapshot" not in existing_columns:
        conn.execute("ALTER TABLE matchup_state ADD COLUMN finalized_snapshot TEXT")
    conn.execute(
        "INSERT OR IGNORE INTO matchup_state (competition_key, finalized) VALUES (?, 0)",
        (GRAND_FINAL_COMPETITION_KEY,),
    )
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@dataclass(frozen=True)
class InterchangeAssignment:
    team_key: str
    target_position: str | None


@dataclass(frozen=True)
class ScoreOverride:
    team_key: str
    position: str
    override_score: float | None
    reason: str | None


@dataclass(frozen=True)
class MatchupState:
    finalized: bool
    finalized_at: str | None
    finalized_note: str | None
    snapshot: dict | None = None


class DecisionsRepository:
    """CRUD for scorer decisions, scoped to a single competition instance.

    Not thread-safe across processes beyond what sqlite itself guarantees --
    adequate for a single-worker prototype.
    """

    def __init__(self, conn: sqlite3.Connection, competition_key: str = GRAND_FINAL_COMPETITION_KEY):
        self.conn = conn
        self.competition_key = competition_key

    # -- DNP -----------------------------------------------------------
    def set_dnp(self, team_key: str, slot: str, dnp: bool) -> None:
        with transaction(self.conn) as conn:
            conn.execute(
                """
                INSERT INTO slot_dnp (competition_key, team_key, slot, dnp, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(competition_key, team_key, slot) DO UPDATE SET dnp = excluded.dnp,
                    updated_at = excluded.updated_at
                """,
                (self.competition_key, team_key, slot, int(dnp), _now()),
            )

    def get_dnp_map(self) -> dict[tuple[str, str], bool]:
        rows = self.conn.execute(
            "SELECT team_key, slot, dnp FROM slot_dnp WHERE competition_key = ?",
            (self.competition_key,),
        ).fetchall()
        return {(row["team_key"], row["slot"]): bool(row["dnp"]) for row in rows}

    # -- Interchange -----------------------------------------------------
    def set_interchange_assignment(self, team_key: str, target_position: str | None) -> None:
        with transaction(self.conn) as conn:
            conn.execute(
                """
                INSERT INTO interchange_assignment (competition_key, team_key, target_position, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(competition_key, team_key) DO UPDATE SET
                    target_position = excluded.target_position,
                    updated_at = excluded.updated_at
                """,
                (self.competition_key, team_key, target_position, _now()),
            )

    def get_interchange_assignments(self) -> dict[str, InterchangeAssignment]:
        rows = self.conn.execute(
            "SELECT team_key, target_position FROM interchange_assignment WHERE competition_key = ?",
            (self.competition_key,),
        ).fetchall()
        return {
            row["team_key"]: InterchangeAssignment(
                team_key=row["team_key"], target_position=row["target_position"]
            )
            for row in rows
        }

    # -- Overrides -------------------------------------------------------
    def set_override(
        self, team_key: str, position: str, override_score: float | None, reason: str | None
    ) -> None:
        with transaction(self.conn) as conn:
            if override_score is None:
                conn.execute(
                    "DELETE FROM score_override WHERE competition_key = ? AND team_key = ? AND position = ?",
                    (self.competition_key, team_key, position),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO score_override
                        (competition_key, team_key, position, override_score, reason, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(competition_key, team_key, position) DO UPDATE SET
                        override_score = excluded.override_score,
                        reason = excluded.reason,
                        updated_at = excluded.updated_at
                    """,
                    (self.competition_key, team_key, position, override_score, reason, _now()),
                )

    def get_overrides(self) -> dict[tuple[str, str], ScoreOverride]:
        rows = self.conn.execute(
            "SELECT team_key, position, override_score, reason FROM score_override WHERE competition_key = ?",
            (self.competition_key,),
        ).fetchall()
        return {
            (row["team_key"], row["position"]): ScoreOverride(
                team_key=row["team_key"],
                position=row["position"],
                override_score=row["override_score"],
                reason=row["reason"],
            )
            for row in rows
        }

    # -- Matchup lifecycle -------------------------------------------------
    def get_matchup_state(self) -> MatchupState:
        row = self.conn.execute(
            "SELECT finalized, finalized_at, finalized_note, finalized_snapshot "
            "FROM matchup_state WHERE competition_key = ?",
            (self.competition_key,),
        ).fetchone()
        if row is None:
            # No decisions recorded yet for this competition instance (e.g. a
            # SuperScore round that hasn't had its first scorer action) --
            # a fresh, unfinalized state rather than an error.
            return MatchupState(finalized=False, finalized_at=None, finalized_note=None, snapshot=None)
        snapshot = json.loads(row["finalized_snapshot"]) if row["finalized_snapshot"] else None
        return MatchupState(
            finalized=bool(row["finalized"]),
            finalized_at=row["finalized_at"],
            finalized_note=row["finalized_note"],
            snapshot=snapshot,
        )

    def finalize(self, note: str | None, snapshot: dict | None = None) -> None:
        """Lock this competition instance and, when a snapshot is supplied,
        freeze the official result as it stood at sign-off time. Once
        finalized with a snapshot, later reads are served from this frozen
        copy rather than re-querying afl-api -- so a post-signoff upstream
        correction, round rollover, or afl-api outage cannot change or hide
        an already-FINAL result (see docs/plans/2027-grand-final-prototype-brief.md's
        persistence/recovery requirements).
        """
        now = _now()
        stored_snapshot = None
        if snapshot is not None:
            snapshot = dict(snapshot)
            snapshot["status"] = "FINAL"
            snapshot["finalized_at"] = now
            snapshot["finalized_note"] = note
            stored_snapshot = json.dumps(snapshot)
        with transaction(self.conn) as conn:
            conn.execute(
                """
                INSERT INTO matchup_state
                    (competition_key, finalized, finalized_at, finalized_note, finalized_snapshot)
                VALUES (?, 1, ?, ?, ?)
                ON CONFLICT(competition_key) DO UPDATE SET
                    finalized = 1,
                    finalized_at = excluded.finalized_at,
                    finalized_note = excluded.finalized_note,
                    finalized_snapshot = excluded.finalized_snapshot
                """,
                (self.competition_key, now, note, stored_snapshot),
            )
