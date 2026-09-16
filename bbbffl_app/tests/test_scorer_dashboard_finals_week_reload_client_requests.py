"""Issue #208 review (second Codex round, on commit 5b99d42): two bugs in
`app/templates/scorer_dashboard.html`'s client-side `load()`/`finalsMutation()`
that only manifest across *reloads* of the composed finals-week dashboard,
never in a single render -- so `tests/test_scorer_dashboard_finals_week_
client_requests.py`'s single-`renderFinalsWeekDashboard()`-call harness could
not have caught either one. This module drives the real extracted script's
`load()` and `finalsMutation()` functions directly under Node, mirroring the
same "execute the real script, never a copy" convention.

1. P1 -- `build_finals_week_dashboard` deliberately returns `"round": None`
   (there is no single ordinary-style round for a composed week), but
   `load()` used that to null out `current.round_id` on every successful
   load. The very first mutation on a finals/SuperScore action then
   reloads with no `round_id` at all, and the server falls back to an
   unrelated ordinary round -- the original "resolves back to Round 20"
   defect, resurfacing one click after the dashboard first renders
   correctly.

2. P2 -- `finalsMutation`'s failure handler set the "Action failed" message
   and then reloaded, but `load()` unconditionally clears the message box
   the instant its own fetch succeeds -- wiping out the failure message
   the same reload was meant to explain, before the operator could read it.

Skipped, not failed, when Node isn't available."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_scorer_dashboard_finals_week_client_requests import _extract_script, _real_dashboard_payload

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not available")


def _run(harness_body: str, payload: dict, tmp_path: Path, name: str) -> str:
    script = _extract_script()
    harness = f"""
const elements = {{}};
globalThis.document = {{
  querySelector: (sel) => {{
    if(!(sel in elements)) elements[sel] = {{ innerHTML: '', textContent: '', value: '', previousElementSibling: null, addEventListener: () => {{}} }};
    return elements[sel];
  }},
  querySelectorAll: () => [],
  getElementById: () => null,
}};
globalThis.window = {{ location: {{ href: 'http://test/scorer', search: '' }} }};
globalThis.URLSearchParams = URLSearchParams;
globalThis.URL = URL;
globalThis.history = {{ replaceState: () => {{}} }};

const dashboardPayload = {json.dumps(payload)};
const requestLog = [];
let mutationShouldFail = false;
const MUTATION_URL = '/api/test/mutation';
globalThis.fetch = async (path, opts) => {{
  const method = (opts && opts.method) || 'GET';
  requestLog.push(path);
  if(path.startsWith('/api/scorer/dashboard')){{
    return {{
      ok: true,
      json: async () => ({{
        acting_context: {{ coach_id: null, display_name: null, active_role: 'scorer', is_replay_context: false }},
        seasons: [{{ season_id: dashboardPayload.season.season_id, year: dashboardPayload.season.year, label: dashboardPayload.season.label }}],
        dashboard: dashboardPayload,
      }}),
    }};
  }}
  if(path === MUTATION_URL && method === 'POST'){{
    if(mutationShouldFail){{
      return {{ ok: false, status: 409, json: async () => ({{ detail: 'stale review version' }}) }};
    }}
    return {{ ok: true, json: async () => ({{}}) }};
  }}
  return {{ ok: true, json: async () => ({{}}) }};
}};

{script}

(async () => {{
{harness_body}
  console.log('CHECK_OK');
}})().catch(e => {{ console.error(e.stack || e); process.exit(1); }});
"""
    script_path = tmp_path / f"{name}.js"
    script_path.write_text(harness)
    result = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "CHECK_OK" in result.stdout
    return result.stdout


def test_reloading_a_finals_week_dashboard_keeps_requesting_the_same_round(tmp_path):
    """Issue #208 review finding (P1): a finals/SuperScore action's reload
    must keep re-requesting the same finals week, never drop back to no
    `round_id` at all (which the server would then resolve to an unrelated
    ordinary round)."""
    payload = _real_dashboard_payload(9711)
    body = """
  current.season_id = dashboardPayload.season.season_id;
  current.round_id = dashboardPayload.superscore.round_id;
  await load();
  if (current.round_id == null) {
    throw new Error('current.round_id was reset to null after loading a finals-week dashboard');
  }
  const expected = (dashboardPayload.finals && dashboardPayload.finals.round_id) || dashboardPayload.superscore.round_id;
  if (current.round_id !== expected) {
    throw new Error('current.round_id after load() was ' + current.round_id + ', expected ' + expected);
  }

  requestLog.length = 0;
  await load();
  const secondRequestUrl = requestLog[0];
  if (!secondRequestUrl || !secondRequestUrl.includes('round_id=')) {
    throw new Error('the second (post-mutation-style) reload dropped round_id from its request: ' + secondRequestUrl);
  }
"""
    _run(body, payload, tmp_path, "harness_reload_round_id")


def test_a_failed_mutation_leaves_its_error_message_visible_after_the_reload(tmp_path):
    """Issue #208 review finding (P2): `finalsMutation`'s failure message
    must survive the reload it triggers, never be silently wiped out by
    `load()`'s own success-path `msg('')` reset."""
    payload = _real_dashboard_payload(9712)
    body = """
  current.season_id = dashboardPayload.season.season_id;
  current.round_id = dashboardPayload.superscore.round_id;
  await load();

  mutationShouldFail = true;
  await finalsMutation(MUTATION_URL, {});
  const finalMessage = elements['#message'] && elements['#message'].textContent;
  if (finalMessage !== 'Action failed: stale review version') {
    throw new Error('expected the failure message to survive the reload, got: ' + JSON.stringify(finalMessage));
  }
"""
    _run(body, payload, tmp_path, "harness_reload_error_message")
