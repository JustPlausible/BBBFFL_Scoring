"""Issue #261 scope extension: executes the real
`app/templates/public_round_centre.html` presentation functions
(`player`, `match`/`scoreCell`) under Node against synthetic DTOs shaped
exactly like `app.public_rounds._side`/`_slot` emit -- the ordinary
per-matchup detail page's own copy of the same football-score/DNP-
interchange presentation `public_season_rounds.html` carries, proven here
separately since the two templates keep independent inline scripts (no
shared JS module system in this codebase). Never a separately maintained
copy of the script, mirroring `tests/test_round_centre_client_requests.py`'s
established convention. Skipped, not failed, when Node isn't available.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is not available to execute the public round centre client script"
)


@pytest.fixture
def rendered_script(monkeypatch):
    """The literal inline `<script>` from a real, server-rendered
    `/seasons/{id}/rounds/{round_id}` page -- never a separately
    maintained copy."""
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app
    from tests.test_competition_lifecycle import operational

    with TestClient(app) as client:
        lifecycle, round_one, _ = operational(client.app.state.database, 9210)
        persisted_round = lifecycle.get_round(round_one.bbbffl_round_id)
        season_id = persisted_round.season_id
        page = client.get(f"/seasons/{season_id}/rounds/{round_one.bbbffl_round_id}")
        assert page.status_code == 200
        html = page.text
    db_path.unlink(missing_ok=True)

    match = re.search(r"<script>(.*)</script>", html, re.S)
    assert match, "inline <script> not found in public_round_centre.html"
    script = match.group(1)
    marker = "load();"
    assert marker in script, "expected the script to call load() once"
    return script[: script.index(marker)]


def _run(script_body: str, tmp_path: Path, name: str) -> str:
    script_path = tmp_path / f"{name}.js"
    script_path.write_text(script_body, encoding="utf-8")
    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_match_scoreline_renders_goals_behinds_total(rendered_script, tmp_path):
    match = {
        "order": 1,
        "status": "official",
        "status_label": "Official final",
        "published_at": None,
        "home": {"team": {"name": "Home"}, "official_score": 158, "calculated_score": None, "football_line": "24.14"},
        "away": {"team": {"name": "Away"}, "official_score": 240, "calculated_score": None, "football_line": "38.12"},
    }
    script = f"""
{rendered_script}
console.log(match({json.dumps(match)}));
"""
    stdout = re.sub(r"<[^>]+>", "|", _run(script, tmp_path, "round_centre_scoreline").strip())
    assert "24.14" in stdout and "(158)" in stdout
    assert "38.12" in stdout and "(240)" in stdout


def test_player_row_names_interchange_replacement_and_shows_position_line(rendered_script, tmp_path):
    slot = {
        "position": "Tackler",
        "player_name": "Original Tackler",
        "effective_score": 48,
        "outcome": "replaced_by_interchange",
        "confirmed_dnp": True,
        "interchange_player_name": "Matt Rowell",
        "football_line": "8.0",
        "deferred_source": None,
    }
    script = f"""
{rendered_script}
console.log(player({json.dumps(slot)}));
"""
    stdout = _run(script, tmp_path, "round_centre_player").strip()
    assert "Original Tackler" in stdout
    assert "DNP" in stdout
    assert "Matt Rowell" in stdout
    assert "8.0" in stdout and "(48)" in stdout
    assert "Vacant" not in stdout


def test_scored_row_is_unaffected(rendered_script, tmp_path):
    """Requirement 6: an ordinary scored row keeps working exactly as
    before, now additionally showing its own football line."""
    slot = {
        "position": "F1",
        "player_name": "Some Forward",
        "effective_score": 14,
        "outcome": "scored",
        "confirmed_dnp": False,
        "interchange_player_name": None,
        "football_line": "1.8",
        "deferred_source": None,
    }
    script = f"""
{rendered_script}
console.log(player({json.dumps(slot)}));
"""
    stdout = _run(script, tmp_path, "round_centre_player_scored").strip()
    assert "Some Forward" in stdout
    assert "1.8" in stdout and "(14)" in stdout
