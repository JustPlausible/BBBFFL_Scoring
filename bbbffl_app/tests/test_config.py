"""Regression coverage for BBBFFL_DATABASE_URL / BBBFFL_DB_PATH precedence,
and for the central validated settings boundary itself (roadmap package 06,
issue #38).

An existing prototype deployment may have only ever set BBBFFL_DB_PATH. The
Docker image must not silently introduce a BBBFFL_DATABASE_URL that shadows
that legacy setting and points the upgraded app at an empty database (PR #23
review). See app/config.py and Dockerfile.
"""

import pytest

from app.config import BASE_DIR, SettingsError, get_settings

# Every environment variable get_settings() reads. Cleared before each
# settings-boundary test below so no test can leak state into another via
# a real process environment variable neither test itself set.
_ALL_SETTINGS_ENV_VARS = (
    "BBBFFL_ENVIRONMENT",
    "BBBFFL_DATABASE_URL",
    "BBBFFL_DB_PATH",
    "BBBFFL_PUBLIC_BASE_URL",
    "BBBFFL_ADMIN_TOKEN",
    "BBBFFL_AFL_MODE",
    "BBBFFL_AFL_REPLAY_EVIDENCE_PATH",
    "BBBFFL_TEAMS_CONFIG_PATH",
    "BBBFFL_SUPERSCORE_CONFIG_PATH",
    "BBBFFL_POLL_INTERVAL_SECONDS",
    "BBBFFL_LOG_LEVEL",
    "AFL_API_BASE_URL",
    "AFL_API_KEY",
    "AFL_API_CONTRACT_VERSION",
    "AFL_API_TIMEOUT_SECONDS",
    "AFL_API_CONNECT_TIMEOUT_SECONDS",
    "AFL_API_READ_TIMEOUT_SECONDS",
    "AFL_API_RETRY_MAX_ATTEMPTS",
    "AFL_API_RETRY_BASE_DELAY_SECONDS",
    "AFL_API_RETRY_MAX_DELAY_SECONDS",
)


@pytest.fixture
def clean_env(monkeypatch):
    """A blank slate: every settings environment variable unset."""
    for name in _ALL_SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _set_valid_production_env(monkeypatch):
    """A complete, valid production-like configuration. Individual tests
    override or delete one variable from this baseline to prove that
    specific requirement fails closed."""
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "production")
    monkeypatch.setenv("BBBFFL_DATABASE_URL", "postgresql+psycopg://bbbffl:s3cret@db.internal/bbbffl")
    monkeypatch.setenv("BBBFFL_PUBLIC_BASE_URL", "https://bbbffl.example.com")
    monkeypatch.setenv("BBBFFL_ADMIN_TOKEN", "a-real-production-admin-token")
    monkeypatch.setenv("AFL_API_BASE_URL", "https://afl-api.example.net")
    monkeypatch.setenv("AFL_API_KEY", "a-real-afl-api-key")


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


# -- Central validated settings boundary (issue #38) ------------------------


def test_development_default_is_permissive_with_nothing_set(clean_env):
    """The unchanged prototype behaviour: no environment variables at all
    still produces a usable, valid development configuration -- an
    unconfigured home-server checkout must keep working exactly as before."""
    settings = get_settings()

    assert settings.environment == "development"
    assert settings.is_production is False
    assert settings.admin_token is None
    assert settings.public_base_url is None
    assert settings.afl_api_base_url == "http://localhost:8000"
    assert settings.database_url == f"sqlite:///{settings.database_path}"
    assert settings.afl_mode == "live"
    assert settings.afl_api_contract_version == "v1"


def test_valid_production_like_configuration_succeeds(clean_env):
    """Acceptance criterion: production startup succeeds when every
    required setting is present and valid."""
    _set_valid_production_env(clean_env)

    settings = get_settings()

    assert settings.environment == "production"
    assert settings.is_production is True
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.public_base_url == "https://bbbffl.example.com"
    assert settings.admin_token == "a-real-production-admin-token"
    assert settings.afl_api_base_url == "https://afl-api.example.net"
    assert settings.afl_mode == "live"


