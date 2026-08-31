"""Regression coverage for the Round Centre browser client's HTTP method
selection (`app/templates/round_centre.html`).

PR #86 review found a P1: `mutation()` used to delegate straight to
`api()`'s body-presence heuristic (`method: body ? 'POST' : 'GET'`), so a
mutation with no JSON body -- e.g. the "Calculate / refresh scores"
control, which calls `mutation(path)` with no second argument at all --
silently issued `GET` against a `POST`-only route (`app/routes/
round_review.py`'s `.../calculate`) and failed with HTTP 405, blocking the
documented Round 1 rehearsal (`bbbffl_app/README.md`'s "Round 1 rehearsal
quick start"). `mutation()` now passes an explicit method to `api()`
regardless of body presence.

This extracts exactly the `api`/`mutation` function definitions from the
real page the server renders (never a separately maintained copy, so it
can't drift from what's actually served) and executes them under Node
with a stubbed `fetch`, proving a body-less mutation call issues `POST`.
Skipped, not failed, when Node isn't available -- this is the one test in
the suite that isn't hermetic Python, so it must never become a required
dependency for the rest of the suite to stay green.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is not available to execute the Round Centre client script"
)


@pytest.fixture
def round_centre_client_functions(tmp_path, monkeypatch):
    """The literal `api`/`mutation` source lines from the real rendered
    `/scorer/round-centre` page."""
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        page = client.get("/scorer/round-centre")
        assert page.status_code == 200
        html = page.text

    api_match = re.search(r"^async function api\(.*\)\{.*\}$", html, re.MULTILINE)
    mutation_match = re.search(r"^function mutation\(.*\)\{.*\}$", html, re.MULTILINE)
    assert api_match, "api() function definition not found in the rendered Round Centre page"
    assert mutation_match, "mutation() function definition not found in the rendered Round Centre page"
    return api_match.group(0), mutation_match.group(0)


def test_mutation_issues_post_even_with_no_json_body(round_centre_client_functions, tmp_path):
    """The exact scenario that broke the browser rehearsal: calling
    `mutation(path)` with no body argument (as the Calculate control
    does) must still issue an HTTP POST, not fall back to GET."""
    api_source, mutation_source = round_centre_client_functions
    script = f"""
{api_source}
{mutation_source}
let recordedMethod = null;
let current = 'some-round-id';
function token() {{ return ''; }}
function loadRound() {{ return Promise.resolve(); }}
global.document = {{ querySelector: () => ({{ innerHTML: '' }}) }};
global.fetch = (path, options) => {{
  recordedMethod = options.method;
  return Promise.resolve({{ ok: true, json: async () => ({{}}) }});
}};
mutation('/api/admin/round-review/some-round-id/calculate')
  .then(() => {{ console.log(recordedMethod); process.exit(0); }})
  .catch((error) => {{ console.error(error); process.exit(1); }});
"""
    script_path = tmp_path / "round_centre_mutation_check.js"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "POST"


def test_api_only_emits_legacy_header_for_a_deliberate_non_empty_token(round_centre_client_functions):
    api_source, _ = round_centre_client_functions
    assert "if(legacyToken)headers['X-Admin-Token']=legacyToken" in api_source
    template = Path(__file__).parents[1] / "app" / "templates" / "round_centre.html"
    source = template.read_text(encoding="utf-8")
    assert ".trim()" in source
    assert "localStorage.removeItem('bbbffl_admin_token')" in source
    assert "'X-Admin-Token':token()" not in source
