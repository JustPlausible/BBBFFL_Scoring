"""Account-page lineup status coverage (issue #90): `/account` must give the
coach a truthful, cheap, authoritative account of each round's draft/
submission state -- derived from the same draft-revision/submission facts
`coach_lineup.html` already uses -- without implying a submission that
never happened, and without confusing a private post-submission edit for a
new authoritative version.

Uses the same persistent Round 1 rehearsal bootstrap as
tests/test_round1_rehearsal_acceptance.py (real owned squad, real AFL
replay evidence) so a genuine `submit()` -- lockouts and validation
included -- can run without reimplementing that evidence stubbing here.
"""

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.coach_lineup import (
    ACCOUNT_STATE_DRAFT_SAVED,
    ACCOUNT_STATE_NOT_SUBMITTED,
    ACCOUNT_STATE_SUBMITTED,
    ACCOUNT_STATE_SUBMITTED_WITH_CHANGES,
    CoachLineupService,
)
from app.player_pool import OwnershipRepository
from scripts.bootstrap_round1_2026 import bootstrap_round1_2026
from tests.db_helpers import migrated_connection
from tests.test_opening_round import nominate_bl_2024, setup_scope


def _hidden(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match, f"{name!r} hidden field not found"
    return match.group(1)


def _round_status_text(html: str) -> str:
    match = re.search(r'<span class="round-status">([^<]+)</span>', html)
    assert match, "round-status span not found"
    return match.group(1).strip()


def _round_meta_text(html: str) -> str | None:
    match = re.search(r'<span class="round-meta">([^<]+)</span>', html)
    return match.group(1).strip() if match else None


@pytest.fixture
def rehearsal(tmp_path, monkeypatch):
    db_path = tmp_path / "rehearsal.db"
    evidence_path = tmp_path / "evidence.json"
    result = bootstrap_round1_2026(f"sqlite:///{db_path}", evidence_path)

    monkeypatch.setenv("BBBFFL_DATABASE_URL", result.database_url)
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("BBBFFL_AFL_MODE", "replay")
    monkeypatch.setenv("BBBFFL_AFL_REPLAY_EVIDENCE_PATH", result.evidence_path)

    from app.main import app

    with TestClient(app) as client:
        yield client, result


def _login(client, email, password):
    login_page = client.get("/login")
    login = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": _hidden(login_page.text, "csrf_token")},
        cookies=login_page.cookies,
        follow_redirects=False,
    )
    assert login.status_code == 303 and login.headers["location"] == "/account"
    return {"bbbffl_session": login.cookies.get("bbbffl_session")}


def _list_rounds(client, coach_id):
    """A freshly constructed service -- as a new request/session would build
    -- reading directly off the persisted database, never off route state."""
    service = CoachLineupService(client.app.state.database, client.app.state.afl_client)
    return service.list_rounds(coach_id)


def test_authenticated_coach_sees_their_available_rounds(rehearsal):
    client, result = rehearsal
    cookies = _login(client, result.coach_a.email, result.coach_a_password)

    account = client.get("/account", cookies=cookies)
    assert account.status_code == 200

    [summary] = _list_rounds(client, result.coach_a.coach_id)
    assert summary["season_id"] == result.season_id
    assert summary["round_id"] == result.bbbffl_round_id
    lineup_url = f"/coach/seasons/{result.season_id}/rounds/{result.bbbffl_round_id}/lineup"
    assert lineup_url in account.text


def test_round_with_no_submission_is_shown_as_not_submitted(rehearsal):
    client, result = rehearsal
    cookies = _login(client, result.coach_a.email, result.coach_a_password)

    [summary] = _list_rounds(client, result.coach_a.coach_id)
    assert summary["state"] == ACCOUNT_STATE_NOT_SUBMITTED
    assert summary["submission_version"] is None
    assert summary["submitted_at"] is None

    account = client.get("/account", cookies=cookies)
    assert _round_status_text(account.text) == "Not submitted"


def test_auto_created_empty_draft_does_not_read_as_a_saved_draft(rehearsal):
    """`ensure_draft` creates an empty revision-1 draft the first time the
    lineup page is viewed. That alone must not read as "the coach saved
    something" -- see issue #90's "Important distinction" section."""
    client, result = rehearsal
    cookies = _login(client, result.coach_a.email, result.coach_a_password)
    lineup_url = f"/coach/seasons/{result.season_id}/rounds/{result.bbbffl_round_id}/lineup"

    lineup_page = client.get(lineup_url, cookies=cookies)
    assert lineup_page.status_code == 200

    [summary] = _list_rounds(client, result.coach_a.coach_id)
    assert summary["draft_revision"] == 1
    assert summary["state"] == ACCOUNT_STATE_NOT_SUBMITTED

    account = client.get("/account", cookies=cookies)
    assert _round_status_text(account.text) == "Not submitted"


