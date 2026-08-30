"""Issue #98 coach UX safeguard: `Submit Lineup` interposes one explicit
confirmation step whenever one or more ordinary positions are vacant,
before creating an authoritative submitted version -- driven through the
real coach HTTP flow, exactly like the persistent Round 1 rehearsal
(tests/test_round1_rehearsal_acceptance.py).

This is a UX-only gate: the underlying #98 domain rules (a deliberate
vacancy is legitimate, a missing position key is malformed, a named
non-participant is DNP) are unchanged and untouched by this file --
see tests/test_lineup_validation.py, tests/test_lockouts.py, etc.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.lineups import POSITIONS, WeeklyLineupRepository
from app.player_pool import OwnershipRepository
from scripts.bootstrap_round1_2026 import bootstrap_round1_2026


def _hidden(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match, f"{name!r} hidden field not found"
    return match.group(1)


@pytest.fixture
def coach_client(tmp_path, monkeypatch):
    db_path = tmp_path / "vacancy-confirm.db"
    evidence_path = tmp_path / "evidence.json"
    result = bootstrap_round1_2026(f"sqlite:///{db_path}", evidence_path)

    monkeypatch.setenv("BBBFFL_DATABASE_URL", result.database_url)
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("BBBFFL_AFL_MODE", "replay")
    monkeypatch.setenv("BBBFFL_AFL_REPLAY_EVIDENCE_PATH", result.evidence_path)

    from app.main import app

    with TestClient(app) as client:
        login_page = client.get("/login")
        login = client.post(
            "/login",
            data={
                "email": result.coach_a.email,
                "password": result.coach_a_password,
                "csrf_token": _hidden(login_page.text, "csrf_token"),
            },
            cookies=login_page.cookies,
            follow_redirects=False,
        )
        cookies = {"bbbffl_session": login.cookies["bbbffl_session"]}
        yield client, result, cookies


def _lineup_url(result):
    return f"/coach/seasons/{result.season_id}/rounds/{result.bbbffl_round_id}/lineup"


def _full_squad_positions(client, result):
    squad = OwnershipRepository(client.app.state.database).current_squad(result.coach_a.season_entry_id)
    return {position: period.season_player_id for position, period in zip(POSITIONS, squad, strict=True)}


def _post(client, url, cookies, positions, action, *, extra=None):
    page = client.get(url, cookies=cookies)
    form = {f"position_{position}": player_id or "" for position, player_id in positions.items()}
    form.update(
        csrf_token=_hidden(page.text, "csrf_token"),
        draft_revision=_hidden(page.text, "draft_revision"),
        submission_version=_hidden(page.text, "submission_version"),
        action=action,
    )
    form.update(extra or {})
    return client.post(url, data=form, cookies=cookies, follow_redirects=False)


def _effective(database, result):
    lineup = database.execute(
        "SELECT lineup_id FROM weekly_lineup WHERE bbbffl_round_id=? AND season_entry_id=?",
        (result.bbbffl_round_id, result.coach_a.season_entry_id),
    ).fetchone()
    if lineup is None:
        return None
    return WeeklyLineupRepository(database).get_effective_submission(lineup["lineup_id"])


# 1. Incomplete draft still saves normally, without any confirmation step.
def test_incomplete_draft_saves_without_confirmation(coach_client):
    client, result, cookies = coach_client
    url = _lineup_url(result)
    full = _full_squad_positions(client, result)
    partial = {"F1": full["F1"]}
    saved = _post(client, url, cookies, partial, "save")
    assert saved.status_code == 303 and "draft-saved" in saved.headers["location"]
    assert _effective(client.app.state.database, result) is None


# 2 & 3. Submit with a vacancy asks for confirmation instead of submitting,
# and clearly identifies which position(s) are vacant.
def test_submit_with_one_vacancy_asks_for_confirmation_instead_of_submitting(coach_client):
    client, result, cookies = coach_client
    url = _lineup_url(result)
    full = _full_squad_positions(client, result)
    positions = dict(full)
    positions["F2"] = None
    response = _post(client, url, cookies, positions, "submit")
    assert response.status_code == 200
    assert "Confirm submission with vacant positions" in response.text
    assert "F2" in response.text
    assert "notice=submitted" not in response.text
    assert _effective(client.app.state.database, result) is None


# 4. Cancelling (never posting the confirmation) leaves submission history
# untouched -- only the private draft (already saved as part of ordinary
# save-then-submit handling) reflects the attempted content.
def test_cancelling_the_confirmation_creates_no_submission(coach_client):
    client, result, cookies = coach_client
    url = _lineup_url(result)
    full = _full_squad_positions(client, result)
    positions = dict(full)
    positions["M3"] = None
    _post(client, url, cookies, positions, "submit")
    assert _effective(client.app.state.database, result) is None

    # "Cancel" is just a link back to the ordinary page -- no POST at all.
    back = client.get(url, cookies=cookies)
    assert back.status_code == 200
    assert _effective(client.app.state.database, result) is None


# 5. Confirming creates the immutable partial submission, exactly as
# requested (the vacancy is recorded as `None`, never fabricated).
def test_confirming_creates_the_partial_submission(coach_client):
    client, result, cookies = coach_client
    url = _lineup_url(result)
    full = _full_squad_positions(client, result)
    positions = dict(full)
    positions["Ruck"] = None
    confirm_page = _post(client, url, cookies, positions, "submit")
    assert "Ruck" in confirm_page.text

    confirmed = _post(client, url, cookies, positions, "submit", extra={"confirm_vacancies": "1"})
    assert confirmed.status_code == 303 and "notice=submitted" in confirmed.headers["location"]
    effective = _effective(client.app.state.database, result)
    assert effective.version == 1
    assert effective.positions["Ruck"] is None
    assert all(effective.positions[p] == full[p] for p in POSITIONS if p != "Ruck")


# 6. Multiple vacancies are confirmed together in one step, not repeated
# per-position dialogs.
def test_multiple_vacancies_are_confirmed_together(coach_client):
    client, result, cookies = coach_client
    url = _lineup_url(result)
    full = _full_squad_positions(client, result)
    positions = dict(full)
    positions["F3"] = None
    positions["Tackler"] = None
    confirm_page = _post(client, url, cookies, positions, "submit")
    assert confirm_page.status_code == 200
    assert "F3" in confirm_page.text and "Tackler" in confirm_page.text

    confirmed = _post(client, url, cookies, positions, "submit", extra={"confirm_vacancies": "1"})
    assert confirmed.status_code == 303 and "notice=submitted" in confirmed.headers["location"]
    effective = _effective(client.app.state.database, result)
    assert effective.positions["F3"] is None and effective.positions["Tackler"] is None
    assert all(effective.positions[p] == full[p] for p in POSITIONS if p not in ("F3", "Tackler"))


# 7. A normal complete lineup still submits immediately -- no unnecessary
# confirmation step when nothing is vacant.
def test_complete_lineup_submits_without_confirmation(coach_client):
    client, result, cookies = coach_client
    url = _lineup_url(result)
    full = _full_squad_positions(client, result)
    submitted = _post(client, url, cookies, full, "submit")
    assert submitted.status_code == 303 and "notice=submitted" in submitted.headers["location"]
    effective = _effective(client.app.state.database, result)
    assert effective.version == 1
    assert effective.positions == full


# Interchange being unnamed is not flagged: only the eight ordinary
# (non-Interchange) positions trigger the confirmation step.
def test_vacant_interchange_alone_never_triggers_confirmation(coach_client):
    client, result, cookies = coach_client
    url = _lineup_url(result)
    full = _full_squad_positions(client, result)
    positions = dict(full)
    positions["Interchange"] = None
    submitted = _post(client, url, cookies, positions, "submit")
    assert submitted.status_code == 303 and "notice=submitted" in submitted.headers["location"]
    effective = _effective(client.app.state.database, result)
    assert effective.positions["Interchange"] is None
