"""Issue #237: the Season setup page's own browser script
(`app/templates/season_setup.html`), extracted from the real rendered page
and driven under Node with a stubbed DOM/fetch -- proving it renders every
setup step, builds the draft order from the operator's chosen positions
(team names on screen, entry ids only as hidden values), refuses an
incomplete/duplicate order client-side without calling the server, and
posts the reason the operator gave. Skipped, not failed, when Node isn't
available (see `test_round_centre_client_requests.py`)."""

import json
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not available")


@pytest.fixture
def setup_script(tmp_path, monkeypatch):
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)
    from app.main import app

    with TestClient(app) as client:
        page = client.get("/admin/season-setup/season-1")
        assert page.status_code == 200
    source = re.search(r"<script>([\s\S]*)</script>", page.text).group(1)
    initial = "api(`/api/admin/season-setup/${seasonId}`).then(render).catch(e=>say(e.message));"
    assert initial in source, "initial load call not found -- page script shape changed"
    return source.replace(initial, "")


def _step(key, status, **extra):
    return {
        "key": key,
        "title": key.replace("_", " ").title(),
        "status": status,
        "summary": f"{key} summary",
        "blockers": [],
        "warnings": [],
        "facts": {},
        "links": {},
        **extra,
    }


ENTRIES = [
    {"season_entry_id": f"entry-{n}", "team_name": f"Team {n}", "coach_display_name": f"Coach {n}"} for n in range(1, 4)
]
SETUP = {
    "season": {"season_id": "season-1", "year": 2027, "label": "2027 BBBFFL", "lifecycle_state": "setup"},
    "read_only": False,
    "next_step": "draft_order",
    "next_step_title": "Preseason draft order",
    "steps": [
        _step("entries", "complete", facts={"entries": ENTRIES}),
        _step("player_pool", "complete", facts={"total": 60}),
        _step("ordinary_competition", "complete", facts={"regular_season_round_count": 20}),
        _step("opening_round", "optional"),
        _step("squad_limit", "complete", facts={"squad_limit": 4, "frozen": False}),
        _step("draft_order", "available", facts={"order": []}, warnings=["check the Opening Round first"]),
        _step("fixture_draw", "action_needed", links={"fixture_setup": "/admin/fixture-setup/season-1"}),
        _step("finals", "blocked", blockers=["20 regular-season round(s) are not final yet"]),
        _step("superscore", "blocked", blockers=["initialize Finals first"]),
    ],
}


def _run(script, body):
    harness = f"""
const elements = {{}};
let selects = [];
function element(){{ return {{innerHTML:'', textContent:'', value:'', className:'', onclick:null, scrollIntoView(){{}}}}; }}
global.document = {{
  querySelector(sel){{ if (!elements[sel]) elements[sel] = element(); return elements[sel]; }},
  querySelectorAll(sel){{ return sel === 'select[data-entry]' ? selects : []; }},
}};
global.localStorage = {{ getItem(){{ return null; }} }};
global.confirm = () => true;
const calls = [];
global.fetch = async (path, init) => {{
  calls.push({{path, method: init.method, body: init.body ? JSON.parse(init.body) : null}});
  return {{ ok: true, json: async () => ({{result: {{created: true}}, setup: {json.dumps(SETUP)}}}) }};
}};
{script}
(async () => {{
{body}
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_render_shows_every_step_status_blocker_and_the_next_safe_action(setup_script):
    out = _run(
        setup_script,
        f"render({json.dumps(SETUP)}); console.log(JSON.stringify({{html: document.querySelector('#app').innerHTML}}));",
    )
    html = out["html"]
    assert "Next safe action:</strong> Preseason draft order" in html
    for step in SETUP["steps"]:
        assert step["title"] in html
    assert "20 regular-season round(s) are not final yet" in html
    assert "check the Opening Round first" in html
    assert 'data-entry="entry-2"' in html and "Team 2" in html
    assert "Accept and freeze draft order" in html
    assert "/admin/fixture-setup/season-1" in html


def test_draft_order_is_built_from_chosen_positions_and_posts_the_reason(setup_script):
    out = _run(
        setup_script,
        f"""
render({json.dumps(SETUP)});
selects = [{{value:'3', dataset:{{entry:'entry-1'}}}}, {{value:'1', dataset:{{entry:'entry-2'}}}}, {{value:'2', dataset:{{entry:'entry-3'}}}}];
elements['#reason-draft_order'] = {{value: 'Agreed at the AGM'}};
await acceptOrder();
await new Promise(r => setTimeout(r, 0));
console.log(JSON.stringify({{calls, message: document.querySelector('#message').textContent}}));
""",
    )
    [call] = out["calls"]
    assert call["path"] == "/api/admin/season-setup/season-1/draft-order"
    assert call["method"] == "POST"
    assert call["body"] == {"ordered_entry_ids": ["entry-2", "entry-3", "entry-1"], "reason": "Agreed at the AGM"}
    assert "Draft order accepted" in out["message"]


def test_duplicate_or_missing_positions_are_refused_without_calling_the_server(setup_script):
    out = _run(
        setup_script,
        f"""
render({json.dumps(SETUP)});
selects = [{{value:'1', dataset:{{entry:'entry-1'}}}}, {{value:'1', dataset:{{entry:'entry-2'}}}}, {{value:'', dataset:{{entry:'entry-3'}}}}];
await acceptOrder();
console.log(JSON.stringify({{calls, message: document.querySelector('#message').textContent}}));
""",
    )
    assert out["calls"] == []
    assert "different pick position" in out["message"]