def test_production_refuses_missing_admin_token(clean_env):
    """Acceptance criterion: production startup refuses a missing required
    admin/session secret."""
    _set_valid_production_env(clean_env)
    clean_env.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("BBBFFL_ADMIN_TOKEN" in e for e in excinfo.value.errors)
    # Never invent/echo a secret value, even in the error path.
    assert "a-real-production-admin-token" not in str(excinfo.value)


def test_development_admin_token_default_does_not_satisfy_production(clean_env):
    """Design constraint: a development-safe default (no token, open admin
    interface) must never accidentally satisfy production's requirement.
    Setting only BBBFFL_ENVIRONMENT=production, with every other variable
    left at its development-style default/unset, must still fail closed on
    the missing admin token specifically."""
    clean_env.setenv("BBBFFL_ENVIRONMENT", "production")
    clean_env.setenv("BBBFFL_DATABASE_URL", "postgresql+psycopg://bbbffl:s3cret@db.internal/bbbffl")
    clean_env.setenv("BBBFFL_PUBLIC_BASE_URL", "https://bbbffl.example.com")
    clean_env.setenv("AFL_API_BASE_URL", "https://afl-api.example.net")
    clean_env.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("BBBFFL_ADMIN_TOKEN" in e for e in excinfo.value.errors)


def test_production_refuses_missing_database_url(clean_env):
    _set_valid_production_env(clean_env)
    clean_env.delenv("BBBFFL_DATABASE_URL", raising=False)
    clean_env.delenv("BBBFFL_DB_PATH", raising=False)

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("BBBFFL_DATABASE_URL" in e for e in excinfo.value.errors)


def test_invalid_database_url_is_rejected_regardless_of_environment(clean_env):
    """Acceptance criterion: an invalid database URL fails with an
    actionable, non-secret error -- proven here in development so the
    check is clearly about URL validity, not just production strictness."""
    clean_env.setenv("BBBFFL_DATABASE_URL", "not a valid url")

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("BBBFFL_DATABASE_URL" in e for e in excinfo.value.errors)


def test_production_refuses_sqlite_database_url(clean_env):
    """PostgreSQL is the only supported production database
    (docs/database-migrations.md); SQLite remains development/test/replay
    only."""
    _set_valid_production_env(clean_env)
    clean_env.setenv("BBBFFL_DATABASE_URL", "sqlite:////data/prod.db")

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("BBBFFL_DATABASE_URL" in e and "PostgreSQL" in e for e in excinfo.value.errors)


def test_production_refuses_missing_public_base_url(clean_env):
    _set_valid_production_env(clean_env)
    clean_env.delenv("BBBFFL_PUBLIC_BASE_URL", raising=False)

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("BBBFFL_PUBLIC_BASE_URL" in e for e in excinfo.value.errors)


def test_invalid_public_base_url_is_rejected(clean_env):
    """Acceptance criterion: invalid public/base application URL fails with
    an actionable error."""
    clean_env.setenv("BBBFFL_PUBLIC_BASE_URL", "not-a-url")

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("BBBFFL_PUBLIC_BASE_URL" in e for e in excinfo.value.errors)


def test_production_refuses_missing_afl_api_base_url(clean_env):
    _set_valid_production_env(clean_env)
    clean_env.delenv("AFL_API_BASE_URL", raising=False)

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("AFL_API_BASE_URL" in e for e in excinfo.value.errors)


def test_invalid_afl_api_endpoint_is_rejected(clean_env):
    """Acceptance criterion: invalid AFL API endpoint configuration fails
    with an actionable, non-secret error."""
    clean_env.setenv("AFL_API_BASE_URL", "ftp://not-http-or-https")

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("AFL_API_BASE_URL" in e for e in excinfo.value.errors)


