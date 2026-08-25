"""Relational storage for scorer decisions.

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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

GRAND_FINAL_COMPETITION_KEY = "grand_final"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _translate(statement, parameters):
    """Rewrite '?' positional placeholders (this module's DB-API-ish
    convention) into the named parameters SQLAlchemy's text() expects."""
    if isinstance(parameters, tuple):
        values = {}
        pieces = statement.split("?")
        rebuilt = pieces[0]
        for index, piece in enumerate(pieces[1:]):
            name = f"p{index}"
            rebuilt += f":{name}" + piece
            values[name] = parameters[index]
        statement, parameters = rebuilt, values
    return statement, parameters


class _Result:
    """A result whose rows are materialized immediately, so callers can read
    them after the connection that produced them has already been released
    back to the pool."""
    def __init__(self, sa_result):
        self._rows = sa_result.mappings().all() if sa_result.returns_rows else []
    def fetchall(self):
        return self._rows
    def fetchone(self):
        return self._rows[0] if self._rows else None


class _TransactionConnection:
    """Bound to one connection for the lifetime of a single transaction()
    block; not retained by callers past that block."""
    def __init__(self, connection):
        self._connection = connection
    def execute(self, statement, parameters=()):
        from sqlalchemy import text
        statement, parameters = _translate(statement, parameters)
        return _Result(self._connection.execute(text(statement), parameters))


class DatabaseConnection:
    """Small DB-API-shaped facade over a SQLAlchemy Engine/pool.

    Deliberately holds no long-lived Connection: a synchronous FastAPI
    handler can run concurrently with others in Starlette's thread pool, and
    a SQLAlchemy Connection is not a thread-safe request boundary. Every
    operation instead acquires a pooled connection for just its own duration
    and releases it immediately afterwards, so requests never share
    transaction state (see PR #23 review).
    """
    def __init__(self, engine):
        self.engine = engine

    def execute(self, statement, parameters=()):
        """Run one statement on a short-lived pooled connection.

        Used for reads (no explicit transaction needed) and is also what
        `init_db`'s legacy-detection probe relies on.
        """
        from sqlalchemy import text
        statement, parameters = _translate(statement, parameters)
        with self.engine.connect() as connection:
            return _Result(connection.execute(text(statement), parameters))

    def close(self):
        self.engine.dispose()


def connect(database_url: str) -> DatabaseConnection:
    from sqlalchemy import create_engine
    if "://" not in database_url:
        database_url = f"sqlite:///{database_url}"
    return DatabaseConnection(create_engine(database_url, connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {}))


def init_db(conn) -> None:
    """Deprecated compatibility shim for callers already on a migrated DB.

    Schema creation belongs exclusively to Alembic. Tests and deployments must
    call app.migrations.migrate before opening the repository connection.
    """
    try:
        conn.execute("SELECT version_num FROM alembic_version").fetchone()
    except Exception as exc:
        raise RuntimeError("database is not migration-managed; run `alembic upgrade head`") from exc


@contextmanager
def transaction(database: DatabaseConnection):
    """Run one write operation in its own pooled connection and transaction,
    committing on success and rolling back on error -- never reusing a
    connection across calls, so one request can never observe or disturb
    another's in-flight transaction."""
    with database.engine.connect() as connection:
        with connection.begin():
            yield _TransactionConnection(connection)


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

    Transactions are provided by SQLAlchemy while persistence semantics remain
    explicit SQL and are shared by SQLite and PostgreSQL.
    """

    def __init__(self, conn, competition_key: str = GRAND_FINAL_COMPETITION_KEY):
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
