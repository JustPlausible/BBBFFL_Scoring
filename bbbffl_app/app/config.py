"""Central, validated application settings boundary (roadmap package 06,
issue #38).

`get_settings()` is the sole supported way to read runtime configuration.
Call sites take a `Settings` value (from `app.state.settings`, populated
once at startup by `app/main.py`'s lifespan) rather than reading
`os.environ`/`os.getenv` directly, so every environment variable name,
default, and production requirement lives in exactly one place.

No credentials are committed. Copy .env.example to .env for local/home-server
use, or set the environment variables directly (e.g. via docker-compose or
a systemd unit).

## Environments

`BBBFFL_ENVIRONMENT` selects one of three modes (default: "development"):

  - "development" -- a single operator's home-server/local prototype.
    Permissive defaults (open admin interface, local SQLite, localhost
    afl-api) are intentional and unchanged from the prototype's original
    behaviour.
  - "test" -- automated/CI test runs. Same permissive defaults as
    development; kept as a distinct declared value so a test run is
    identifiable rather than indistinguishable from a real deployment.
  - "production" -- a real, reachable deployment. A development default is
    never silently reused to satisfy a production requirement: each
    production-required setting below is left genuinely unset unless
    explicitly provided, so a missing value is always reported as missing
    rather than quietly filled in.

## Fail-closed validation

`get_settings()` always validates before returning. `SettingsError.errors`
lists every problem found (not just the first), each naming the offending
environment variable and never including a secret's value -- only whether
it is missing or invalid. `app/main.py`'s lifespan calls `get_settings()`
as its first statement, so an invalid configuration raises before
migrations run, before a database connection opens, and before the app can
accept a request: the application never partially starts.

## AFL access mode

`BBBFFL_AFL_MODE` is "live" (default) or "replay". "replay" is for
deterministic/replay execution (roadmap package 32) against curated
evidence instead of a live afl-api deployment, and must be declared
explicitly: setting it also requires `BBBFFL_AFL_REPLAY_EVIDENCE_PATH`, so
a replay/deterministic run can never silently fall back to live afl-api
access just because that path was left unset. "replay" is refused outright
in production, which must always use live access.

`app/main.py` selects the strict file-backed `ReplayAflDataSource` for a
well-formed replay declaration. It never constructs `AflApiClient` in that
branch, and evidence loading fails closed before application services start.

## AFL consumer contract version

`AFL_API_CONTRACT_VERSION` (default "v1") declares which `afl-api`
consumer contract BBBFFL expects, per the pinning policy in
`docs/afl-api-v1-contract.md` (issue #18). It is validated against the set
of versions this codebase actually implements and passed to
`AflApiClient` so every request path is built from it -- an operator
requesting an unimplemented version fails at startup instead of BBBFFL
silently trying to speak a contract it does not support.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent

ENVIRONMENTS = ("development", "test", "production")
AFL_MODES = ("live", "replay")
SUPPORTED_AFL_API_CONTRACT_VERSIONS = ("v1",)

# Development/test-only session/CSRF signing secret (roadmap package 19,
# issue #74) -- see BBBFFL_SESSION_SECRET below. Never used to satisfy a
# production requirement: get_settings() refuses this exact value outright
# when BBBFFL_ENVIRONMENT=production, in case a deployment ever copies
# .env.example without changing it.
_DEV_SESSION_SECRET = "dev-insecure-session-secret-change-in-production"
DEFAULT_SESSION_LIFETIME_SECONDS = 12 * 60 * 60


class SettingsError(ValueError):
    """Raised by `get_settings()` for one or more invalid/missing settings.

    `errors` holds every problem found, each already formatted as
    "<ENV_VAR_NAME>: <reason>". Never contains a secret's value -- only
    whether one is missing or malformed.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("Invalid BBBFFL configuration:\n" + "\n".join(f"  - {e}" for e in self.errors))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str) -> float | None:
    raw = os.getenv(name)
    return float(raw) if raw else None


def _is_http_url(value: str) -> bool:
    """True for a well-formed absolute http(s) URL suitable for a critical
    endpoint setting.

    Deliberately rejects embedded userinfo (`https://user:pass@host/...`):
    afl-api/public URLs already have a dedicated credential channel
    (`AFL_API_KEY`), and accepting one here would risk that credential
    being written to a startup log line alongside the base URL --
    violating "never log secret values" by construction rather than by
    remembering to redact every call site that logs a configured URL.
    Also rejects a malformed port (e.g. `https://host:notaport`), which
    `urlsplit` alone accepts syntactically but which would otherwise only
    fail later, inside `httpx`, well after settings validation claimed
    success.
    """
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    if not parts.hostname:
        return False
    try:
        parts.port  # noqa: B018 -- raises ValueError for a non-numeric port
    except ValueError:
        return False
    return True


