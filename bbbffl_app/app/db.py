"""SQLite storage for scorer decisions.

Scorer decisions (DNP, interchange assignment, direct score overrides, and
Grand Final finalisation) are kept entirely separate from the coach-declared
team configuration (see teams.py) and are never used to mutate AFL source
statistics -- they only affect the calculated BBBFFL score. This store must
survive a container restart, so the database file lives on a mounted volume
(see Dockerfile / README).
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS slot_dnp (
    team_key TEXT NOT NULL,
    slot TEXT NOT NULL,
    dnp INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (team_key, slot)
);

CREATE TABLE IF NOT EXISTS interchange_assignment (
    team_key TEXT PRIMARY KEY,
    target_position TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS score_override (
    team_key TEXT NOT NULL,
    position TEXT NOT NULL,
    override_score REAL,
    reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (team_key, position)
);

CREATE TABLE IF NOT EXISTS matchup_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    finalized INTEGER NOT NULL DEFAULT 0,
    finalized_at TEXT,
    finalized_note TEXT
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


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO matchup_state (id, finalized) VALUES (1, 0)"
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


class DecisionsRepository:
    """CRUD for scorer decisions. Not thread-safe across processes beyond
    what sqlite itself guarantees -- adequate for a single-worker prototype.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- DNP -----------------------------------------------------------
    def set_dnp(self, team_key: str, slot: str, dnp: bool) -> None:
        with transaction(self.conn) as conn:
            conn.execute(
                """
                INSERT INTO slot_dnp (team_key, slot, dnp, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(team_key, slot) DO UPDATE SET dnp = excluded.dnp,
                    updated_at = excluded.updated_at
                """,
                (team_key, slot, int(dnp), _now()),
            )

    def get_dnp_map(self) -> dict[tuple[str, str], bool]:
        rows = self.conn.execute("SELECT team_key, slot, dnp FROM slot_dnp").fetchall()
        return {(row["team_key"], row["slot"]): bool(row["dnp"]) for row in rows}

    # -- Interchange -----------------------------------------------------
    def set_interchange_assignment(self, team_key: str, target_position: str | None) -> None:
        with transaction(self.conn) as conn:
            conn.execute(
                """
                INSERT INTO interchange_assignment (team_key, target_position, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(team_key) DO UPDATE SET target_position = excluded.target_position,
                    updated_at = excluded.updated_at
                """,
                (team_key, target_position, _now()),
            )

    def get_interchange_assignments(self) -> dict[str, InterchangeAssignment]:
        rows = self.conn.execute(
            "SELECT team_key, target_position FROM interchange_assignment"
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
                    "DELETE FROM score_override WHERE team_key = ? AND position = ?",
                    (team_key, position),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO score_override (team_key, position, override_score, reason, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(team_key, position) DO UPDATE SET
                        override_score = excluded.override_score,
                        reason = excluded.reason,
                        updated_at = excluded.updated_at
                    """,
                    (team_key, position, override_score, reason, _now()),
                )

    def get_overrides(self) -> dict[tuple[str, str], ScoreOverride]:
        rows = self.conn.execute(
            "SELECT team_key, position, override_score, reason FROM score_override"
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
            "SELECT finalized, finalized_at, finalized_note FROM matchup_state WHERE id = 1"
        ).fetchone()
        return MatchupState(
            finalized=bool(row["finalized"]),
            finalized_at=row["finalized_at"],
            finalized_note=row["finalized_note"],
        )

    def finalize(self, note: str | None) -> None:
        with transaction(self.conn) as conn:
            conn.execute(
                "UPDATE matchup_state SET finalized = 1, finalized_at = ?, finalized_note = ? WHERE id = 1",
                (_now(), note),
            )