def test_development_afl_api_default_does_not_satisfy_production(clean_env):
    """Design constraint: the development localhost default must never
    accidentally satisfy production's requirement that AFL_API_BASE_URL be
    explicitly configured."""
    clean_env.setenv("BBBFFL_ENVIRONMENT", "production")
    _set_valid_production_env(clean_env)
    clean_env.delenv("AFL_API_BASE_URL", raising=False)

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    errors = "\n".join(excinfo.value.errors)
    assert "AFL_API_BASE_URL" in errors
    assert "http://localhost:8000" not in errors


def test_afl_api_contract_version_defaults_to_v1(clean_env):
    settings = get_settings()

    assert settings.afl_api_contract_version == "v1"


def test_explicit_afl_api_contract_version_is_accepted(clean_env):
    """Acceptance criterion: the expected AFL API contract/version is
    explicit and validated -- a supported value is accepted and threaded
    through to the settings object AflApiClient is built from."""
    clean_env.setenv("AFL_API_CONTRACT_VERSION", "v1")

    settings = get_settings()

    assert settings.afl_api_contract_version == "v1"


def test_unsupported_afl_api_contract_version_is_rejected(clean_env):
    clean_env.setenv("AFL_API_CONTRACT_VERSION", "v2")

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("AFL_API_CONTRACT_VERSION" in e for e in excinfo.value.errors)


def test_afl_client_builds_request_paths_from_configured_contract_version():
    """The contract version is not just validated and ignored -- it drives
    the actual request paths AflApiClient builds."""
    import httpx

    from app.afl_client import AflApiClient

    seen_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(200, json={"seasons": []})

    client = AflApiClient(base_url="http://afl-api.test", contract_version="v1")
    client._client = httpx.Client(base_url="http://afl-api.test", transport=httpx.MockTransport(handler))
    try:
        client._get("/api/v1/seasons")
    finally:
        client.close()

    assert seen_paths == ["/api/v1/seasons"]


def test_replay_mode_requires_explicit_evidence_path(clean_env):
    """Acceptance criterion: replay/test/live modes cannot be confused
    through implicit fallback -- declaring replay mode without a
    deterministic evidence path must never silently fall back to live
    afl-api access."""
    clean_env.setenv("BBBFFL_AFL_MODE", "replay")
    clean_env.delenv("BBBFFL_AFL_REPLAY_EVIDENCE_PATH", raising=False)

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("BBBFFL_AFL_REPLAY_EVIDENCE_PATH" in e for e in excinfo.value.errors)


def test_replay_mode_with_explicit_evidence_path_succeeds(clean_env):
    clean_env.setenv("BBBFFL_AFL_MODE", "replay")
    clean_env.setenv("BBBFFL_AFL_REPLAY_EVIDENCE_PATH", "/data/replay/2026-round-1")

    settings = get_settings()

    assert settings.afl_mode == "replay"
    assert settings.afl_replay_evidence_path == "/data/replay/2026-round-1"


def test_replay_mode_is_refused_in_production_even_with_evidence_path(clean_env):
    """Production must always use live afl-api access; replay is a
    development/test/replay-harness concept only."""
    _set_valid_production_env(clean_env)
    clean_env.setenv("BBBFFL_AFL_MODE", "replay")
    clean_env.setenv("BBBFFL_AFL_REPLAY_EVIDENCE_PATH", "/data/replay/2026-round-1")

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("BBBFFL_AFL_MODE" in e for e in excinfo.value.errors)


def test_unknown_afl_mode_is_rejected(clean_env):
    clean_env.setenv("BBBFFL_AFL_MODE", "sandbox")

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("BBBFFL_AFL_MODE" in e for e in excinfo.value.errors)


def test_unknown_environment_is_rejected(clean_env):
    clean_env.setenv("BBBFFL_ENVIRONMENT", "staging")

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("BBBFFL_ENVIRONMENT" in e for e in excinfo.value.errors)


