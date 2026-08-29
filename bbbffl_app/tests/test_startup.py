"""Startup selection of hermetic replay versus the unchanged live client."""

from pathlib import Path

from fastapi.testclient import TestClient

FIXTURE = Path(__file__).parent / "fixtures" / "replay_round_2026" / "evidence.json"
TEAMS = Path(__file__).parent.parent / "data" / "grand_final_teams.json"


def test_replay_mode_starts_without_constructing_or_calling_live_client(tmp_path, monkeypatch):
    import app.main as main_module

    monkeypatch.setenv("BBBFFL_DB_PATH", str(tmp_path / "replay.db"))
    monkeypatch.setenv("BBBFFL_TEAMS_CONFIG_PATH", str(TEAMS))
    monkeypatch.setenv("BBBFFL_AFL_MODE", "replay")
    monkeypatch.setenv("BBBFFL_AFL_REPLAY_EVIDENCE_PATH", str(FIXTURE))

    def forbidden(*args, **kwargs):
        raise AssertionError("live AflApiClient constructed in replay mode")

    monkeypatch.setattr(main_module, "AflApiClient", forbidden)
    with TestClient(main_module.app) as client:
        assert main_module.app.state.afl_client.get_matches(1344)[0].match_id == 2601
        assert client.get("/health").status_code == 200


def test_live_mode_is_unaffected_and_still_starts_normally(tmp_path, monkeypatch):
    from app.main import app

    monkeypatch.setenv("BBBFFL_DB_PATH", str(tmp_path / "live.db"))
    monkeypatch.setenv("BBBFFL_TEAMS_CONFIG_PATH", str(TEAMS))
    monkeypatch.setenv("AFL_API_BASE_URL", "http://unused.invalid")
    monkeypatch.delenv("BBBFFL_AFL_MODE", raising=False)
    monkeypatch.delenv("BBBFFL_AFL_REPLAY_EVIDENCE_PATH", raising=False)
    with TestClient(app) as client:
        assert app.state.settings.afl_mode == "live"
        assert client.get("/health").status_code == 200
