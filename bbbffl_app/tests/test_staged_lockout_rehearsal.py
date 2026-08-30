"""Interactive HTTP/persistence rehearsal for issue #91.

All progression is explicit replay-evidence state; there are no sleeps and no
wall-clock decisions. The normal coach service, LockoutRepository and immutable
weekly-lineup submission history remain the only production boundaries used.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.lineups import POSITIONS, WeeklyLineupRepository
from app.lockouts import LockoutRepository, LockoutTriggerRepository, LockState, RoundMatchFactsProvider
from app.player_pool import OwnershipRepository
from app.replay import ReplayAflDataSource
from app.round_mapping import RoundMappingRepository
from scripts.staged_lockout_rehearsal import REPLAY_EFFECTIVE_AT, advance_evidence, bootstrap_staged_lockout_rehearsal


def hidden(html, name):
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match
    return match.group(1)


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    result = bootstrap_staged_lockout_rehearsal(f"sqlite:///{tmp_path / 'staged.db'}", tmp_path / "evidence.json")
    base = result.bootstrap
    monkeypatch.setenv("BBBFFL_DATABASE_URL", base.database_url)
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("BBBFFL_AFL_MODE", "replay")
    monkeypatch.setenv("BBBFFL_AFL_REPLAY_EVIDENCE_PATH", base.evidence_path)
    from app.main import app

    with TestClient(app) as client:
        login_page = client.get("/login")
        login = client.post(
            "/login",
            data={
                "email": base.coach_a.email,
                "password": base.coach_a_password,
                "csrf_token": hidden(login_page.text, "csrf_token"),
            },
            cookies=login_page.cookies,
            follow_redirects=False,
        )
        cookies = {"bbbffl_session": login.cookies["bbbffl_session"]}
        yield client, result, cookies


def page_form(client, url, cookies, positions, action):
    page = client.get(url, cookies=cookies)
    form = {f"position_{position}": player_id or "" for position, player_id in positions.items()}
    form.update(
        csrf_token=hidden(page.text, "csrf_token"),
        draft_revision=hidden(page.text, "draft_revision"),
        submission_version=hidden(page.text, "submission_version"),
        action=action,
    )
    return form


def post(client, url, cookies, positions, action="submit"):
    return client.post(
        url, data=page_form(client, url, cookies, positions, action), cookies=cookies, follow_redirects=False
    )


def reload_stage(client, result, stage):
    advance_evidence(result.bootstrap.evidence_path, stage)
    # A real operator restarts the app. Replacing the eagerly loaded instance
    # here is the exact in-process equivalent and keeps the acceptance test fast.
    client.app.state.afl_client = ReplayAflDataSource(result.bootstrap.evidence_path)


def effective(database, lineup_id):
    return WeeklyLineupRepository(database).get_effective_submission(lineup_id)


def test_bootstrap_advance_reset_and_bootstrap_again(tmp_path):
    """The documented disposable-file lifecycle is repeatable verbatim."""
    database_path = tmp_path / "staged.db"
    evidence_path = tmp_path / "evidence.json"
    database_url = f"sqlite:///{database_path}"

    first = bootstrap_staged_lockout_rehearsal(database_url, evidence_path)
    for stage in ("selective-a", "selective-b", "main"):
        advance_evidence(evidence_path, stage)
        source = ReplayAflDataSource(evidence_path)
        assert source.manifest["staged_lockout_stage"] == stage
        assert source.clock.now().isoformat() == "2000-02-01T00:00:00+00:00"

    Path(database_path).unlink()
    Path(evidence_path).unlink()
    second = bootstrap_staged_lockout_rehearsal(database_url, evidence_path)
    assert second.bootstrap.season_id != first.bootstrap.season_id
    assert ReplayAflDataSource(evidence_path).manifest["staged_lockout_stage"] == "initial"


def test_progressive_plan_through_real_coach_flow_and_persisted_versions(rehearsal):
    client, result, cookies = rehearsal
    base = result.bootstrap
    database = client.app.state.database
    url = f"/coach/seasons/{base.season_id}/rounds/{base.bbbffl_round_id}/lineup"
    squad = OwnershipRepository(database).current_squad(base.coach_a.season_entry_id)
    positions = {position: period.season_player_id for position, period in zip(POSITIONS, squad, strict=True)}

    triggers = LockoutTriggerRepository(database).list_triggers(base.bbbffl_round_id)
    assert [(t.trigger_key, t.afl_match_ids) for t in triggers] == [
        ("selective-a", (9101,)),
        ("selective-b", (9102, 9103)),
        ("main", (9105,)),
    ]

    # Initial: Match 4 is LIVE but not selectively configured, so chronology
    # alone locks nothing. Save draft and submit both use normal browser flow.
    saved = post(client, url, cookies, positions, "save")
    assert saved.status_code == 303 and "draft-saved" in saved.headers["location"]
    submitted = post(client, url, cookies, positions)
    assert submitted.status_code == 303 and "notice=submitted" in submitted.headers["location"]
    lineup = database.execute(
        "SELECT lineup_id FROM weekly_lineup WHERE bbbffl_round_id=? AND season_entry_id=?",
        (base.bbbffl_round_id, base.coach_a.season_entry_id),
    ).fetchone()
    lineup_id = lineup["lineup_id"]
    first = effective(database, lineup_id)
    assert first.version == 1 and first.positions == positions
    initial_page = client.get(url, cookies=cookies)
    assert initial_page.text.count('<span class="badge">Editable</span>') == len(POSITIONS)
    initial_view = LockoutRepository(database).lock_state(
        lineup_id,
        base.bbbffl_round_id,
        base.coach_a.season_entry_id,
        first.positions,
        match_facts=RoundMatchFactsProvider(RoundMappingRepository(database), client.app.state.afl_client),
    )
    # All scheduled boundaries are in 2000 (long before the real CI clock),
    # but the manifest's explicit replay clock is before them. If lockout
    # evaluation accidentally falls back to wall time this assertion fails.
    assert initial_view.evaluated_at == "2000-02-01T00:00:00+00:00"
    assert client.app.state.afl_client.clock.now().isoformat() == "2000-02-01T00:00:00+00:00"
    assert REPLAY_EFFECTIVE_AT == "2000-02-01T00:00:00Z"

    # Trigger A: a mixed lineup. A crafted locked mutation is rejected with a
    # clear 409 page and cannot replace authoritative version 1.
    reload_stage(client, result, "selective-a")
    stage_a_page = client.get(url, cookies=cookies)
    assert "selective trigger activated · AFL match 9101" in stage_a_page.text
    assert "Editable" in stage_a_page.text
    invalid = dict(positions)
    invalid["F1"], invalid["M1"] = invalid["M1"], invalid["F1"]
    rejected = post(client, url, cookies, invalid)
    assert rejected.status_code == 409
    assert "position F1 cannot be changed (locked: selective_trigger_activated)" in rejected.text
    assert effective(database, lineup_id).version == 1

    # Restore the private draft, then save and submit a change containing only
    # editable positions. Version 1 remains append-only and F1 stays authoritative.
    editable_a = dict(positions)
    editable_a["M2"], editable_a["M3"] = editable_a["M3"], editable_a["M2"]
    assert post(client, url, cookies, editable_a, "save").status_code == 303
    assert post(client, url, cookies, editable_a).status_code == 303
    second = effective(database, lineup_id)
    assert second.version == 2 and second.positions["F1"] == first.positions["F1"]

    # Trigger B fires because Match 2 is LIVE. Match 3 itself remains UPCOMING,
    # yet both F2 and F3 lock due to the persisted multi-match group. Match 4's
    # M1 remains editable despite having been LIVE since the initial stage.
    reload_stage(client, result, "selective-b")
    view = LockoutRepository(database).lock_state(
        lineup_id,
        base.bbbffl_round_id,
        base.coach_a.season_entry_id,
        second.positions,
        match_facts=RoundMatchFactsProvider(RoundMappingRepository(database), client.app.state.afl_client),
    )
    assert view.positions["F1"].state == LockState.LOCKED
    assert view.positions["F2"].state == LockState.LOCKED
    assert view.positions["F3"].state == LockState.LOCKED
    assert view.positions["F3"].observed_status == "UPCOMING"
    assert view.positions["M1"].state == LockState.EDITABLE

    invalid_b = dict(second.positions)
    invalid_b["F3"], invalid_b["M1"] = invalid_b["M1"], invalid_b["F3"]
    assert post(client, url, cookies, invalid_b).status_code == 409
    editable_b = dict(second.positions)
    editable_b["M1"], editable_b["M2"] = editable_b["M2"], editable_b["M1"]
    assert post(client, url, cookies, editable_b, "save").status_code == 303
    assert post(client, url, cookies, editable_b).status_code == 303
    third = effective(database, lineup_id)
    assert third.version == 3
    assert third.positions["F1"] == first.positions["F1"]
    assert third.positions["F2"] == second.positions["F2"]
    assert third.positions["F3"] == second.positions["F3"]

    history = [WeeklyLineupRepository(database).get_submission(lineup_id, version) for version in (1, 2, 3)]
    assert all(history)
    assert history[0].positions == first.positions

    # Main locks every remaining ordinary position independently of its match.
    reload_stage(client, result, "main")
    final_page = client.get(url, cookies=cookies)
    assert final_page.text.count('class="badge locked"') == len(POSITIONS)
    after_main = dict(third.positions)
    after_main["M1"], after_main["M2"] = after_main["M2"], after_main["M1"]
    final_rejection = post(client, url, cookies, after_main)
    assert final_rejection.status_code == 409
    assert "main_lockout_triggered" in final_rejection.text
    assert effective(database, lineup_id).positions == third.positions
    assert effective(database, lineup_id).version == 3
