"""Regression coverage for the Round Preflight browser client
(`app/templates/round_preflight.html`).

## Issue #153's gap (preserved)

Every mutating control (`map`/`trigger`/`openRound`) only ever called
`render(...)` with the mutation's own response on success. On failure --
a stale/rejected mapping, an invalid lockout-trigger configuration, a
round that failed preflight readiness -- the handler did nothing but show
the error text: the page kept displaying whatever it rendered *before*
the attempt, which could already be stale. Each handler's `catch` block
calls `reloadAfterFailure()` -- a plain GET of the same preflight view,
never a repeat of the rejected mutation -- before reporting the failure.

## Issue #152 additions covered here

`map()`/`trigger()` now submit human-readable season/round selections (or
an advanced manual override) and a checked-match list respectively, both
requiring an explicit reason and, for mapping, an explicit `confirmed`
flag and a revision the browser last observed (`expected_revision`) --
never inferred, never defaulted to "yes". `useMappingRecommendation()`/
`useLockoutStage()` only ever *fill* form fields from an advisory
recommendation; they must never themselves issue a network request.

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


def _trigger_view(**overrides):
    base = {
        "trigger_key": "early",
        "trigger_type": "selective",
        "sequence": 1,
        "revision": 1,
        "scope": "Players involved in the activating AFL match(es)",
        "participating_clubs": [],
        "activating_matches": [],
        "activation": {
            "activated": False,
            "activation_reason": None,
            "effective_lock_at": None,
            "observed_status_at_activation": None,
        },
    }
    base.update(overrides)
    return base


def _view(
    mapping=None, mapping_history=None, mapping_recommendation=None, lockout_triggers=None, lockout_recommendation=None
):
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
        "mapping_context": None,
        "mapping_history": mapping_history or [],
        "mapping_recommendation": mapping_recommendation,
        "afl_seasons": [],
        "afl_evidence_fresh": True,
        "afl_matches": [],
        "lockout_triggers": lockout_triggers or [],
        "lockout_recommendation": lockout_recommendation,
        "replay_checkpoint_recommendations": [],
        "opening_round": {"applies": False, "deferred_selections": []},
        "readiness": {"safe_to_open": False, "blockers": [], "advisories": []},
    }


_STUBS = """
const elements = {};
function element(){ return {innerHTML: '', textContent: '', value: '', onclick: null, onsubmit: null, className: '', addEventListener(){}, querySelector(){ return null; }, querySelectorAll(){ return []; }}; }
function formElement(fields){ const f = {}; for (const name of fields) f[name] = {value: ''}; f.querySelector = () => null; f.querySelectorAll = () => []; return f; }
global.document = {
  querySelector(sel){
    if (!elements[sel]) {
      if (sel === '#mapping') elements[sel] = formElement(['reason', 'season_manual', 'round_manual']);
      else if (sel === '#trigger') elements[sel] = formElement(['key', 'type', 'sequence', 'reason']);
      else if (sel === 'select[name="season_select"]') elements[sel] = { onchange: null };
      else elements[sel] = element();
    }
    return elements[sel];
  },
  querySelectorAll(){ return []; },
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
    (4) preserve the entered reason so the operator does not have to retype
    it."""
    import json

    view_before = json.dumps(_view())
    view_after = json.dumps(_view(mapping={"afl_season_id": 2026, "afl_round_id": 9, "revision": 1}))
    action = f"""
let getCount = 0, postCount = 0, lastBody = null;
global.fetch = (path, options) => {{
  const method = options && options.method ? options.method : (options && options.body ? 'POST' : 'GET');
  if (method === 'GET') {{ getCount++; return Promise.resolve({{ ok: true, json: async () => JSON.parse(getCount === 1 ? '{view_before}' : '{view_after}') }}); }}
  postCount++; lastBody = JSON.parse(options.body);
  return Promise.resolve({{ ok: false, json: async () => ({{ detail: 'stale mapping revision' }}) }});
}};

api(`/api/admin/round-preflight/${{roundId}}`).then(render).then(async () => {{
  const form = document.querySelector('#mapping');
  form.reason.value = 'operator note';
  await map({{ preventDefault(){{}}, target: {{
    _data: {{ season_manual: '2026', round_manual: '9', reason: 'operator note' }},
    confirmed: {{ checked: true }},
  }} }});
  console.log('GET_COUNT:' + getCount);
  console.log('POST_COUNT:' + postCount);
  console.log('CONFIRMED_SENT:' + lastBody.confirmed);
  console.log('EXPECTED_REVISION_SENT:' + lastBody.expected_revision);
  console.log('MESSAGE:' + document.querySelector('#message').textContent);
  console.log('REASON_PRESERVED:' + document.querySelector('#mapping').reason.value);
  process.exit(0);
}}).catch((error) => {{ console.error(error); process.exit(1); }});
"""
    stdout = _run(tmp_path, preflight_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert lines["POST_COUNT"] == "1", "the rejected mutation must not be retried"
    assert lines["GET_COUNT"] == "2", "one initial load plus exactly one reload after the rejection"
    assert lines["CONFIRMED_SENT"] == "true", "confirmation must be sent exactly as the operator set it"
    assert lines["EXPECTED_REVISION_SENT"] == "0", "no prior mapping observed -> expected_revision 0"
    assert "NOT confirmed" in lines["MESSAGE"]
    assert "reloaded" in lines["MESSAGE"].lower()
    assert lines["REASON_PRESERVED"] == "operator note"


def test_map_sends_advanced_manual_ids_when_supplied(preflight_script, tmp_path):
    """The advanced manual-entry fallback (season_manual/round_manual) must
    take precedence over the season/round selects when both are filled."""
    import json

    view = json.dumps(_view(mapping={"afl_season_id": 2026, "afl_round_id": 100, "revision": 3}))
    action = f"""
let lastBody = null;
global.fetch = (path, options) => {{
  const method = options && options.method ? options.method : (options && options.body ? 'POST' : 'GET');
  if (method === 'GET') {{ return Promise.resolve({{ ok: true, json: async () => JSON.parse('{view}') }}); }}
  lastBody = JSON.parse(options.body);
  return Promise.resolve({{ ok: true, json: async () => JSON.parse('{view}') }});
}};

api(`/api/admin/round-preflight/${{roundId}}`).then(render).then(async () => {{
  await map({{ preventDefault(){{}}, target: {{
    _data: {{ season_manual: '2030', round_manual: '77', reason: 'divergent on purpose' }},
    confirmed: {{ checked: true }},
  }} }});
  console.log('SEASON:' + lastBody.afl_season_id);
  console.log('ROUND:' + lastBody.afl_round_id);
  console.log('EXPECTED_REVISION:' + lastBody.expected_revision);
  process.exit(0);
}}).catch((error) => {{ console.error(error); process.exit(1); }});
"""
    stdout = _run(tmp_path, preflight_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert lines["SEASON"] == "2030"
    assert lines["ROUND"] == "77"
    assert lines["EXPECTED_REVISION"] == "3"


def test_rejected_trigger_reloads_and_preserves_key_type_sequence(preflight_script, tmp_path):
    import json

    view_before = json.dumps(_view())
    view_after = json.dumps(_view(lockout_triggers=[_trigger_view(trigger_key="early", revision=2)]))
    action = f"""
let getCount = 0, postCount = 0, lastBody = null;
global.fetch = (path, options) => {{
  const method = options && options.method ? options.method : (options && options.body ? 'POST' : 'GET');
  if (method === 'GET') {{ getCount++; return Promise.resolve({{ ok: true, json: async () => JSON.parse(getCount === 1 ? '{view_before}' : '{view_after}') }}); }}
  postCount++; lastBody = JSON.parse(options.body);
  return Promise.resolve({{ ok: false, json: async () => ({{ detail: 'not part of the accepted mapping' }}) }});
}};

api(`/api/admin/round-preflight/${{roundId}}`).then(render).then(async () => {{
  await trigger({{ preventDefault(){{}}, target: {{
    _data: {{ key: 'early', type: 'selective', sequence: '1', reason: 'note' }},
    querySelectorAll: (sel) => sel.includes('matches') ? [{{value: '9001'}}, {{value: '9002'}}] : [],
  }} }});
  console.log('GET_COUNT:' + getCount);
  console.log('POST_COUNT:' + postCount);
  console.log('MATCH_IDS_SENT:' + JSON.stringify(lastBody.afl_match_ids));
  console.log('EXPECTED_REVISION_SENT:' + lastBody.expected_revision);
  console.log('MESSAGE:' + document.querySelector('#message').textContent);
  console.log('KEY_PRESERVED:' + document.querySelector('#trigger').key.value);
  console.log('TYPE_PRESERVED:' + document.querySelector('#trigger').type.value);
  process.exit(0);
}}).catch((error) => {{ console.error(error); process.exit(1); }});
"""
    stdout = _run(tmp_path, preflight_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert lines["POST_COUNT"] == "1"
    assert lines["GET_COUNT"] == "2"
    assert lines["MATCH_IDS_SENT"] == "[9001,9002]"
    assert lines["EXPECTED_REVISION_SENT"] == "0", "no existing trigger with this key -> expected_revision 0"
    assert "NOT persisted" in lines["MESSAGE"]
    assert lines["KEY_PRESERVED"] == "early"
    assert lines["TYPE_PRESERVED"] == "selective"


def test_trigger_uses_existing_revision_when_editing_a_known_key(preflight_script, tmp_path):
    import json

    view = json.dumps(_view(lockout_triggers=[_trigger_view(trigger_key="main", trigger_type="main", revision=5)]))
    action = f"""
let lastBody = null;
global.fetch = (path, options) => {{
  const method = options && options.method ? options.method : (options && options.body ? 'POST' : 'GET');
  if (method === 'GET') {{ return Promise.resolve({{ ok: true, json: async () => JSON.parse('{view}') }}); }}
  lastBody = JSON.parse(options.body);
  return Promise.resolve({{ ok: true, json: async () => JSON.parse('{view}') }});
}};

api(`/api/admin/round-preflight/${{roundId}}`).then(render).then(async () => {{
  await trigger({{ preventDefault(){{}}, target: {{
    _data: {{ key: 'main', type: 'main', sequence: '2', reason: 'note' }},
    querySelectorAll: () => [{{value: '9002'}}],
  }} }});
  console.log('EXPECTED_REVISION:' + lastBody.expected_revision);
  process.exit(0);
}}).catch((error) => {{ console.error(error); process.exit(1); }});
"""
    stdout = _run(tmp_path, preflight_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert lines["EXPECTED_REVISION"] == "5"


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
    """Codex review finding (P2) on issue #153's PR: if the mutation is
    rejected *and* the follow-up reload GET also fails (e.g. a transient
    outage), the message must not claim the page was refreshed -- the
    operator needs to know the displayed state may still be stale."""
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
  await map({{ preventDefault(){{}}, target: {{
    _data: {{ season_manual: '2026', round_manual: '9', reason: 'operator note' }},
    confirmed: {{ checked: true }},
  }} }});
  console.log('MESSAGE:' + document.querySelector('#message').textContent);
  process.exit(0);
}}).catch((error) => {{ console.error(error); process.exit(1); }});
"""
    stdout = _run(tmp_path, preflight_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert "NOT confirmed" in lines["MESSAGE"]
    assert "has been reloaded" not in lines["MESSAGE"]
    assert "could not be reloaded" in lines["MESSAGE"].lower()


def test_use_mapping_recommendation_only_fills_fields_and_never_calls_fetch(preflight_script, tmp_path):
    """A recommendation must remain advisory-only: clicking "use it" fills
    the advanced manual fields but must never itself mutate anything."""
    import json

    view = json.dumps(
        _view(
            mapping_recommendation={
                "afl_season_id": 91,
                "afl_round_id": 1234,
                "afl_season_year": 2026,
                "afl_round_number": 5,
                "evidence": "exact match",
            }
        )
    )
    action = f"""
let fetchCount = 0;
global.fetch = () => {{ fetchCount++; return Promise.resolve({{ ok: true, json: async () => JSON.parse('{view}') }}); }};

api(`/api/admin/round-preflight/${{roundId}}`).then(render).then(async () => {{
  fetchCount = 0;
  useMappingRecommendation();
  console.log('FETCH_COUNT:' + fetchCount);
  console.log('SEASON_MANUAL:' + document.querySelector('#mapping').season_manual.value);
  console.log('ROUND_MANUAL:' + document.querySelector('#mapping').round_manual.value);
  process.exit(0);
}}).catch((error) => {{ console.error(error); process.exit(1); }});
"""
    stdout = _run(tmp_path, preflight_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert lines["FETCH_COUNT"] == "0", "a recommendation fill must never itself issue a network request"
    assert lines["SEASON_MANUAL"] == "91"
    assert lines["ROUND_MANUAL"] == "1234"


def test_map_derives_expected_revision_from_mapping_history_when_no_mapping_is_accepted_yet(preflight_script, tmp_path):
    """Codex review (second pass, P1): a round with an unresolved/ambiguous
    mapping *proposal* already has a `round_afl_mapping` row at revision 1+
    even though `mapping` (the *accepted* mapping only) is null. Falling
    back to a bare 0 there would make the operator's very first explicit
    accept always look stale and get permanently rejected -- the JS must
    fall back to the latest `mapping_history` revision instead."""
    import json

    view = json.dumps(
        _view(mapping_history=[{"revision": 1, "state": "ambiguous"}, {"revision": 2, "state": "ambiguous"}])
    )
    action = f"""
let lastBody = null;
global.fetch = (path, options) => {{
  const method = options && options.method ? options.method : (options && options.body ? 'POST' : 'GET');
  if (method === 'GET') {{ return Promise.resolve({{ ok: true, json: async () => JSON.parse('{view}') }}); }}
  lastBody = JSON.parse(options.body);
  return Promise.resolve({{ ok: true, json: async () => JSON.parse('{view}') }});
}};

api(`/api/admin/round-preflight/${{roundId}}`).then(render).then(async () => {{
  await map({{ preventDefault(){{}}, target: {{
    _data: {{ season_manual: '2026', round_manual: '100', reason: 'explicit accept over an ambiguous proposal' }},
    confirmed: {{ checked: true }},
  }} }});
  console.log('EXPECTED_REVISION:' + lastBody.expected_revision);
  process.exit(0);
}}).catch((error) => {{ console.error(error); process.exit(1); }});
"""
    stdout = _run(tmp_path, preflight_script, action)
    lines = dict(line.split(":", 1) for line in stdout.strip().splitlines())
    assert lines["EXPECTED_REVISION"] == "2", "must use the latest mapping_history revision, not a bare 0"
