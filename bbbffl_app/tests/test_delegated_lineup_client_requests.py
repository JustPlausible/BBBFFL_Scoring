"""Regression coverage for issue #153 on the delegated/proxy lineup
browser client (`app/templates/delegated_lineup.html`).

## The gap

`submit()` already reloaded the authoritative lineup view on a rejected
mutation (save-then-submit means the private draft may have persisted
even though the submission itself was refused -- see its own comment).
`save()` and `discardDraft()` did not: a rejected save (most commonly a
stale `expected_revision` after another tab/operator saved first) left
whatever was rendered *before* the attempt on screen, with no indication
that a retry would use the same now-stale revision and fail again.

## The fix

`save()`/`discardDraft()` now match `submit()`'s existing pattern: on
rejection, reload the authoritative view with a plain GET (never a repeat
of the rejected PUT) before reporting the failure, so the draft revision,
lock state and submission shown are never stale/concurrent with the
server.

This extracts the literal script from the real rendered page and drives
it under Node with a stubbed DOM/fetch. Skipped, not failed, when Node
isn't available, matching this suite's existing convention (see
`test_round_centre_client_requests.py`).
"""

import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is not available to execute the delegated lineup client script"
)


@pytest.fixture
def delegated_lineup_script(tmp_path, monkeypatch):
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        page = client.get("/operations/rounds/some-round-id/lineup")
        assert page.status_code == 200
        html = page.text

    match = re.search(r"<script>([\s\S]*)</script>", html)
    assert match, "inline <script> not found in the rendered delegated lineup page"
    source = match.group(1)
    self_invoking = "api(`/api/operations/rounds/${roundId}/lineup`).then(render).catch(e=>msg(e.message));"
    assert self_invoking in source, "self-invoking initial load call not found -- page script shape changed"
    return source.replace(self_invoking, "")


_STUBS = """
const elements = {};
function element(){ return {innerHTML: '', textContent: '', value: '', style: {}, onclick: null, onchange: null, addEventListener(){}}; }
global.document = {
  querySelector(sel){ if (!elements[sel]) elements[sel] = element(); return elements[sel]; },
  querySelectorAll(){ return []; },
};
global.confirm = () => true;

function freshView() {
  return {
    acting_context: {display_name: 'Op', active_role: 'replay_operator', team_name: 'Team A'},
    season: {season_id: 'season-1', label: '2026'},
    round: {label: 'Round 5'},
    submission: null,
    draft: {revision: 3},
    lock_state: [],
    lockout_plan: [],
    lockout_plan_unavailable: null,
    draft_diverges_from_submission: false,
    submission_rejected: null,
    carry_forward_source: null,
    carry_forward_message: 'No previous submitted lineup is available. Enter this team manually.',
  };
}
"""


def _run(tmp_path, source, action):
    script_path = tmp_path / "delegated_lineup_check.js"
    script_path.write_text(_STUBS + source + action, encoding="utf-8")
    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_rejected_save_reloads_authoritative_draft_without_repeating_the_mutation(delegated_lineup_script, tmp_path):
    """A rejected `save()` (typically a stale `expected_revision`) must
    reload the authoritative draft/lock state via a plain GET -- never
    repeat the rejected PUT -- and never claim success."""
    action = """
let putCount = 0, getCount = 0, entriesCount = 0;
global.fetch = (path, options) => {
  if (path.endsWith('/lineup/draft') && options.method === 'PUT') {
    putCount++;
    return Promise.resolve({ ok: false, json: async () => ({ detail: 'stale draft revision' }) });
  }
  if (path.endsWith('/lineup') && options.method === 'GET') {
    getCount++;
    return Promise.resolve({ ok: true, json: async () => freshView() });
  }
  if (path.startsWith('/api/context/entries')) {
    entriesCount++;
    return Promise.resolve({ ok: true, json: async () => [] });
  }
  return Promise.reject(new Error('unexpected fetch ' + path));
};

api(`/api/operations/rounds/${roundId}/lineup`).then(render).then(async () => {
  await save();
  console.log('PUT_COUNT:' + putCount);
  console.log('GET_COUNT:' + getCount);
  console.log('MESSAGE:' + document.querySelector('#message').textContent);
  process.exit(0);
}).catch((error) => { console.error(error); process.exit(1); });
"""
    stdout = _run(tmp_path, delegated_lineup_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert lines["PUT_COUNT"] == "1", "the rejected save must not be retried"
    assert lines["GET_COUNT"] == "2", "one initial load plus exactly one reload after the rejection"
    assert "NOT saved" in lines["MESSAGE"]
    assert "reloaded" in lines["MESSAGE"].lower()


def test_rejected_discard_reloads_authoritative_draft_without_repeating_the_mutation(delegated_lineup_script, tmp_path):
    """Same guarantee for `discardDraft()`: a rejected discard/rebase must
    reload the authoritative state, never leave the stale pre-attempt
    render on screen, and never repeat the rejected PUT."""
    action = """
let putCount = 0, getCount = 0;
global.fetch = (path, options) => {
  if (path.endsWith('/lineup/draft') && options.method === 'PUT') {
    putCount++;
    return Promise.resolve({ ok: false, json: async () => ({ detail: 'stale draft revision' }) });
  }
  if (path.endsWith('/lineup') && options.method === 'GET') {
    getCount++;
    return Promise.resolve({ ok: true, json: async () => freshView() });
  }
  if (path.startsWith('/api/context/entries')) {
    return Promise.resolve({ ok: true, json: async () => [] });
  }
  return Promise.reject(new Error('unexpected fetch ' + path));
};

api(`/api/operations/rounds/${roundId}/lineup`).then(render).then(async () => {
  state.submission = {positions: {F1: null}};
  await discardDraft();
  console.log('PUT_COUNT:' + putCount);
  console.log('GET_COUNT:' + getCount);
  console.log('MESSAGE:' + document.querySelector('#message').textContent);
  process.exit(0);
}).catch((error) => { console.error(error); process.exit(1); });
"""
    stdout = _run(tmp_path, delegated_lineup_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert lines["PUT_COUNT"] == "1"
    assert lines["GET_COUNT"] == "2"
    assert "NOT applied" in lines["MESSAGE"]
    assert "reloaded" in lines["MESSAGE"].lower()
