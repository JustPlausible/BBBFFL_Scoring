"""Environment-driven application settings.

No credentials are committed. Copy .env.example to .env for local/home-server
use, or set the environment variables directly (e.g. via docker-compose or
a systemd unit).
"""

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str) -> float | None:
    raw = os.getenv(name)
    return float(raw) if raw else None


@dataclass(frozen=True)
class Settings:
    afl_api_base_url: str
    afl_api_key: str | None
    afl_api_timeout_seconds: float
    # Explicit connect/read timeout budgets (see app/afl_client.py). Both
    # default to afl_api_timeout_seconds when unset, preserving the exact
    # prior single-timeout behaviour for any deployment that has not set
    # these two new variables.
    afl_api_connect_timeout_seconds: float | None
    afl_api_read_timeout_seconds: float | None
    # Bounded transient retry/backoff (see app/afl_resilience.py). Retries
    # only ever apply to connection failures, timeouts and transient
    # (408/429/5xx) upstream responses -- never to a contract/schema
    # incompatibility.
    afl_api_retry_max_attempts: int
    afl_api_retry_base_delay_seconds: float
    afl_api_retry_max_delay_seconds: float
    database_path: str
    database_url: str
    teams_config_path: str
    admin_token: str | None
    poll_interval_seconds: int
    log_level: str
    # SuperScore is entirely opt-in: unset (the default), the app behaves
    # exactly as it does today -- no SuperScore state, routes still exist
    # but report disabled. Set BBBFFL_SUPERSCORE_CONFIG_PATH to a checked-in
    # JSON entries file (see data/superscore_teams.example.json) to enable it.
    superscore_config_path: str | None


def get_settings() -> Settings:
    database_path = os.getenv("BBBFFL_DB_PATH", str(BASE_DIR / "data" / "scorer_decisions.db"))
    return Settings(
        afl_api_base_url=os.getenv("AFL_API_BASE_URL", "http://localhost:8000").rstrip("/"),
        afl_api_key=os.getenv("AFL_API_KEY") or None,
        afl_api_timeout_seconds=float(os.getenv("AFL_API_TIMEOUT_SECONDS", "10")),
        afl_api_connect_timeout_seconds=_env_float("AFL_API_CONNECT_TIMEOUT_SECONDS"),
        afl_api_read_timeout_seconds=_env_float("AFL_API_READ_TIMEOUT_SECONDS"),
        afl_api_retry_max_attempts=int(os.getenv("AFL_API_RETRY_MAX_ATTEMPTS", "3")),
        afl_api_retry_base_delay_seconds=float(os.getenv("AFL_API_RETRY_BASE_DELAY_SECONDS", "0.2")),
        afl_api_retry_max_delay_seconds=float(os.getenv("AFL_API_RETRY_MAX_DELAY_SECONDS", "2.0")),
        database_path=database_path,
        database_url=os.getenv("BBBFFL_DATABASE_URL", f"sqlite:///{database_path}"),
        teams_config_path=os.getenv(
            "BBBFFL_TEAMS_CONFIG_PATH", str(BASE_DIR / "data" / "grand_final_teams.json")
        ),
        admin_token=os.getenv("BBBFFL_ADMIN_TOKEN") or None,
        poll_interval_seconds=int(os.getenv("BBBFFL_POLL_INTERVAL_SECONDS", "25")),
        log_level=os.getenv("BBBFFL_LOG_LEVEL", "INFO"),
        superscore_config_path=os.getenv("BBBFFL_SUPERSCORE_CONFIG_PATH") or None,
    )
