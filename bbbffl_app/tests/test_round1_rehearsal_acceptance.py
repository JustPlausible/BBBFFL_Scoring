"""HTTP acceptance vertical for the persistent Round 1 rehearsal bootstrap
(issue #85): proves the bootstrapped state is reachable through the real
browser/API routes, not just through direct repository calls --

    coach login -> /account -> lineup save/submit ->
    scorer Round Centre discovery -> calculate -> lifecycle transition ->
    sign-off -> public Round Centre/ladder

Complements tests/test_bootstrap_round1_2026.py (bootstrap state/
duplicate-protection/replay-mode coverage) and the exhaustive domain-level
suites (tests/test_round_review.py, tests/test_lineups.py, tests/
test_lineup_validation.py, etc.) -- this test proves the routing/wiring
end-to-end, not the scoring engine, which stays covered where it already
is.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.lineups import POSITIONS
from app.player_pool import OwnershipRepository
from scripts.bootstrap_round1_2026 import bootstrap_round1_2026


def _hidden(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match, f"{name!r} hidden field not found"
    return match.group(1)


@pytest.fixture
def rehearsal_client(tmp_path, monkeypatch):
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


def test_round1_rehearsal_browser_vertical(rehearsal_client):
    client, result = rehearsal_client
    lineup_url = f"/coach/seasons/{result.season_id}/rounds/{result.bbbffl_round_id}/lineup"

    # 1. Coach A authenticates through the real /login form.
    login_page = client.get("/login")
    assert login_page.status_code == 200
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
    assert login.status_code == 303 and login.headers["location"] == "/account"
    session_cookie = login.cookies.get("bbbffl_session")
    assert session_cookie
    cookies = {"bbbffl_session": session_cookie}

    # 2. /account discovers the Round 1 lineup link -- no manually entered
    # database IDs.
    account = client.get("/account", cookies=cookies)
    assert account.status_code == 200
    assert lineup_url in account.text

    # 3. Save a legal draft (Coach A's own owned squad, one player per
    # slot -- any arrangement is legal; app.player_pool records no
    # positional eligibility beyond ownership), then submit it.
    squad = OwnershipRepository(client.app.state.database).current_squad(result.coach_a.season_entry_id)
    assert len(squad) == len(POSITIONS)
    positions = {position: period.season_player_id for position, period in zip(POSITIONS, squad, strict=True)}

    lineup_page = client.get(lineup_url, cookies=cookies)
    assert lineup_page.status_code == 200
    form = {f"position_{position}": player_id for position, player_id in positions.items()}
    form["csrf_token"] = _hidden(lineup_page.text, "csrf_token")
    form["draft_revision"] = _hidden(lineup_page.text, "draft_revision")
    form["action"] = "save"
    save = client.post(lineup_url, data=form, cookies=cookies, follow_redirects=False)
    assert save.status_code == 303 and "notice=draft-saved" in save.headers["location"], save.text

    submit_page = client.get(lineup_url, cookies=cookies)
    form["csrf_token"] = _hidden(submit_page.text, "csrf_token")
    form["draft_revision"] = _hidden(submit_page.text, "draft_revision")
    form["submission_version"] = _hidden(submit_page.text, "submission_version")
    form["action"] = "submit"
    submit = client.post(lineup_url, data=form, cookies=cookies, follow_redirects=False)
    assert submit.status_code == 303 and "notice=submitted" in submit.headers["location"], submit.text

    # 4. Scorer Round Centre discovers Round 1 and all five matchups without
    # any manually entered database IDs. Coach A's own session cookie is
    # still attached to this shared TestClient (matching a real browser
    # that stays signed in) and must grant no scorer/admin authority of its
    # own (see app/authorization.py) -- the scorer surface authenticates the
    # same way the Round Centre's own JS does, an explicit (possibly empty
    # in dev, since BBBFFL_ADMIN_TOKEN is unset) `X-Admin-Token` header.
    scorer = {"X-Admin-Token": ""}
    centre_page = client.get("/scorer/round-centre")
    assert centre_page.status_code == 200
    discovered = client.get("/api/admin/round-review", headers=scorer).json()
    assert [item["bbbffl_round_id"] for item in discovered] == [result.bbbffl_round_id]
    review = client.get(f"/api/admin/round-review/{result.bbbffl_round_id}", headers=scorer).json()
    assert len(review["matchups"]) == 5
    assert review["state"] == "open"

    # 5. Advance the round's ordinary lifecycle (open -> live -> review)
    # and calculate through the newly wired scorer endpoints.
    for target in ("live", "review"):
        advanced = client.post(
            f"/api/admin/round-review/{result.bbbffl_round_id}/transition", json={"target": target}, headers=scorer
        )
        assert advanced.status_code == 200, advanced.text
        assert advanced.json()["state"] == target

    calculated = client.post(f"/api/admin/round-review/{result.bbbffl_round_id}/calculate", headers=scorer)
    assert calculated.status_code == 200, calculated.text
    review = calculated.json()
    assert review["ready_for_signoff"], review["blockers"]
    assert all(matchup["calculation_revision"] is not None for matchup in review["matchups"])

    # 6. Authoritative sign-off through the existing service boundary.
    signoff = client.post(
        f"/api/admin/round-review/{result.bbbffl_round_id}/signoff", json={"reason": "rehearsal"}, headers=scorer
    )
    assert signoff.status_code == 200, signoff.text
    signed_off = client.get(f"/api/admin/round-review/{result.bbbffl_round_id}", headers=scorer).json()
    assert signed_off["state"] == "final"
    assert all(matchup["official_result"]["version"] == 1 for matchup in signed_off["matchups"])

    # 7. Official Round 1 results and the ladder are publicly visible.
    public_round = client.get(f"/api/public/seasons/{result.season_id}/rounds/{result.bbbffl_round_id}")
    assert public_round.status_code == 200
    public_body = public_round.json()
    assert public_body["round_state"] == "final"
    assert all(matchup["status"] == "official" for matchup in public_body["matchups"])

    public_page = client.get(f"/seasons/{result.season_id}/rounds/{result.bbbffl_round_id}")
    assert public_page.status_code == 200

    season_overview = client.get(f"/seasons/{result.season_id}", follow_redirects=False)
    assert season_overview.status_code == 302
    assert season_overview.headers["location"] == f"/seasons/{result.season_id}/rounds/{result.bbbffl_round_id}"

    ladder = client.get(f"/api/public/seasons/{result.season_id}/rounds/{result.bbbffl_round_id}/ladder")
    assert ladder.status_code == 200
    ladder_body = ladder.json()
    assert len(ladder_body["rows"]) == 10
    assert sum(row["played"] for row in ladder_body["rows"]) == 10
