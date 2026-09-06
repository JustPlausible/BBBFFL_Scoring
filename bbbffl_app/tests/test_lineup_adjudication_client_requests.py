"""Regression coverage for issue #153 on the missed-submission
adjudication browser client (`app/templates/lineup_adjudication.html`).

## The gap

A rejected `submitAcceptEvidencedDraft()`/`submitCarryForward()` (e.g. a
concurrent adjudication or correction already created a submission for
this team) only ever showed the error text and left the previous
candidate render on screen -- possibly no longer matching the server's
eligibility/trigger/preview state.

## The fix

Both handlers now reload the authoritative candidate with a plain GET
(`loadEntry`, never a repeat of the rejected POST) before reporting the
failure, and restore the entered reason text.

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
    shutil.which("node") is None, reason="Node.js is not available to execute the lineup adjudication client script"
)


@pytest.fixture
def adjudication_script(tmp_path, monkeypatch):
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        page = client.get("/scorer/lineup-adjudication")
        assert page.status_code == 200
        html = page.text

    match = re.search(r"<script>([\s\S]*)</script>", html)
    assert match, "inline <script> not found in the rendered lineup adjudication page"
    source = match.group(1)
    self_invoking = "if(document.querySelector('#round-id').value) loadRound();"
    assert self_invoking in source, "self-invoking initial-load guard not found -- page script shape changed"
    return source.replace(self_invoking, "")


_STUBS = """
const elements = {};
function element(){ return {innerHTML: '', textContent: '', value: '', className: '', style: {}, onclick: null, onsubmit: null}; }
global.document = {
  querySelector(sel){ if (!elements[sel]) elements[sel] = element(); return elements[sel]; },
  querySelectorAll(){ return []; },
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
    eligible: true,
    ineligible_reason: null,
    activated_triggers: [],
    calculation: {
      calculated: false, calculation_revision: null, calculated_lineup_version: null,
      current_lineup_version: null, stale: false, message: 'This matchup has not been calculated yet.',
    },
    draft_revision: 1,
    draft_updated_at: '2026-01-01T00:00:00+00:00',
    evidenced_preview: [{position: 'F1', season_player_id: 'p1', player_name: 'Player One', afl_club: 'Fitzroy', evidence_status: 'proven_pre_lock', lock_reason: null, afl_match_id: null, effective_lock_at: null, evidence_saved_at: '2026-01-01T00:00:00+00:00'}],
    carry_forward_preview: null,
  };
}
"""


def _run(tmp_path, source, action):
    script_path = tmp_path / "lineup_adjudication_check.js"
    script_path.write_text(_STUBS + source + action, encoding="utf-8")
    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_rejected_accept_evidenced_draft_reloads_and_preserves_reason(adjudication_script, tmp_path):
    action = """
let listGetCount = 0, candidateGetCount = 0, postCount = 0;
global.fetch = (path, options) => {
  const method = (options && options.method) || 'GET';
  if (path === '/api/admin/lineup-adjudication/round-1' && method === 'GET') {
    listGetCount++;
    return Promise.resolve({ ok: true, json: async () => ({ round: { label: 'Round 5', state: 'live' }, season: { label: '2026' }, entries: [{ season_entry_id: 'entry-1', team_name: 'Team A', coach_name: 'Coach A', has_effective_submission: false }] }) });
  }
  if (path === '/api/admin/lineup-adjudication/round-1/entry-1' && method === 'GET') {
    candidateGetCount++;
    return Promise.resolve({ ok: true, json: async () => candidateView() });
  }
  if (path === '/api/admin/lineup-adjudication/round-1/entry-1/accept-evidenced-draft' && method === 'POST') {
    postCount++;
    return Promise.resolve({ ok: false, json: async () => ({ detail: 'an effective authoritative submission already exists' }) });
  }
  return Promise.reject(new Error('unexpected fetch ' + path));
};

(async () => {
  document.querySelector('#round-id').value = 'round-1';
  await loadRound();
  await loadEntry('entry-1');
  await submitAcceptEvidencedDraft({ preventDefault(){}, target: { _data: { reason: 'League chat confirmed the pre-lockout draft' } } });
  console.log('LIST_GET_COUNT:' + listGetCount);
  console.log('CANDIDATE_GET_COUNT:' + candidateGetCount);
  console.log('POST_COUNT:' + postCount);
  console.log('MESSAGE:' + document.querySelector('#message').textContent);
  console.log('REASON_PRESERVED:' + document.querySelector('#accept-form [name="reason"]').value);
  process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
"""
    stdout = _run(tmp_path, adjudication_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert lines["POST_COUNT"] == "1", "the rejected adjudication must not be retried"
    assert lines["CANDIDATE_GET_COUNT"] == "2", "one initial load plus exactly one reload after the rejection"
    assert "NOT accepted" in lines["MESSAGE"]
    assert "reloaded" in lines["MESSAGE"].lower()
    assert lines["REASON_PRESERVED"] == "League chat confirmed the pre-lockout draft"


def test_rejected_carry_forward_reloads_and_preserves_reason(adjudication_script, tmp_path):
    action = """
let candidateGetCount = 0, postCount = 0;
global.fetch = (path, options) => {
  const method = (options && options.method) || 'GET';
  if (path === '/api/admin/lineup-adjudication/round-1' && method === 'GET') {
    return Promise.resolve({ ok: true, json: async () => ({ round: { label: 'Round 5', state: 'live' }, season: { label: '2026' }, entries: [{ season_entry_id: 'entry-1', team_name: 'Team A', coach_name: 'Coach A', has_effective_submission: false }] }) });
  }
  if (path === '/api/admin/lineup-adjudication/round-1/entry-1' && method === 'GET') {
    candidateGetCount++;
    return Promise.resolve({ ok: true, json: async () => candidateView() });
  }
  if (path === '/api/admin/lineup-adjudication/round-1/entry-1/apply-carry-forward' && method === 'POST') {
    postCount++;
    return Promise.resolve({ ok: false, json: async () => ({ detail: 'no lockout trigger has activated for this round yet' }) });
  }
  return Promise.reject(new Error('unexpected fetch ' + path));
};

(async () => {
  document.querySelector('#round-id').value = 'round-1';
  await loadRound();
  await loadEntry('entry-1');
  await submitCarryForward({ preventDefault(){}, target: { _data: { reason: 'Quorum rejected the late capture request' } } });
  console.log('CANDIDATE_GET_COUNT:' + candidateGetCount);
  console.log('POST_COUNT:' + postCount);
  console.log('MESSAGE:' + document.querySelector('#message').textContent);
  console.log('REASON_PRESERVED:' + document.querySelector('#carry-forward-form [name="reason"]').value);
  process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
"""
    stdout = _run(tmp_path, adjudication_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert lines["POST_COUNT"] == "1"
    assert lines["CANDIDATE_GET_COUNT"] == "2"
    assert "NOT applied" in lines["MESSAGE"]
    assert "reloaded" in lines["MESSAGE"].lower()
    assert lines["REASON_PRESERVED"] == "Quorum rejected the late capture request"
