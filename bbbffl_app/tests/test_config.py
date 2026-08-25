"""Regression coverage for BBBFFL_DATABASE_URL / BBBFFL_DB_PATH precedence.

An existing prototype deployment may have only ever set BBBFFL_DB_PATH. The
Docker image must not silently introduce a BBBFFL_DATABASE_URL that shadows
that legacy setting and points the upgraded app at an empty database (PR #23
review). See app/config.py and Dockerfile.
"""

from pathlib import Path

from app.config import BASE_DIR, get_settings


def test_explicit_database_url_wins_over_db_path(monkeypatch):
    monkeypatch.setenv("BBBFFL_DATABASE_URL", "postgresql+psycopg://user:pass@db/bbbffl")
    monkeypatch.setenv("BBBFFL_DB_PATH", "/legacy/scorer_decisions.db")

    settings = get_settings()

    assert settings.database_url == "postgresql+psycopg://user:pass@db/bbbffl"


def test_explicit_db_path_is_used_when_no_database_url_is_set(monkeypatch):
    monkeypatch.delenv("BBBFFL_DATABASE_URL", raising=False)
    monkeypatch.setenv("BBBFFL_DB_PATH", "/legacy/scorer_decisions.db")

    settings = get_settings()

    assert settings.database_url == "sqlite:////legacy/scorer_decisions.db"
    assert settings.database_path == "/legacy/scorer_decisions.db"


def test_default_sqlite_path_is_used_when_neither_is_set(monkeypatch):
    monkeypatch.delenv("BBBFFL_DATABASE_URL", raising=False)
    monkeypatch.delenv("BBBFFL_DB_PATH", raising=False)

    settings = get_settings()

    assert settings.database_url == f"sqlite:///{settings.database_path}"
    assert settings.database_path.endswith("data/scorer_decisions.db")


def test_dockerfile_does_not_set_database_url_over_legacy_db_path():
    """The image must default BBBFFL_DB_PATH, not BBBFFL_DATABASE_URL, or an
    upgraded legacy deployment that only ever set BBBFFL_DB_PATH would have
    it silently overridden and appear to lose its existing data."""
    dockerfile = (BASE_DIR / "Dockerfile").read_text()

    assert "BBBFFL_DB_PATH=" in dockerfile
    assert "BBBFFL_DATABASE_URL=" not in dockerfile
