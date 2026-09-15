"""Issue #208: executes the real `app/templates/scorer_dashboard.html`
finals-week rendering functions under Node against realistic dashboard
payloads built from `app.finals_superscore_dashboard.build_finals_week_
dashboard` (the exact shape the browser receives from `/api/scorer/
dashboard`), proving `renderFinalsWeekDashboard` runs without throwing --
never a separately maintained copy of the script, mirroring
`tests/test_round_centre_client_requests.py`'s established convention.
Skipped, not failed, when Node isn't available."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.encoders import jsonable_encoder

from app.audit import ActorContext
from app.coach_lineup import CoachLineupService
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.finals import FinalsBracketRepository
from app.finals_superscore_dashboard import build_finals_week_dashboard
from app.identity import IdentityRepository
from app.round_review import RoundReviewRepository
from app.superscore_results import SuperScoreCalculationService
from tests.finals_helpers import accept_week_mapping, build_finals_ready_season
from tests.test_coach_lineup_finals_superscore import _coach_id, _StubAflClient
from tests.test_scorer_dashboard_finals_superscore import _open_finals_week1_and_superscore1

_ACTOR = ActorContext.anonymous_operator("test")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not available")

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "app" / "templates" / "scorer_dashboard.html"


def _extract_script() -> str:
    html = TEMPLATE_PATH.read_text()
    match = re.search(r"<script>(.*)</script>", html, re.S)
    assert match, "inline <script> not found in scorer_dashboard.html"
    script = (
        match.group(1)
        .replace("{{ csrf_token|tojson }}", '"test-token"')
        .replace("{{ scorer_dashboard_roles|tojson }}", '["scorer"]')
    )
    # Strip the script's own auto-invoking `load();` call at the end -- this
    # harness drives `renderFinalsWeekDashboard` directly against a
    # controlled payload, never the real page's own async fetch/DOM cycle
    # (mirrors `tests/test_round_centre_client_requests.py`'s extraction of
    # only the specific functions under test).
    assert script.rstrip().endswith("load();"), "expected the script to end with its own load(); call"
    return script.rstrip().removesuffix("load();")


def _render_or_fail(payload: dict, tmp_path: Path, name: str) -> None:
    script = _extract_script()
    harness = f"""
globalThis.document = {{
  querySelector: () => ({{ innerHTML: '', addEventListener: () => {{}} }}),
  querySelectorAll: () => [],
  getElementById: () => null,
}};
globalThis.window = {{ location: {{ href: 'http://test/scorer', search: '' }} }};
globalThis.URLSearchParams = URLSearchParams;
globalThis.URL = URL;
globalThis.history = {{ replaceState: () => {{}} }};
globalThis.fetch = async () => ({{ ok: true, json: async () => ({{}}) }});
{script}
const dashboard = {json.dumps(payload)};
renderFinalsWeekDashboard(dashboard);
console.log('RENDER_OK');
"""
    script_path = tmp_path / f"{name}.js"
    script_path.write_text(harness)
    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "RENDER_OK" in result.stdout


def _submit_and_calculate_all_superscore_entries(built: dict) -> None:
    """Every entry submits a (valid, fully-vacant) SuperScore 1 lineup and
    the round is calculated -- so the rendered payload's per-entry
    `effective_entry` (slots *and* interchange) is populated, exercising
    the interchange DNP/assignment controls the dashboard renders (issue
    #208 review: these were previously never rendered for SuperScore at
    all)."""
    database = built["database"]
    service = CoachLineupService(database, afl_client=_StubAflClient())
    for entry_obj in built["entries"]:
        coach_id = _coach_id(database, entry_obj.season_entry_id)
        entry = service.resolve(coach_id, built["season"].season_id, built["ss1_round_id"])
        draft = service.ensure_draft(built["season"].season_id, built["ss1_round_id"], entry)
        service.submit(draft, submission_version=0, coach_id=coach_id)
    SuperScoreCalculationService(database, _StubAflClient()).calculate_round(built["ss1_round_id"])


def _real_dashboard_payload(year: int) -> dict:
    built = _open_finals_week1_and_superscore1(year=year)
    _submit_and_calculate_all_superscore_entries(built)
    database = built["database"]
    dashboard = build_finals_week_dashboard(
        database,
        CompetitionLifecycleRepository(database),
        IdentityRepository(database),
        RoundReviewRepository(database),
        _StubAflClient(),
        built["season"],
        built["week1_round_id"],
    )
    return jsonable_encoder(dashboard)


def test_render_finals_week_dashboard_does_not_throw_for_an_open_week_with_calculated_superscore_entries(tmp_path):
    payload = _real_dashboard_payload(9701)
    # Confirms the fixture actually exercises the interchange rendering
    # path this test is meant to cover -- not merely the "not yet
    # calculated" placeholder.
    assert any(entry["effective_entry"] is not None for entry in payload["superscore"]["entries"])
    assert all("interchange" in entry["effective_entry"] for entry in payload["superscore"]["entries"])
    _render_or_fail(payload, tmp_path, "harness_open")


def test_render_finals_week_dashboard_does_not_throw_before_the_week_has_opened(tmp_path):
    """The `!finals.available`-style branches (preflight not yet satisfied,
    SuperScore not yet configured for this week) are real, reachable states
    -- exercise them too, not just an already-open week."""
    built = build_finals_ready_season(year=9702)
    database = built["database"]
    repo = FinalsBracketRepository(database)
    bracket = repo.create_bracket(
        built["season"].season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=_ACTOR,
        reason="not-yet-opened dashboard render test",
    )["bracket"]
    week1_round_id = repo.get_week_round_id(bracket.bracket_id, 1)
    accept_week_mapping(database, week1_round_id, year=9702, afl_round_id=9001)

    dashboard = build_finals_week_dashboard(
        database,
        CompetitionLifecycleRepository(database),
        IdentityRepository(database),
        RoundReviewRepository(database),
        _StubAflClient(),
        built["season"],
        week1_round_id,
    )
    payload = jsonable_encoder(dashboard)
    assert payload["finals"]["lifecycle_state"] == "not_created"
    assert payload["superscore"]["available"] is False
    _render_or_fail(payload, tmp_path, "harness_unopened")
