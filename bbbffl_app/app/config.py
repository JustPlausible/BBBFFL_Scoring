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


@dataclass(frozen=True)
class Settings:
    afl_api_base_url: str
    afl_api_key: str | None
    afl_api_timeout_seconds: float
    database_path: str
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
    return Settings(
        afl_api_base_url=os.getenv("AFL_API_BASE_URL", "http://localhost:8000").rstrip("/"),
        afl_api_key=os.getenv("AFL_API_KEY") or None,
        afl_api_timeout_seconds=float(os.getenv("AFL_API_TIMEOUT_SECONDS", "10")),
        database_path=os.getenv("BBBFFL_DB_PATH", str(BASE_DIR / "data" / "scorer_decisions.db")),
        teams_config_path=os.getenv(
            "BBBFFL_TEAMS_CONFIG_PATH", str(BASE_DIR / "data" / "grand_final_teams.json")
        ),
        admin_token=os.getenv("BBBFFL_ADMIN_TOKEN") or None,
        poll_interval_seconds=int(os.getenv("BBBFFL_POLL_INTERVAL_SECONDS", "25")),
        log_level=os.getenv("BBBFFL_LOG_LEVEL", "INFO"),
        superscore_config_path=os.getenv("BBBFFL_SUPERSCORE_CONFIG_PATH") or None,
    )