_POSTGRESQL_SCHEME = re.compile(r"^postgresql(\+[a-zA-Z0-9_]+)?$")


def _is_database_url(value: str) -> bool:
    scheme = urlsplit(value).scheme
    return scheme == "sqlite" or bool(_POSTGRESQL_SCHEME.match(scheme))


@dataclass(frozen=True)
class Settings:
    environment: str
    afl_api_base_url: str
    afl_api_key: str | None
    # Declares which afl-api /api/v1-style consumer contract this
    # deployment expects (see docs/afl-api-v1-contract.md, issue #18).
    # AflApiClient builds every request path from this value.
    afl_api_contract_version: str
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
    # "live" talks to afl-api; "replay" selects strict controlled evidence.
    afl_mode: str
    afl_replay_evidence_path: str | None
    database_path: str
    database_url: str
    teams_config_path: str
    admin_token: str | None
    # Coach authentication CSRF-token signing secret (roadmap package 19,
    # issue #74) -- keys the HMAC in app/csrf.py. Session bearer tokens
    # themselves (app/auth.py) are independent high-entropy random values
    # looked up by hash, so they need no secret of their own. See
    # docs/coach-authentication.md.
    session_secret: str | None
    session_lifetime_seconds: int
    # The externally reachable base URL of this deployment. Not yet
    # consumed by application behaviour (no link generation/CORS exists
    # yet) -- validated and made explicit now per roadmap package 06 so
    # later packages (39 deployment, 40 notifications) have one place to
    # read it from rather than inventing their own environment variable.
    public_base_url: str | None
    poll_interval_seconds: int
    log_level: str
    # SuperScore is entirely opt-in: unset (the default), the app behaves
    # exactly as it does today -- no SuperScore state, routes still exist
    # but report disabled. Set BBBFFL_SUPERSCORE_CONFIG_PATH to a checked-in
    # JSON entries file (see data/superscore_teams.example.json) to enable it.
    superscore_config_path: str | None

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