def test_account_status_progression_save_submit_edit_resubmit(rehearsal):
    client, result = rehearsal
    cookies = _login(client, result.coach_a.email, result.coach_a_password)
    lineup_url = f"/coach/seasons/{result.season_id}/rounds/{result.bbbffl_round_id}/lineup"
    squad = OwnershipRepository(client.app.state.database).current_squad(result.coach_a.season_entry_id)
    from app.lineups import POSITIONS

    positions = {position: period.season_player_id for position, period in zip(POSITIONS, squad, strict=True)}

    # 1. Save a private draft without submitting.
    lineup_page = client.get(lineup_url, cookies=cookies)
    form = {f"position_{position}": player_id for position, player_id in positions.items()}
    form["csrf_token"] = _hidden(lineup_page.text, "csrf_token")
    form["draft_revision"] = _hidden(lineup_page.text, "draft_revision")
    form["action"] = "save"
    saved = client.post(lineup_url, data=form, cookies=cookies, follow_redirects=False)
    assert saved.status_code == 303 and "notice=draft-saved" in saved.headers["location"]

    [summary] = _list_rounds(client, result.coach_a.coach_id)
    assert summary["state"] == ACCOUNT_STATE_DRAFT_SAVED
    assert summary["submission_version"] is None

    account = client.get("/account", cookies=cookies)
    status_text = _round_status_text(account.text)
    assert status_text == "Draft saved"
    assert "Submitted" not in status_text

    # 2. Submit -- an authoritative version now exists.
    submit_page = client.get(lineup_url, cookies=cookies)
    form["csrf_token"] = _hidden(submit_page.text, "csrf_token")
    form["draft_revision"] = _hidden(submit_page.text, "draft_revision")
    form["submission_version"] = _hidden(submit_page.text, "submission_version")
    form["action"] = "submit"
    submitted = client.post(lineup_url, data=form, cookies=cookies, follow_redirects=False)
    assert submitted.status_code == 303 and "notice=submitted" in submitted.headers["location"]

    [summary] = _list_rounds(client, result.coach_a.coach_id)
    assert summary["state"] == ACCOUNT_STATE_SUBMITTED
    assert summary["submission_version"] == 1
    assert summary["submitted_at"]

    account = client.get("/account", cookies=cookies)
    assert _round_status_text(account.text) == "Submitted"
    meta = _round_meta_text(account.text)
    assert meta is not None and "Submitted version 1" in meta and summary["submitted_at"] in meta

    # 3. Sign out and back in: the authoritative state survives, freshly
    # reconstructed from persisted data, not transient route/session state.
    logout_csrf = _hidden(account.text, "csrf_token")
    client.post(
        "/logout",
        data={"csrf_token": logout_csrf},
        cookies={**cookies, "bbbffl_csrf": account.cookies.get("bbbffl_csrf")},
        follow_redirects=False,
    )
    cookies = _login(client, result.coach_a.email, result.coach_a_password)
    account = client.get("/account", cookies=cookies)
    assert _round_status_text(account.text) == "Submitted"

    # 4. Edit and save again without resubmitting: private changes sit on
    # top of the still-authoritative submitted version.
    edit_page = client.get(lineup_url, cookies=cookies)
    form["csrf_token"] = _hidden(edit_page.text, "csrf_token")
    form["draft_revision"] = _hidden(edit_page.text, "draft_revision")
    form["action"] = "save"
    client.post(lineup_url, data=form, cookies=cookies, follow_redirects=False)

    [summary] = _list_rounds(client, result.coach_a.coach_id)
    assert summary["state"] == ACCOUNT_STATE_SUBMITTED_WITH_CHANGES
    assert summary["submission_version"] == 1

    account = client.get("/account", cookies=cookies)
    assert _round_status_text(account.text) == "Submitted + private changes"
    meta = _round_meta_text(account.text)
    assert meta is not None and "Submitted version 1" in meta

    # 5. Submitting again updates the authoritative version shown.
    resubmit_page = client.get(lineup_url, cookies=cookies)
    form["csrf_token"] = _hidden(resubmit_page.text, "csrf_token")
    form["draft_revision"] = _hidden(resubmit_page.text, "draft_revision")
    form["submission_version"] = _hidden(resubmit_page.text, "submission_version")
    form["action"] = "submit"
    client.post(lineup_url, data=form, cookies=cookies, follow_redirects=False)

    [summary] = _list_rounds(client, result.coach_a.coach_id)
    assert summary["state"] == ACCOUNT_STATE_SUBMITTED
    assert summary["submission_version"] == 2

    account = client.get("/account", cookies=cookies)
    assert _round_status_text(account.text) == "Submitted"
    assert "Submitted version 2" in _round_meta_text(account.text)


def test_opening_round_preload_alone_is_not_read_as_a_saved_draft():
    """An active Opening Round nomination (issue #69) makes `ensure_draft`'s
    `preload_target_lineup` call advance the draft's revision past 1 the
    first time the coach merely *views* the round -- with no Save/Submit
    action at all. `revision > 1` on its own would misreport that as
    "Draft saved"; the account summary must still call it "Not submitted"."""
    database = migrated_connection()
    _, round_, entries, scope = setup_scope(database, 2024, 956)
    entry = entries[0]
    nominate_bl_2024(database, scope["season_id"], round_.bbbffl_round_id, entry)
    coach = database.execute(
        "SELECT coach_id FROM season_entry_coach_history WHERE season_entry_id=? AND ended_at IS NULL",
        (entry.season_entry_id,),
    ).fetchone()

    service = CoachLineupService(database, afl_client=SimpleNamespace())
    entry_context = service.resolve(coach["coach_id"], scope["season_id"], round_.bbbffl_round_id)
    draft = service.ensure_draft(scope["season_id"], round_.bbbffl_round_id, entry_context)

    # Confirms this test actually exercises the preload-bumps-revision
    # behaviour, not a no-op.
    assert draft.revision > 1

    [summary] = service.list_rounds(coach["coach_id"])
    assert summary["state"] == ACCOUNT_STATE_NOT_SUBMITTED
    assert summary["draft_revision"] == draft.revision