def test_test_environment_keeps_development_style_permissive_defaults(clean_env):
    """The 'test' environment is a distinct declared value for
    identification purposes, but must not itself impose production-style
    requirements -- CI/automated test runs must keep working unchanged."""
    clean_env.setenv("BBBFFL_ENVIRONMENT", "test")

    settings = get_settings()

    assert settings.environment == "test"
    assert settings.is_production is False
    assert settings.admin_token is None
    assert settings.database_url.startswith("sqlite:///")


def test_settings_error_message_never_contains_secret_values(clean_env):
    """Never log/expose secret values in validation errors -- even when a
    secret *is* configured, an unrelated validation failure's error text
    must not leak it."""
    _set_valid_production_env(clean_env)
    clean_env.setenv("BBBFFL_DATABASE_URL", "not a valid url")

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    message = str(excinfo.value)
    assert "a-real-production-admin-token" not in message
    assert "a-real-afl-api-key" not in message
    assert "s3cret" not in message


def test_afl_api_base_url_with_embedded_userinfo_credentials_is_rejected(clean_env):
    """Code review finding (PR #49): a URL with embedded userinfo
    (`https://user:pass@host`) would otherwise pass URL-format validation
    and then be written verbatim to app/main.py's startup log line,
    leaking the credential -- afl-api authentication has a dedicated
    channel (AFL_API_KEY) and must never be smuggled through the base
    URL."""
    clean_env.setenv("AFL_API_BASE_URL", "https://user:s3cret@afl-api.example.net")

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("AFL_API_BASE_URL" in e for e in excinfo.value.errors)
    assert "s3cret" not in str(excinfo.value)


def test_public_base_url_with_embedded_userinfo_credentials_is_rejected(clean_env):
    clean_env.setenv("BBBFFL_PUBLIC_BASE_URL", "https://user:s3cret@bbbffl.example.com")

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("BBBFFL_PUBLIC_BASE_URL" in e for e in excinfo.value.errors)


def test_afl_api_base_url_with_malformed_port_is_rejected(clean_env):
    """Code review finding (PR #49): urlsplit() alone accepts a
    non-numeric port syntactically; without an explicit check this would
    only fail later inside httpx, well after settings validation claimed
    success and after migrations had already run."""
    clean_env.setenv("AFL_API_BASE_URL", "https://afl-api.example.net:notaport")

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("AFL_API_BASE_URL" in e for e in excinfo.value.errors)


def test_database_url_with_unsupported_postgresql_like_scheme_is_rejected(clean_env):
    """Code review finding (PR #49): `scheme.startswith("postgresql")`
    would also accept a scheme like "postgresqlfoo" that is not a real
    SQLAlchemy dialect, deferring the failure to a confusing SQLAlchemy
    error during migration instead of the settings boundary naming the
    invalid variable."""
    clean_env.setenv("BBBFFL_DATABASE_URL", "postgresqlfoo://db.internal/bbbffl")

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    assert any("BBBFFL_DATABASE_URL" in e for e in excinfo.value.errors)


def test_multiple_failures_are_all_reported_together(clean_env):
    """A production start with several problems at once should report all
    of them, not just the first, so an operator can fix everything in one
    pass rather than one failed restart at a time."""
    clean_env.setenv("BBBFFL_ENVIRONMENT", "production")
    # Deliberately leave BBBFFL_DATABASE_URL, BBBFFL_PUBLIC_BASE_URL,
    # BBBFFL_ADMIN_TOKEN and AFL_API_BASE_URL all unset.

    with pytest.raises(SettingsError) as excinfo:
        get_settings()

    joined = "\n".join(excinfo.value.errors)
    for name in ("BBBFFL_DATABASE_URL", "BBBFFL_PUBLIC_BASE_URL", "BBBFFL_ADMIN_TOKEN", "AFL_API_BASE_URL"):
        assert name in joined
