"""Regression coverage for issue #153 on the locked-lineup correction
browser client (`app/templates/lineup_correction.html`).

## The gap

A rejected `submitCorrection()` (e.g. a stale `expected_submission_version`
because another operator corrected the lineup first) only ever showed the
error text -- the previously rendered candidate (lock states, effective
lineup, calculation status) stayed on screen, possibly no longer matching
the server, and a retry would reuse the same now-stale
`expected_submission_version` and fail again.

## The fix

The `catch` block now reloads the authoritative candidate with a plain
GET (never a repeat of the rejected POST) before reporting the failure,
and restores the entered reason text (safe to retype; the attempted
position changes are correctly *not* restored, since they may no longer
be legal against the reloaded lock state).

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
    shutil.which("node") is None, reason="Node.js is not available to execute the lineup correction client script"
)


@pytest.fixture
def correction_script(tmp_path, monkeypatch):
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        page = client.get("/scorer/lineup-correction")
        assert page.status_code == 200
        html = page.text

    match = re.search(r"<script>([\s\S]*)</script>", html)
    assert match, "inline <script> not found in the rendered lineup correction page"
    source = match.group(1)
    self_invoking = "if(document.querySelector('#round-id').value) loadRound();"
    assert self_invoking in source, "self-invoking initial-load guard not found -- page script shape changed"
    return source.replace(self_invoking, "")


_STUBS = """
const elements = {};
function element(){ return {innerHTML: '', textContent: '', value: '', className: '', style: {}, onclick: null, onchange: null, onsubmit: null}; }
const correctSelect = {name: 'F1', value: 'new-id', dataset: {original: 'old-id'}, onchange: null};
global.document = {
  querySelector(sel){ if (!elements[sel]) elements[sel] = element(); return elements[sel]; },
  querySelectorAll(sel){ return sel === '#correct-form select' ? [correctSelect] : []; },
};
global.history = { replaceState(){} };
global.FormData = class {
  constructor(target) { this._data = (target && target._data) || {}; }
  get(key) { return Object.prototype.hasOwnProperty.call(this._data, key) ? this._data[key] : null; }
};

function candidateView() {
  return {
    team_name: 'Team A',
    coach_name: 'Coach A',
    round_state: 'live',
    calculation: {
      calculated: false, calculation_revision: null, calculated_lineup_version: null,
      current_lineup_version: null, stale: false, message: 'This matchup has not been calculated yet.',
    },
    expected_submission_version: 1,
    slots: [
      {position: 'F1', player_display_name: 'Old Player', afl_club_name: 'Fitzroy', lock_state: 'locked', lock_reason: 'trigger activated', season_player_id: 'old-id'},
    ],
    available_players: [
      {season_player_id: 'old-id', display_name: 'Old Player', afl_club_name: 'Fitzroy'},
      {season_player_id: 'new-id', display_name: 'New Player', afl_club_name: 'Richmond'},
    ],
    correction_history: [],
  };
}
"""


def _run(tmp_path, source, action):
    script_path = tmp_path / "lineup_correction_check.js"
    script_path.write_text(_STUBS + source + action, encoding="utf-8")
    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_rejected_correction_reloads_and_preserves_reason_without_repeating_the_mutation(correction_script, tmp_path):
    action = """
let listGetCount = 0, candidateGetCount = 0, postCount = 0;
global.fetch = (path, options) => {
  const method = (options && options.method) || 'GET';
  if (path === '/api/admin/lineup-correction/round-1' && method === 'GET') {
    listGetCount++;
    return Promise.resolve({ ok: true, json: async () => ({ round: { label: 'Round 5', state: 'live' }, season: { label: '2026' }, entries: [{ season_entry_id: 'entry-1', team_name: 'Team A', coach_name: 'Coach A' }] }) });
  }
  if (path === '/api/admin/lineup-correction/round-1/entry-1' && method === 'GET') {
    candidateGetCount++;
    return Promise.resolve({ ok: true, json: async () => candidateView() });
  }
  if (path === '/api/admin/lineup-correction/round-1/entry-1/correct' && method === 'POST') {
    postCount++;
    return Promise.resolve({ ok: false, json: async () => ({ detail: 'stale submission version' }) });
  }
  return Promise.reject(new Error('unexpected fetch ' + path));
};

(async () => {
  document.querySelector('#round-id').value = 'round-1';
  await loadRound();
  await loadEntry('entry-1');
  await submitCorrection({ preventDefault(){}, target: { _data: { reason: 'League chat confirmed the transposed forwards' } } });
  console.log('LIST_GET_COUNT:' + listGetCount);
  console.log('CANDIDATE_GET_COUNT:' + candidateGetCount);
  console.log('POST_COUNT:' + postCount);
  console.log('MESSAGE:' + document.querySelector('#message').textContent);
  console.log('REASON_PRESERVED:' + document.querySelector('#correct-form [name="reason"]').value);
  process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
"""
    stdout = _run(tmp_path, correction_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert lines["POST_COUNT"] == "1", "the rejected correction must not be retried"
    assert lines["CANDIDATE_GET_COUNT"] == "2", "one initial load plus exactly one reload after the rejection"
    assert "NOT applied" in lines["MESSAGE"]
    assert "reloaded" in lines["MESSAGE"].lower()
    assert lines["REASON_PRESERVED"] == "League chat confirmed the transposed forwards"


def test_when_the_post_rejection_reload_also_fails_correction_never_claims_success(correction_script, tmp_path):
    """Codex review finding (P2) on this PR: if the rejected correction's
    follow-up reload GET also fails, the message must not claim the page
    was refreshed."""
    action = """
let candidateGetCount = 0;
global.fetch = (path, options) => {
  const method = (options && options.method) || 'GET';
  if (path === '/api/admin/lineup-correction/round-1' && method === 'GET') {
    return Promise.resolve({ ok: true, json: async () => ({ round: { label: 'Round 5', state: 'live' }, season: { label: '2026' }, entries: [{ season_entry_id: 'entry-1', team_name: 'Team A', coach_name: 'Coach A' }] }) });
  }
  if (path === '/api/admin/lineup-correction/round-1/entry-1' && method === 'GET') {
    candidateGetCount++;
    if (candidateGetCount === 1) { return Promise.resolve({ ok: true, json: async () => candidateView() }); }
    return Promise.reject(new Error('network error'));
  }
  if (path === '/api/admin/lineup-correction/round-1/entry-1/correct' && method === 'POST') {
    return Promise.resolve({ ok: false, json: async () => ({ detail: 'stale submission version' }) });
  }
  return Promise.reject(new Error('unexpected fetch ' + path));
};

(async () => {
  document.querySelector('#round-id').value = 'round-1';
  await loadRound();
  await loadEntry('entry-1');
  await submitCorrection({ preventDefault(){}, target: { _data: { reason: 'League chat confirmed the transposed forwards' } } });
  console.log('MESSAGE:' + document.querySelector('#message').textContent);
  process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
"""
    stdout = _run(tmp_path, correction_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert "NOT applied" in lines["MESSAGE"]
    assert "has been reloaded" not in lines["MESSAGE"]
    assert "could not be reloaded" in lines["MESSAGE"].lower()