def get_settings() -> Settings:
    errors: list[str] = []

    environment = os.getenv("BBBFFL_ENVIRONMENT", "development").strip().lower()
    if environment not in ENVIRONMENTS:
        errors.append(f"BBBFFL_ENVIRONMENT: must be one of {', '.join(ENVIRONMENTS)} (got '{environment}')")
        environment = "development"  # only to let remaining defaults resolve; still fails below
    is_production = environment == "production"

    # Database URL/path: a production deployment must say explicitly where
    # its database lives (and it must be PostgreSQL -- see
    # docs/database-migrations.md); the SQLite development default is never
    # silently reused to satisfy that requirement.
    database_path = os.getenv("BBBFFL_DB_PATH", str(BASE_DIR / "data" / "scorer_decisions.db"))
    raw_database_url = os.getenv("BBBFFL_DATABASE_URL")
    if raw_database_url:
        database_url = raw_database_url
    elif is_production:
        database_url = ""
        errors.append("BBBFFL_DATABASE_URL: required in production (no default database is used)")
    else:
        database_url = f"sqlite:///{database_path}"
    if database_url and not _is_database_url(database_url):
        errors.append(
            "BBBFFL_DATABASE_URL: must be a sqlite:/// or postgresql(+driver):// URL "
            "(driver, if present, must be a plain alphanumeric DBAPI name)"
        )
    elif database_url and is_production and not database_url.split("://", 1)[0].startswith("postgresql"):
        errors.append(
            "BBBFFL_DATABASE_URL: must be a PostgreSQL URL in production (SQLite is development/test/replay only)"
        )

    raw_public_base_url = (os.getenv("BBBFFL_PUBLIC_BASE_URL") or "").strip() or None
    public_base_url = raw_public_base_url
    if public_base_url is not None and not _is_http_url(public_base_url):
        errors.append(
            "BBBFFL_PUBLIC_BASE_URL: must be an absolute http(s) URL with a valid host/port "
            "and no embedded userinfo credentials"
        )
    elif public_base_url is None and is_production:
        errors.append("BBBFFL_PUBLIC_BASE_URL: required in production")

    raw_afl_api_base_url = os.getenv("AFL_API_BASE_URL")
    if raw_afl_api_base_url:
        afl_api_base_url = raw_afl_api_base_url.rstrip("/")
    elif is_production:
        afl_api_base_url = ""
        errors.append("AFL_API_BASE_URL: required in production (no default afl-api endpoint is used)")
    else:
        afl_api_base_url = "http://localhost:8000"
    if afl_api_base_url and not _is_http_url(afl_api_base_url):
        errors.append(
            "AFL_API_BASE_URL: must be an absolute http(s) URL with a valid host/port "
            "and no embedded userinfo credentials -- use AFL_API_KEY for authentication"
        )

    afl_api_contract_version = os.getenv("AFL_API_CONTRACT_VERSION", "v1").strip().lower()
    if afl_api_contract_version not in SUPPORTED_AFL_API_CONTRACT_VERSIONS:
        errors.append(
            "AFL_API_CONTRACT_VERSION: must be one of "
            f"{', '.join(SUPPORTED_AFL_API_CONTRACT_VERSIONS)} (got '{afl_api_contract_version}') "
            "-- see docs/afl-api-v1-contract.md"
        )

    afl_mode = os.getenv("BBBFFL_AFL_MODE", "live").strip().lower()
    afl_replay_evidence_path = os.getenv("BBBFFL_AFL_REPLAY_EVIDENCE_PATH") or None
    if afl_mode not in AFL_MODES:
        errors.append(f"BBBFFL_AFL_MODE: must be one of {', '.join(AFL_MODES)} (got '{afl_mode}')")
    elif afl_mode == "replay":
        if is_production:
            errors.append(
                "BBBFFL_AFL_MODE: 'replay' is not permitted when BBBFFL_ENVIRONMENT=production; "
                "production must always use live afl-api access"
            )
        if not afl_replay_evidence_path:
            errors.append(
                "BBBFFL_AFL_REPLAY_EVIDENCE_PATH: required when BBBFFL_AFL_MODE=replay -- "
                "replay/deterministic execution must never silently fall back to live afl-api access"
            )

    admin_token = os.getenv("BBBFFL_ADMIN_TOKEN") or None
    if is_production and not admin_token:
        errors.append(
            "BBBFFL_ADMIN_TOKEN: required in production (refusing to start with the admin interface open to any caller)"
        )

    # Coach session/CSRF secret (roadmap package 19, issue #74): unlike
    # BBBFFL_ADMIN_TOKEN (which simply disables its check when unset),
    # this always has *some* value -- development/test get a fixed,
    # clearly-labelled placeholder so the auth flow is exercisable locally
    # without configuration, but that placeholder is refused outright in
    # production, exactly like BBBFFL_AFL_MODE=replay is refused there.
    raw_session_secret = os.getenv("BBBFFL_SESSION_SECRET") or None
    if is_production:
        if not raw_session_secret:
            errors.append(
                "BBBFFL_SESSION_SECRET: required in production (coach session/CSRF signing must not use "
                "the shared development placeholder)"
            )
            session_secret = None
        elif raw_session_secret == _DEV_SESSION_SECRET:
            errors.append("BBBFFL_SESSION_SECRET: must not be the development placeholder value in production")
            session_secret = None
        else:
            session_secret = raw_session_secret
    else:
        session_secret = raw_session_secret or _DEV_SESSION_SECRET

    session_lifetime_seconds = int(os.getenv("BBBFFL_SESSION_LIFETIME_SECONDS", str(DEFAULT_SESSION_LIFETIME_SECONDS)))
    if session_lifetime_seconds <= 0:
        errors.append("BBBFFL_SESSION_LIFETIME_SECONDS: must be a positive number of seconds")

    if errors:
        raise SettingsError(errors)

    return Settings(
        environment=environment,
        afl_api_base_url=afl_api_base_url,
        afl_api_key=os.getenv("AFL_API_KEY") or None,
        afl_api_contract_version=afl_api_contract_version,
        afl_api_timeout_seconds=float(os.getenv("AFL_API_TIMEOUT_SECONDS", "10")),
        afl_api_connect_timeout_seconds=_env_float("AFL_API_CONNECT_TIMEOUT_SECONDS"),
        afl_api_read_timeout_seconds=_env_float("AFL_API_READ_TIMEOUT_SECONDS"),
        afl_api_retry_max_attempts=int(os.getenv("AFL_API_RETRY_MAX_ATTEMPTS", "3")),
        afl_api_retry_base_delay_seconds=float(os.getenv("AFL_API_RETRY_BASE_DELAY_SECONDS", "0.2")),
        afl_api_retry_max_delay_seconds=float(os.getenv("AFL_API_RETRY_MAX_DELAY_SECONDS", "2.0")),
        afl_mode=afl_mode,
        afl_replay_evidence_path=afl_replay_evidence_path,
        database_path=database_path,
        database_url=database_url,
        teams_config_path=os.getenv("BBBFFL_TEAMS_CONFIG_PATH", str(BASE_DIR / "data" / "grand_final_teams.json")),
        admin_token=admin_token,
        session_secret=session_secret,
        session_lifetime_seconds=session_lifetime_seconds,
        public_base_url=public_base_url,
        poll_interval_seconds=int(os.getenv("BBBFFL_POLL_INTERVAL_SECONDS", "25")),
        log_level=os.getenv("BBBFFL_LOG_LEVEL", "INFO"),
        superscore_config_path=os.getenv("BBBFFL_SUPERSCORE_CONFIG_PATH") or None,
    )
