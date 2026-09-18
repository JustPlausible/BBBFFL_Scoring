"""Issue #216: executes the real `app/templates/scorer_dashboard.html`
finals-week rendering functions under Node against realistic bracket-
progression and collapsed-SuperScore-action-required payloads, mirroring
tests/test_scorer_dashboard_finals_week_client_requests.py's established
harness convention. Skipped, not failed, when Node isn't available."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.encoders import jsonable_encoder

from app.competition_lifecycle import CompetitionLifecycleRepository
from app.finals import FinalsBracketRepository
from app.finals_superscore_dashboard import build_finals_week_dashboard
from app.identity import IdentityRepository
from app.round_review import RoundReviewRepository
from tests.finals_helpers import mark_finals_round_final
from tests.test_finals import _bracket_with_mappings, _open_and_seed_week1
from tests.test_scorer_dashboard_finals_superscore import _open_finals_week1_and_superscore1, _StubAflClient

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
    assert script.rstrip().endswith("load();"), "expected the script to end with its own load(); call"
    return script.rstrip().removesuffix("load();")


def _rendered_html(payload: dict, tmp_path: Path, name: str) -> str:
    """Like tests/test_scorer_dashboard_finals_week_client_requests.py's own
    `_rendered_html` -- captures and returns the HTML actually assigned to
    `#app`'s `innerHTML` so a test can inspect the rendered shape, not just
    that rendering didn't throw."""
    script = _extract_script()
    harness = f"""
const captured = {{}};
globalThis.document = {{
  querySelector: (sel) => {{
    if (!captured[sel]) captured[sel] = {{ addEventListener: () => {{}}, dataset: {{}} }};
    return captured[sel];
  }},
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
console.log('HTML_START');
console.log(captured['#app'].innerHTML);
console.log('HTML_END');
"""
    script_path = tmp_path / f"{name}.js"
    script_path.write_text(harness)
    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    stdout = result.stdout
    return stdout.split("HTML_START\n", 1)[1].rsplit("HTML_END", 1)[0]


def test_pairing_missing_progression_renders_a_preview_button_and_next_action_card(tmp_path):
    built, bracket = _bracket_with_mappings(year=2820)
    database = built["database"]
    _open_and_seed_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    week1_round_id = FinalsBracketRepository(database).get_week_round_id(bracket.bracket_id, 1)
    # Codex review (PR #217, P2): the progression cue only applies once the
    # source week is genuinely `final` -- mark it so here, matching the
    # domain's own `advance_bracket` precondition.
    mark_finals_round_final(database, week1_round_id)
    week2_round_id = FinalsBracketRepository(database).get_week_round_id(bracket.bracket_id, 2)

    dashboard = build_finals_week_dashboard(
        database,
        CompetitionLifecycleRepository(database),
        IdentityRepository(database),
        RoundReviewRepository(database),
        _StubAflClient(),
        built["season"],
        week2_round_id,
    )
    payload = jsonable_encoder(dashboard)
    assert payload["finals"]["progression"]["reason"] == "pairing_missing"
    assert payload["next_action"]["code"] == "finals_bracket_progression_required"

    html = _rendered_html(payload, tmp_path, "harness_progression_missing")
    assert "Next safe action" in html
    assert "Progress the bracket" in html
    assert "Preview bracket progression" in html
    # Not yet fetched -- the confirm/apply control must not appear until
    # the operator has explicitly requested and seen the preview.
    assert "Confirm and advance bracket" not in html


def test_pairing_missing_without_a_final_source_week_never_renders_a_progression_button(tmp_path):
    """Codex review (PR #217, P2): a progression preview/apply can only
    ever succeed once the source week is `final` -- while Finals Week 1 is
    still `open`, selecting Finals Week 2 (pairing missing) must not offer
    "Preview bracket progression" at all, only guidance to finish Week 1."""
    built, bracket = _bracket_with_mappings(year=2823)
    database = built["database"]
    _open_and_seed_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    week2_round_id = FinalsBracketRepository(database).get_week_round_id(bracket.bracket_id, 2)

    dashboard = build_finals_week_dashboard(
        database,
        CompetitionLifecycleRepository(database),
        IdentityRepository(database),
        RoundReviewRepository(database),
        _StubAflClient(),
        built["season"],
        week2_round_id,
    )
    payload = jsonable_encoder(dashboard)
    assert payload["finals"]["progression"] is None
    assert payload["finals"]["blocked_by_week"]["week_label"] == "Finals Week 1"
    assert payload["next_action"]["code"] == "finals_prior_week_incomplete"

    html = _rendered_html(payload, tmp_path, "harness_blocked_by_prior_week")
    assert "Preview bracket progression" not in html
    assert "Complete Finals Week 1 first" in html


def test_ready_to_progress_after_publication_renders_a_preview_button(tmp_path):
    built, bracket = _bracket_with_mappings(year=2822)
    database = built["database"]
    _open_and_seed_week1(built, bracket, qf_result=(100, 50), ef_result=(50, 100))
    week1_round_id = FinalsBracketRepository(database).get_week_round_id(bracket.bracket_id, 1)
    mark_finals_round_final(database, week1_round_id)

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
    assert payload["finals"]["progression"]["reason"] == "ready_to_progress"

    html = _rendered_html(payload, tmp_path, "harness_ready_to_progress")
    assert "Preview bracket progression" in html


def test_superscore_row_with_no_submission_shows_the_action_required_badge(tmp_path):
    built = _open_finals_week1_and_superscore1(year=2821)
    database = built["database"]
    dashboard = build_finals_week_dashboard(
        database,
        CompetitionLifecycleRepository(database),
        IdentityRepository(database),
        RoundReviewRepository(database),
        _StubAflClient(),
        built["season"],
        built["ss1_round_id"],
    )
    payload = jsonable_encoder(dashboard)
    assert all(e["action_required"] for e in payload["superscore"]["entries"])

    superscore_section = _rendered_html(payload, tmp_path, "harness_action_required").split(
        'id="superscore-heading"', 1
    )[1]
    assert "Action required" in superscore_section
    assert 'class="action-required"' in superscore_section
