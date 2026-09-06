"""Regression coverage for issue #153 on the Round Preflight browser
client (`app/templates/round_preflight.html`).

## The gap

Every mutating control (`map`/`trigger`/`openRound`) only ever called
`render(...)` with the mutation's own response on success. On failure --
a stale/rejected mapping, an invalid lockout-trigger configuration, a
round that failed preflight readiness -- the handler did nothing but show
the error text: the page kept displaying whatever it rendered *before*
the attempt, which could already be stale (e.g. another operator accepted
a different mapping, or advanced the round, in the meantime). Nothing
reloaded the authoritative view, so a rejected/stale mutation could leave
the browser showing readiness/blockers/mapping state that no longer
matched the server.

## The fix

Each handler's `catch` block now calls `reloadAfterFailure()` -- a plain
GET of the same preflight view, never a repeat of the rejected mutation
-- before reporting the failure, and (for the two form-based mutations)
restores the operator's entered field values onto the freshly rendered
form, since retyping season/round/reason on a validation failure carries
no risk.

This extracts the literal script from the real rendered page and drives
it under Node with a stubbed DOM/fetch, proving a failed mutation issues
exactly one extra GET (never a second POST) and that the message reports
failure, never success. Skipped, not failed, when Node isn't available,
matching this suite's existing convention (see
`test_round_centre_client_requests.py`).
"""

import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is not available to execute the Round Preflight client script"
)


@pytest.fixture
def preflight_script(tmp_path, monkeypatch):
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        page = client.get("/admin/round-preflight/some-round-id")
        assert page.status_code == 200
        html = page.text

    match = re.search(r"<script>([\s\S]*)</script>", html)
    assert match, "inline <script> not found in the rendered Round Preflight page"
    source = match.group(1)
    self_invoking = "api(`/api/admin/round-preflight/${roundId}`).then(render).catch(x=>msg(x.message));"
    assert self_invoking in source, "self-invoking initial load call not found -- page script shape changed"
    return source.replace(self_invoking, "")


def _view(mapping=None):
    return {
        "round": {
            "season_label": "2026",
            "competition_label": "Ordinary",
            "label": "Round 5",
            "sequence": 5,
            "lifecycle_state": "upcoming",
            "bbbffl_round_id": "some-round-id",
        },
        "fixture_matchups": [],
        "mapping": mapping,
        "afl_evidence_fresh": True,
        "afl_matches": [],
        "lockout_triggers": [],
        "opening_round": {"applies": False, "deferred_selections": []},
        "readiness": {"safe_to_open": False, "blockers": [], "advisories": []},
    }


_STUBS = """
const elements = {};
function element(){ return {innerHTML: '', textContent: '', value: '', onclick: null, onsubmit: null, className: ''}; }
function formElement(){ return {season: {value: ''}, round: {value: ''}, reason: {value: ''}, key: {value: ''}, type: {value: ''}, sequence: {value: ''}, matches: {value: ''}}; }
global.document = {
  querySelector(sel){
    if (!elements[sel]) elements[sel] = (sel === '#mapping' || sel === '#trigger') ? formElement() : element();
    return elements[sel];
  },
};
global.confirm = () => true;
global.localStorage = { getItem(){ return null; }, removeItem(){}, setItem(){} };
global.FormData = class {
  constructor(target) { this._data = (target && target._data) || {}; }
  get(key) { return Object.prototype.hasOwnProperty.call(this._data, key) ? this._data[key] : null; }
};
"""


