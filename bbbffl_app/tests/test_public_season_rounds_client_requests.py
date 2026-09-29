"""Issue #261: executes the real `app/templates/public_season_rounds.html`
presentation functions (`player`, `finalsMatchCard`/`scoreCell`,
`superscoreSection`) under Node against synthetic DTOs shaped exactly like
`app.public_rounds._side`/`app.public_finals.build_public_superscore_round`
emit, proving the rendered markup matches issue #261's acceptance examples
-- never a separately maintained copy of the script, mirroring
`tests/test_round_centre_client_requests.py`'s established convention.
Skipped, not failed, when Node isn't available.
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
    shutil.which("node") is None, reason="Node.js is not available to execute the public season rounds client script"
)


def test_superscore_player_detail_stacks_at_the_mobile_breakpoint():
    """The five detail fields must not retain the desktop five-column grid
    on the common 320--375px viewport range (PR #264 review)."""
    template = (Path(__file__).parents[1] / "app" / "templates" / "public_season_rounds.html").read_text(
        encoding="utf-8"
    )
    mobile = template[template.index("@media(max-width:640px)") : template.index("</style>")]

    assert ".superscore-player{grid-template-columns:minmax(0,1fr) auto" in mobile
    assert ".superscore-player>strong,.superscore-player>span:nth-child(2)" in mobile
    assert ".superscore-player>span:nth-child(5){grid-column:1/-1" in mobile
    assert "overflow-wrap:anywhere" in mobile
    assert ".superscore-detail{padding:0 0 12px}" in mobile


@pytest.fixture
def rendered_script(monkeypatch):
    """The literal inline `<script>` from a real, server-rendered
    `/seasons/{id}/rounds/{n}` page -- never a separately maintained copy."""
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app
    from tests.test_competition_lifecycle import operational

    with TestClient(app) as client:
        _, _, entries = operational(client.app.state.database, 9200)
        season_id = entries[0].season_id
        page = client.get(f"/seasons/{season_id}/rounds/1")
        assert page.status_code == 200
        html = page.text
    db_path.unlink(missing_ok=True)

    match = re.search(r"<script>(.*)</script>", html, re.S)
    assert match, "inline <script> not found in public_season_rounds.html"
    script = match.group(1)
    # Strip the script's own auto-invoking `render()` call and the trailing
    # poll-timer wiring -- this harness calls the specific rendering
    # functions directly against controlled payloads, never the real
    # page's own async fetch/DOM cycle (mirrors
    # tests/test_scorer_dashboard_finals_week_client_requests.py's
    # extraction convention).
    marker = "render();"
    assert marker in script, "expected the script to call render() once"
    return script[: script.index(marker)]


def _run(script_body: str, tmp_path: Path, name: str) -> str:
    # The script's top-level `document.querySelector('#roundSelect').
    # addEventListener(...)` call runs immediately (it isn't inside
    # `render()`), so `document` needs a minimal stub even though this
    # harness never actually calls that listener.
    harness = "global.document = { querySelector: () => ({ addEventListener: () => {} }) };\n" + script_body
    script_path = tmp_path / f"{name}.js"
    script_path.write_text(harness, encoding="utf-8")
    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


# -- Requirement 2: finals matchup totals as "G.B (Total)" -------------------


def test_finals_scoreline_renders_the_2026_grand_final_acceptance_example(rendered_script, tmp_path):
    running_hots = {"official_score": 158, "calculated_score": None, "football_line": "24.14"}
    evil_absolutes = {"official_score": 240, "calculated_score": None, "football_line": "38.12"}
    script = f"""
{rendered_script}
console.log(scoreCell({json.dumps(running_hots)}));
console.log(scoreCell({json.dumps(evil_absolutes)}));
"""
    stdout = _run(script, tmp_path, "finals_scoreline")
    lines = [re.sub(r"<[^>]+>", "", line) for line in stdout.strip().splitlines()]
    assert lines[0] == "24.14 (158)"
    assert lines[1] == "38.12 (240)"


def test_scoreline_falls_back_to_the_bare_total_with_no_football_line(rendered_script, tmp_path):
    side = {"official_score": 100, "calculated_score": None, "football_line": None}
    script = f"""
{rendered_script}
console.log(scoreCell({json.dumps(side)}));
"""
    stdout = _run(script, tmp_path, "finals_scoreline_fallback")
    assert stdout.strip() == "100"


def test_ordinary_match_card_also_renders_goals_behinds_total(rendered_script, tmp_path):
    """Issue #261 scope extension: `matchCard` (the ordinary Round 1-20
    card, not just `finalsMatchCard`) now shows the same "G.B (Total)"
    presentation via the identical shared `scoreCell` -- never a second
    conversion path for ordinary rounds."""
    match = {
        "status": "official",
        "status_label": "Official final",
        "order": 1,
        "published_at": None,
        "home": {
            "team": {"name": "Home Team"},
            "official_score": 158,
            "calculated_score": None,
            "football_line": "24.14",
        },
        "away": {
            "team": {"name": "Away Team"},
            "official_score": 96,
            "calculated_score": None,
            "football_line": "16.0",
        },
    }
    script = f"""
{rendered_script}
console.log(matchCard({json.dumps(match)}, null));
"""
    stdout = re.sub(r"<[^>]+>", "|", _run(script, tmp_path, "ordinary_match_card").strip())
    assert "24.14" in stdout and "(158)" in stdout
    assert "16.0" in stdout and "(96)" in stdout


# -- Requirement 3: DNP/interchange replacement clarity ----------------------


def test_player_row_names_original_dnp_selection_and_interchange_replacement(rendered_script, tmp_path):
    slot = {
        "position": "Tackler",
        "player_name": "Original Tackler",
        "effective_score": 48,
        "outcome": "replaced_by_interchange",
        "confirmed_dnp": True,
        "interchange_player_name": "Matt Rowell",
        "deferred_source": None,
    }
    script = f"""
{rendered_script}
console.log(player({json.dumps(slot)}));
"""
    stdout = _run(script, tmp_path, "player_dnp_interchange").strip()
    assert "Original Tackler" in stdout
    assert "DNP" in stdout
    assert "Matt Rowell" in stdout
    assert "48" in stdout
    assert "Vacant" not in stdout


def test_player_row_names_interchange_replacement_over_a_genuine_vacancy(rendered_script, tmp_path):
    """A position nobody ever selected, now covered by an Interchange --
    `player_name` is genuinely `None`, but the row must still name the
    interchange player rather than reading as a bare, unexplained score
    against "Vacant"."""
    slot = {
        "position": "Tackler",
        "player_name": None,
        "effective_score": 48,
        "outcome": "replaced_by_interchange",
        "confirmed_dnp": False,
        "interchange_player_name": "Matt Rowell",
        "deferred_source": None,
    }
    script = f"""
{rendered_script}
console.log(player({json.dumps(slot)}));
"""
    stdout = _run(script, tmp_path, "player_vacancy_interchange").strip()
    assert "Matt Rowell" in stdout
    assert "48" in stdout
    assert "Vacant" in stdout  # accurate: no one was ever selected here
    assert "replaced by Matt Rowell" in stdout


def test_ordinary_dnp_zero_and_scored_rows_are_unaffected(rendered_script, tmp_path):
    """Requirement 6: the existing confirmed-DNP-scored-zero and plain
    scored rows keep their established wording."""
    scored = {
        "position": "F1",
        "player_name": "Some Forward",
        "effective_score": 24,
        "outcome": "scored",
        "confirmed_dnp": False,
        "interchange_player_name": None,
        "deferred_source": None,
    }
    dnp_zero = {
        "position": "M1",
        "player_name": "Withdrew Player",
        "effective_score": 0,
        "outcome": "confirmed_dnp_zero",
        "confirmed_dnp": True,
        "interchange_player_name": None,
        "deferred_source": None,
    }
    script = f"""
{rendered_script}
console.log(player({json.dumps(scored)}));
console.log(player({json.dumps(dnp_zero)}));
"""
    lines = _run(script, tmp_path, "player_unaffected").strip().splitlines()
    assert "Some Forward" in lines[0]
    assert "24" in lines[0]
    assert "Withdrew Player" in lines[1]
    assert "Confirmed DNP" in lines[1]


def test_player_row_shows_the_per_position_football_line(rendered_script, tmp_path):
    """Issue #261 scope extension: individual positional presentation --
    `player()` shows the position's own "G.B (Total)" line
    (`p.football_line`, from `app.public_rounds._slot`), not just the bare
    effective score."""
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
    stdout = _run(script, tmp_path, "player_position_football_line").strip()
    assert "1.8" in stdout
    assert "(14)" in stdout
    assert "2.2" not in stdout  # the naive divmod approximation must never appear


def test_player_row_omits_the_line_when_no_calculation_exists_yet(rendered_script, tmp_path):
    """A slot with no calculation (`football_line` is `null`, mirroring
    `app.public_rounds._slot`'s `calculated is None` branch) falls back to
    the bare effective score exactly as before this scope extension."""
    slot = {
        "position": "F1",
        "player_name": "Some Forward",
        "effective_score": None,
        "outcome": "awaiting_score",
        "confirmed_dnp": False,
        "interchange_player_name": None,
        "football_line": None,
        "deferred_source": None,
    }
    script = f"""
{rendered_script}
console.log(player({json.dumps(slot)}));
"""
    stdout = _run(script, tmp_path, "player_no_football_line").strip()
    assert "Some Forward" in stdout


# -- Requirement 1: SuperScore integral totals -------------------------------


def test_superscore_table_shows_integral_totals_without_a_trailing_zero(rendered_script, tmp_path):
    ss = {
        "available": True,
        "published": True,
        "published_at": None,
        "round_label": "SuperScore 4",
        "entries": [
            {"rank": 1, "is_joint_winner": False, "team_name": "Team A", "total_score": 271.0, "total_display": 271},
            {"rank": 2, "is_joint_winner": False, "team_name": "Team B", "total_score": 246.0, "total_display": 246},
            {
                "rank": 3,
                "is_joint_winner": False,
                "team_name": "Team C",
                "total_score": 227.5,
                "total_display": 227.5,
            },
        ],
    }
    script = f"""
{rendered_script}
console.log(superscoreSection({json.dumps(ss)}));
"""
    stdout = _run(script, tmp_path, "superscore_integral").strip()
    assert "(271)" in stdout
    assert "(246)" in stdout
    assert "(227.5)" in stdout
    assert "271.0" not in stdout
    assert "246.0" not in stdout


def test_superscore_entry_is_an_accessible_inline_expander_with_progress(rendered_script, tmp_path):
    entry = {
        "rank": 1,
        "season_entry_id": "entry-a",
        "team_name": "JHAS",
        "total_display": 12,
        "football_line": "2.0",
        "is_joint_winner": False,
        "positions": [
            {
                "slot": "F1",
                "label": "Forward 1",
                "player_name": "Mitch Lewis",
                "afl_club": "Hawthorn",
                "display_state": "completed",
                "effective_score": 12,
                "football_line": "2.0",
                "interchange_applied": False,
            }
        ],
        "interchange": {"player_name": None, "afl_club": None, "display_state": "vacant", "target_position": None},
    }
    script = f"""
{rendered_script}
expandedSuperScoreEntry='entry-a';
console.log(superScoreEntry({json.dumps(entry)}));
"""
    stdout = _run(script, tmp_path, "superscore_expander").strip()
    assert '<button type="button"' in stdout
    assert 'aria-expanded="true"' in stdout
    assert 'aria-controls="superscore-detail-entry-a"' in stdout
    assert 'class="superscore-dot completed"' in stdout
    assert "Mitch Lewis" in stdout
    assert "Hawthorn" in stdout
    assert "2.0" in stdout


def test_vacant_position_covered_by_interchange_is_not_mislabeled_dnp(rendered_script, tmp_path):
    position = {
        "label": "Forward 1",
        "player_name": None,
        "afl_club": None,
        "display_state": "completed",
        "effective_score": 12,
        "football_line": "2.0",
        "interchange_applied": True,
        "confirmed_dnp": False,
        "replacement_player_name": "Bench Player",
        "replacement_afl_club": "Hawthorn",
    }
    script = f"""
{rendered_script}
console.log(superScorePlayer({json.dumps(position)}));
"""
    stdout = _run(script, tmp_path, "superscore_vacant_interchange").strip()
    assert "Vacant" in stdout
    assert "replaced by Bench Player" in stdout
    assert "DNP" not in stdout
