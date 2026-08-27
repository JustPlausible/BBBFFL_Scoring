"""Regression coverage for issue #38's fail-closed startup guarantee that
declaring `BBBFFL_AFL_MODE=replay` can never fall through to constructing
the live `AflApiClient`.

This exercises the real FastAPI app (`app/main.py`'s `lifespan`), not just
`app/config.py`'s `get_settings()` in isolation -- `get_settings()` alone
accepts a well-formed "replay" declaration as valid configuration (see
`tests/test_config.py`'s `test_replay_mode_with_explicit_evidence_path_succeeds`).
The invariant this file protects is a level up: no replay-backed
`AflDataSource` exists in this codebase yet (roadmap package 32), so the
application itself must refuse to start with "replay" rather than quietly
proceeding to build a live client anyway.
"""

import os

import pytest
from fastapi.testclient import TestClient

from app.main import ReplayModeNotWiredError

TEAMS_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "grand_final_teams.json")


@pytest.fixture
def replay_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BBBFFL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("BBBFFL_TEAMS_CONFIG_PATH", TEAMS_CONFIG_PATH)
    monkeypatch.setenv("BBBFFL_AFL_MODE", "replay")
    monkeypatch.setenv("BBBFFL_AFL_REPLAY_EVIDENCE_PATH", str(tmp_path / "replay-evidence"))
    monkeypatch.delenv("BBBFFL_ENVIRONMENT", raising=False)  # stays "development"


def test_replay_mode_refuses_to_start_the_app(replay_env):
    from app.main import app

    with pytest.raises(ReplayModeNotWiredError) as excinfo:
        with TestClient(app):
            pass

    message = str(excinfo.value)
    assert "BBBFFL_AFL_MODE" in message
    assert "replay" in message
    assert "live afl-api access" in message


def test_replay_mode_never_constructs_the_live_afl_client(replay_env, monkeypatch):
    """The important invariant per issue #38: declaring replay must make
    live AFL access impossible, not merely undesired. Proven directly by
    making construction itself fail loudly if it is ever reached."""
    import app.main as main_module

    def _fail_if_constructed(*args, **kwargs):
        raise AssertionError("AflApiClient must never be constructed when BBBFFL_AFL_MODE=replay")

    monkeypatch.setattr(main_module, "AflApiClient", _fail_if_constructed)

    with pytest.raises(ReplayModeNotWiredError):
        with TestClient(main_module.app):
            pass


def test_replay_mode_never_reaches_database_migration(replay_env, monkeypatch):
    """The application must not partially start either: refusing before
    AflApiClient construction is not enough on its own if migrations (and
    thus a real database connection) already ran first."""
    import app.main as main_module

    def _fail_if_migrated(*args, **kwargs):
        raise AssertionError("migrate() must not run when BBBFFL_AFL_MODE=replay is refused")

    monkeypatch.setattr(main_module, "migrate", _fail_if_migrated)

    with pytest.raises(ReplayModeNotWiredError):
        with TestClient(main_module.app):
            pass


def test_live_mode_is_unaffected_and_still_starts_normally(tmp_path, monkeypatch):
    """Regression guard: the replay refusal must not accidentally catch
    the default "live" mode too."""
    monkeypatch.setenv("BBBFFL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("BBBFFL_TEAMS_CONFIG_PATH", TEAMS_CONFIG_PATH)
    monkeypatch.setenv("AFL_API_BASE_URL", "http://unused.invalid")
    monkeypatch.delenv("BBBFFL_AFL_MODE", raising=False)
    monkeypatch.delenv("BBBFFL_AFL_REPLAY_EVIDENCE_PATH", raising=False)

    from app.main import app

    with TestClient(app) as client:
        assert app.state.settings.afl_mode == "live"
        r = client.get("/health")
        assert r.status_code == 200