def _run(tmp_path, source, action):
    script_path = tmp_path / "round_preflight_check.js"
    script_path.write_text(_STUBS + source + action, encoding="utf-8")
    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_rejected_mapping_reloads_authoritative_state_without_repeating_the_mutation(preflight_script, tmp_path):
    """A rejected `map()` call must: (1) never imply success, (2) reload
    the current authoritative view via a plain GET rather than leaving the
    pre-attempt render on screen, (3) never repeat the rejected POST, and
    (4) preserve the entered season/round/reason so the operator does not
    have to retype them."""
    import json

    view_before = json.dumps(_view())
    view_after = json.dumps(_view(mapping={"afl_season_id": 2026, "afl_round_id": 9, "revision": 1}))
    action = f"""
let getCount = 0, postCount = 0;
global.fetch = (path, options) => {{
  const method = options && options.method ? options.method : (options && options.body ? 'POST' : 'GET');
  if (method === 'GET') {{ getCount++; return Promise.resolve({{ ok: true, json: async () => JSON.parse(getCount === 1 ? '{view_before}' : '{view_after}') }}); }}
  postCount++;
  return Promise.resolve({{ ok: false, json: async () => ({{ detail: 'stale mapping revision' }}) }});
}};

api(`/api/admin/round-preflight/${{roundId}}`).then(render).then(async () => {{
  const form = document.querySelector('#mapping');
  form.season.value = '2026'; form.round.value = '9'; form.reason.value = 'operator note';
  await map({{ preventDefault(){{}}, target: {{ _data: {{ season: '2026', round: '9', reason: 'operator note' }} }} }});
  console.log('GET_COUNT:' + getCount);
  console.log('POST_COUNT:' + postCount);
  console.log('MESSAGE:' + document.querySelector('#message').textContent);
  console.log('SEASON_PRESERVED:' + document.querySelector('#mapping').season.value);
  console.log('REASON_PRESERVED:' + document.querySelector('#mapping').reason.value);
  process.exit(0);
}}).catch((error) => {{ console.error(error); process.exit(1); }});
"""
    stdout = _run(tmp_path, preflight_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert lines["POST_COUNT"] == "1", "the rejected mutation must not be retried"
    assert lines["GET_COUNT"] == "2", "one initial load plus exactly one reload after the rejection"
    assert "NOT confirmed" in lines["MESSAGE"]
    assert "reloaded" in lines["MESSAGE"].lower()
    assert lines["SEASON_PRESERVED"] == "2026"
    assert lines["REASON_PRESERVED"] == "operator note"


def test_rejected_open_round_reloads_and_never_claims_success(preflight_script, tmp_path):
    """A rejected `openRound()` call (e.g. the round failed readiness
    between page load and the click) must reload the authoritative view
    and report failure, never the success message."""
    import json

    view = json.dumps(_view())
    action = f"""
let getCount = 0, postCount = 0;
global.fetch = (path, options) => {{
  const method = options && options.method ? options.method : (options && options.body ? 'POST' : 'GET');
  if (method === 'GET') {{ getCount++; return Promise.resolve({{ ok: true, json: async () => JSON.parse('{view}') }}); }}
  postCount++;
  return Promise.resolve({{ ok: false, json: async () => ({{ detail: 'round failed preflight and was not opened' }}) }});
}};

api(`/api/admin/round-preflight/${{roundId}}`).then(render).then(async () => {{
  await openRound();
  console.log('GET_COUNT:' + getCount);
  console.log('POST_COUNT:' + postCount);
  console.log('MESSAGE:' + document.querySelector('#message').textContent);
  process.exit(0);
}}).catch((error) => {{ console.error(error); process.exit(1); }});
"""
    stdout = _run(tmp_path, preflight_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert lines["POST_COUNT"] == "1"
    assert lines["GET_COUNT"] == "2"
    assert "NOT opened" in lines["MESSAGE"]
    assert "opened. coaches" not in lines["MESSAGE"].lower()


def test_when_the_post_rejection_reload_also_fails_the_message_never_claims_success(preflight_script, tmp_path):
    """Codex review finding (P2) on this PR: if the mutation is rejected
    *and* the follow-up reload GET also fails (e.g. a transient outage),
    the message must not claim the page was refreshed -- the operator
    needs to know the displayed state may still be stale."""
    import json

    view = json.dumps(_view())
    action = f"""
let getCount = 0;
global.fetch = (path, options) => {{
  const method = options && options.method ? options.method : (options && options.body ? 'POST' : 'GET');
  if (method === 'GET') {{
    getCount++;
    if (getCount === 1) {{ return Promise.resolve({{ ok: true, json: async () => JSON.parse('{view}') }}); }}
    return Promise.reject(new Error('network error'));
  }}
  return Promise.resolve({{ ok: false, json: async () => ({{ detail: 'stale mapping revision' }}) }});
}};

api(`/api/admin/round-preflight/${{roundId}}`).then(render).then(async () => {{
  await map({{ preventDefault(){{}}, target: {{ _data: {{ season: '2026', round: '9', reason: 'operator note' }} }} }});
  console.log('MESSAGE:' + document.querySelector('#message').textContent);
  process.exit(0);
}}).catch((error) => {{ console.error(error); process.exit(1); }});
"""
    stdout = _run(tmp_path, preflight_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert "NOT confirmed" in lines["MESSAGE"]
    assert "has been reloaded" not in lines["MESSAGE"]
    assert "could not be reloaded" in lines["MESSAGE"].lower()
