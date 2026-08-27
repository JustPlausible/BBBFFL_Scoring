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


def test_afl_api_resilience_settings_default_sensibly(monkeypatch):
    """Roadmap package 05 / issue #37: connect/read timeouts default to
    unset (AflApiClient then falls back to afl_api_timeout_seconds for
    both, preserving prior single-timeout behaviour), and retry policy has
    small, bounded defaults."""
    for name in (
        "AFL_API_CONNECT_TIMEOUT_SECONDS",
        "AFL_API_READ_TIMEOUT_SECONDS",
        "AFL_API_RETRY_MAX_ATTEMPTS",
        "AFL_API_RETRY_BASE_DELAY_SECONDS",
        "AFL_API_RETRY_MAX_DELAY_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = get_settings()

    assert settings.afl_api_connect_timeout_seconds is None
    assert settings.afl_api_read_timeout_seconds is None
    assert settings.afl_api_retry_max_attempts == 3
    assert settings.afl_api_retry_base_delay_seconds == 0.2
    assert settings.afl_api_retry_max_delay_seconds == 2.0


def test_afl_api_resilience_settings_are_configurable(monkeypatch):
    monkeypatch.setenv("AFL_API_CONNECT_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("AFL_API_READ_TIMEOUT_SECONDS", "8")
    monkeypatch.setenv("AFL_API_RETRY_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("AFL_API_RETRY_BASE_DELAY_SECONDS", "0.5")
    monkeypatch.setenv("AFL_API_RETRY_MAX_DELAY_SECONDS", "10")

    settings = get_settings()

    assert settings.afl_api_connect_timeout_seconds == 3.0
    assert settings.afl_api_read_timeout_seconds == 8.0
    assert settings.afl_api_retry_max_attempts == 5
    assert settings.afl_api_retry_base_delay_seconds == 0.5
    assert settings.afl_api_retry_max_delay_seconds == 10.0


def test_dockerfile_does_not_set_database_url_over_legacy_db_path():
    """The image must default BBBFFL_DB_PATH, not BBBFFL_DATABASE_URL, or an
    upgraded legacy deployment that only ever set BBBFFL_DB_PATH would have
    it silently overridden and appear to lose its existing data."""
    dockerfile = (BASE_DIR / "Dockerfile").read_text()

    assert "BBBFFL_DB_PATH=" in dockerfile
    assert "BBBFFL_DATABASE_URL=" not in dockerfile
