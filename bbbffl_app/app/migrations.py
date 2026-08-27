"""Versioned schema migration entry points and validated legacy bootstrap."""
from pathlib import Path
import argparse
import os

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

# Importing the database boundary registers SQLite's per-connection foreign-key
# enforcement before Alembic or legacy-schema inspection opens an engine.
from app import db as _database_boundary  # noqa: F401

HEAD = "0007_fixture"
TABLES = {"slot_dnp", "interchange_assignment", "score_override", "matchup_state"}
LEGACY_COLUMNS = {
    "slot_dnp": {"team_key", "slot", "dnp", "updated_at"},
    "interchange_assignment": {"team_key", "target_position", "updated_at"},
    "score_override": {"team_key", "position", "override_score", "reason", "updated_at"},
}
CURRENT_COLUMNS = {
    "slot_dnp": {"competition_key", "team_key", "slot", "dnp", "updated_at"},
    "interchange_assignment": {"competition_key", "team_key", "target_position", "updated_at"},
    "score_override": {"competition_key", "team_key", "position", "override_score", "reason", "updated_at"},
    "matchup_state": {"competition_key", "finalized", "finalized_at", "finalized_note", "finalized_snapshot"},
}
LEGACY_PRIMARY_KEYS = {
    "slot_dnp": ["team_key", "slot"],
    "interchange_assignment": ["team_key"],
    "score_override": ["team_key", "position"],
    "matchup_state": ["id"],
}
CURRENT_PRIMARY_KEYS = {
    "slot_dnp": ["competition_key", "team_key", "slot"],
    "interchange_assignment": ["competition_key", "team_key"],
    "score_override": ["competition_key", "team_key", "position"],
    "matchup_state": ["competition_key"],
}


def _config(database_url: str) -> Config:
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def _shape(inspector, table):
    return {column["name"] for column in inspector.get_columns(table)}


def _validate_legacy(inspector) -> str | None:
    user_tables = set(inspector.get_table_names()) - {"alembic_version"}
    if not user_tables:
        return None
    if user_tables != TABLES:
        raise RuntimeError(f"Unrecognized BBBFFL schema: expected exactly {sorted(TABLES)}, found {sorted(user_tables)}")
    shapes = {table: _shape(inspector, table) for table in TABLES}
    primary_keys = {
        table: inspector.get_pk_constraint(table).get("constrained_columns") or []
        for table in TABLES
    }
    legacy_matchup = {"id", "finalized", "finalized_at", "finalized_note"}
    if (all(shapes[t] == LEGACY_COLUMNS[t] for t in LEGACY_COLUMNS)
            and shapes["matchup_state"] in (legacy_matchup, legacy_matchup | {"finalized_snapshot"})
            and primary_keys == LEGACY_PRIMARY_KEYS):
        return "0001_prototype"
    if shapes == CURRENT_COLUMNS and primary_keys == CURRENT_PRIMARY_KEYS:
        # This is the 0002 shape specifically (four decision tables, no
        # audit_event yet) -- stamp at that revision, not the dynamic HEAD,
        # so the upgrade below still runs any newer revisions (e.g. 0003) on
        # top of it instead of skipping them.
        return "0002_competition"
    details = ", ".join(
        f"{table} columns={sorted(columns)} pk={primary_keys[table]}"
        for table, columns in sorted(shapes.items())
    )
    raise RuntimeError(f"Unrecognized BBBFFL schema columns; refusing to stamp or mutate: {details}")


def migrate(database_url: str, revision: str = "head") -> None:
    """Validate unversioned databases, bootstrap them, then migrate."""
    cfg = _config(database_url)
    engine = create_engine(database_url)
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        if "alembic_version" not in tables:
            baseline = _validate_legacy(inspect(connection))
            if baseline:
                cfg.attributes["connection"] = connection
                command.stamp(cfg, baseline)
                connection.commit()
                del cfg.attributes["connection"]
    engine.dispose()
    command.upgrade(cfg, revision)


def current(database_url: str) -> None:
    command.current(_config(database_url), verbose=True)


def downgrade(database_url: str, revision: str) -> None:
    command.downgrade(_config(database_url), revision)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the BBBFFL relational schema")
    parser.add_argument("action", choices=("upgrade", "current", "downgrade"))
    parser.add_argument("revision", nargs="?", default="head")
    parser.add_argument("--database-url", default=os.getenv("BBBFFL_DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        parser.error("set BBBFFL_DATABASE_URL or pass --database-url")
    if args.action == "upgrade":
        migrate(args.database_url, args.revision)
    elif args.action == "current":
        current(args.database_url)
    else:
        downgrade(args.database_url, args.revision)


if __name__ == "__main__":
    main()
