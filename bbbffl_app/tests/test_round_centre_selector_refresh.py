"""Regression coverage for issue #153: the Scorer Round Centre's round
selector must never keep showing a lifecycle label the page body has
already moved past.

## The replay evidence

During the 2026 first-half replay, the Round Centre's `<select
id="round-select">` dropdown kept reading a Round 2 as `open` after the
page body below it correctly rendered `Final . Published`. The database
was correct throughout -- the defect was purely in
`app/templates/round_centre.html`'s browser client:

- `load()` builds the dropdown's option text (including each round's
  `state`) exactly once, from its initial `/api/admin/round-review` list
  fetch;
- every mutation (`mutation()`) and the page's own periodic re-load
  (`loadRound()`) only ever refreshed the page *body* (`#round`) from
  `/api/admin/round-review/{id}` -- nothing ever touched the selector
  again.

So a round's dropdown label could go stale indefinitely while its body
kept advancing through calculation, review and publication.

## The fix

`app/templates/round_centre.html` now has `loadRound()` call
`syncRoundSelector(d)` on every load, which patches the cached round list
from the exact same authoritative response the body is rendered from (no
second, independently-timed fetch of the round list -- so this is never a
repeated mutation, just reusing data already in hand) and re-renders the
selector's options immediately. `roundOptionLabel`/`renderRoundOptions`
are the shared, single source of the option label text `load()` and
`syncRoundSelector()` both use, so the two can never build it two
different ways.

This extracts the literal script from the real rendered page (never a
separately maintained copy) and drives it under Node with a stubbed DOM/
fetch, proving the selector reflects a round's *current* lifecycle state
after a second `loadRound()` -- exactly what a Round Centre mutation
triggers -- and never regresses back to reading it once at page load.
Skipped, not failed, when Node isn't available, matching this suite's
existing convention (see `test_round_centre_client_requests.py`).
"""

import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is not available to execute the Round Centre client script"
)


@pytest.fixture
def round_centre_script(tmp_path, monkeypatch):
    """The literal inline `<script>` body from the real rendered
    `/scorer/round-centre` page, with its self-invoking `load();` call at
    the bottom stripped so a test driver can call `load()` itself and
    await it."""
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        page = client.get("/scorer/round-centre")
        assert page.status_code == 200
        html = page.text

    match = re.search(r"<script>([\s\S]*)</script>", html)
    assert match, "inline <script> not found in the rendered Round Centre page"
    source = match.group(1)
    assert "load();" in source, "self-invoking load() call not found -- page script shape changed"
    return source.replace("load();", "")


def _run(tmp_path, stubs, source, action):
    """`stubs` (the DOM/fetch stand-ins) must exist before `source` runs --
    the real page touches `document` at the top level (e.g. the legacy-
    token input), not only inside functions."""
    script_path = tmp_path / "round_centre_selector_check.js"
    script_path.write_text(stubs + source + action, encoding="utf-8")
    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_selector_label_reflects_current_lifecycle_after_a_reload(round_centre_script, tmp_path):
    """The exact replay scenario: a round starts `live`, the selector
    shows that, then the round is published (`state` becomes `final`) and
    the page reloads that round (as every `mutation()` does on success) --
    the selector must immediately read `final`, never the stale `live`."""
    stubs = """
const responses = {
  '/api/context': {coach_id: null, display_name: null, active_role: 'scorer', represented_season_entry: null},
  '/api/admin/round-review': [{bbbffl_round_id: 'r1', afl_season_id: 2026, fixture_round_number: 2, state: 'live'}],
};
let detailState = 'live';
function detail() {
  return {
    state: detailState,
    matchups: [],
    ready_for_signoff: false,
    blockers: [],
    ladder: null,
    replay: {enabled: false, classification: ''},
    identity: {bbbffl_round_id: 'r1', afl_season_id: 2026, afl_round_id: 5, fixture_round_number: 2, mapping_revision: 1},
  };
}
const elements = {};
function element(){ return {innerHTML: '', textContent: '', value: '', onclick: null, onchange: null}; }
global.document = {
  querySelector(sel){ if(!elements[sel]) elements[sel] = element(); return elements[sel]; },
  querySelectorAll(){ return []; },
};
global.history = { replaceState(){} };
global.localStorage = { getItem(){ return null; }, removeItem(){}, setItem(){} };
global.fetch = (path) => {
  if (path === '/api/admin/round-review/r1') return Promise.resolve({ ok: true, json: async () => detail() });
  return Promise.resolve({ ok: true, json: async () => responses[path] });
};
"""
    action = """
load().then(async () => {
  console.log('BEFORE:' + elements['#round-select'].innerHTML);
  detailState = 'final';
  await loadRound('r1');
  console.log('AFTER:' + elements['#round-select'].innerHTML);
  process.exit(0);
}).catch((error) => { console.error(error); process.exit(1); });
"""
    stdout = _run(tmp_path, stubs, round_centre_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert "live" in lines["BEFORE"]
    assert "final" in lines["AFTER"]
    assert "live" not in lines["AFTER"], "selector must not keep a lifecycle label the page body has moved past"


def test_selector_never_repeats_the_round_list_fetch_to_refresh(round_centre_script, tmp_path):
    """Issue #153 constraint: refreshing the selector must never repeat a
    mutation or issue a second, independent fetch of the round list --
    `syncRoundSelector` reuses the single-round response `loadRound`
    already fetched. Counts every call to `/api/admin/round-review` (the
    list endpoint) across an initial load plus one simulated post-mutation
    reload, and asserts it was only ever fetched once."""
    stubs = """
let listFetchCount = 0;
let detailState = 'live';
function detail() {
  return {
    state: detailState,
    matchups: [],
    ready_for_signoff: false,
    blockers: [],
    ladder: null,
    replay: {enabled: false, classification: ''},
    identity: {bbbffl_round_id: 'r1', afl_season_id: 2026, afl_round_id: 5, fixture_round_number: 2, mapping_revision: 1},
  };
}
const elements = {};
function element(){ return {innerHTML: '', textContent: '', value: '', onclick: null, onchange: null}; }
global.document = {
  querySelector(sel){ if(!elements[sel]) elements[sel] = element(); return elements[sel]; },
  querySelectorAll(){ return []; },
};
global.history = { replaceState(){} };
global.localStorage = { getItem(){ return null; }, removeItem(){}, setItem(){} };
global.fetch = (path) => {
  if (path === '/api/context') return Promise.resolve({ ok: true, json: async () => ({coach_id: null, active_role: 'scorer'}) });
  if (path === '/api/admin/round-review') { listFetchCount++; return Promise.resolve({ ok: true, json: async () => [{bbbffl_round_id: 'r1', afl_season_id: 2026, fixture_round_number: 2, state: 'live'}] }); }
  if (path === '/api/admin/round-review/r1') return Promise.resolve({ ok: true, json: async () => detail() });
  return Promise.reject(new Error('unexpected fetch ' + path));
};
"""
    action = """
load().then(async () => {
  detailState = 'final';
  await loadRound('r1');
  console.log('LIST_FETCHES:' + listFetchCount);
  console.log('SELECTOR:' + elements['#round-select'].innerHTML);
  process.exit(0);
}).catch((error) => { console.error(error); process.exit(1); });
"""
    stdout = _run(tmp_path, stubs, round_centre_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert lines["LIST_FETCHES"] == "1"
    assert "final" in lines["SELECTOR"]
