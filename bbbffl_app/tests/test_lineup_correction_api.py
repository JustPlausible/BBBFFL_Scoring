"""HTTP-level coverage for issue #137's Scorer/Admin locked-lineup
correction surface (`app/routes/lineup_correction.py`), exercised through
the real `/api/admin/lineup-correction` API via `app.main.app` -- the same
authenticated acting-context flow
`tests/test_opening_round_operations_api.py::_authenticate_replay_operator`
already establishes for a different operations surface.
"""

import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from tests.test_competition_lifecycle import configured

ADMIN = ActorContext.anonymous_operator("admin")


class _NoMatchesAflClient:
    """Duck-typed AFL client with no live network dependency: no AFL match
    ever resolves, so every position reads back as lock-state
    INDETERMINATE rather than the test hitting a real afl-api host. Only
    the correction workflow's HTTP wiring (auth, CSRF, season-scoping,
    response shape) is under test here -- lockout mechanics themselves are
    covered exhaustively at the domain level in tests/test_lineup_correction.py."""

    def get_matches(self, round_id):
        return []


@pytest.fixture
def correction_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        monkeypatch.setattr(client.app.state, "afl_client", _NoMatchesAflClient())
        yield client
    db_path.unlink(missing_ok=True)


def _season_scope(db, round_id):
    return dict(
        db.execute(
            "SELECT c.season_id, c.competition_id FROM bbbffl_round r "
            "JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
            (round_id,),
        ).fetchone()
    )


def _own(db, season_id, entry, canonical_id, name):
    player = PlayerPoolRepository(db).refresh_player(season_id, canonical_id, name)
    OwnershipRepository(db).acquire(player.season_player_id, entry.season_entry_id)
    return player


def _login_coach(client, email, password="correct horse battery staple"):
    login_page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
    login = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": token},
        cookies=login_page.cookies,
        follow_redirects=False,
    )
    session = login.cookies["bbbffl_session"]
    account = client.get("/account", cookies={"bbbffl_session": session})
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', account.text).group(1)
    cookies = {"bbbffl_session": session, "bbbffl_csrf": account.cookies["bbbffl_csrf"]}
    headers = {"X-CSRF-Token": csrf}
    return cookies, headers


def _authenticate_scorer(client, season_id):
    """Real authenticated-coach acting-context flow, granting `scorer`
    scoped to `season_id` only."""
    app = client.app
    operator = app.state.identities.create_coach("Authenticated Scorer", email="scorer@example.com")
    app.state.credentials.set_password(operator.coach_id, "correct horse battery staple", actor=ADMIN)
    app.state.role_grants.grant(operator.coach_id, "scorer", season_id=season_id, actor=ADMIN)
    cookies, headers = _login_coach(client, "scorer@example.com")
    assert (
        client.post("/api/context/role", json={"role": "scorer"}, cookies=cookies, headers=headers).status_code == 200
    )
    return operator, cookies, headers


def _authenticate_plain_coach(client, email="coach@example.com"):
    app = client.app
    coach = app.state.identities.create_coach("Ordinary Coach", email=email)
    app.state.credentials.set_password(coach.coach_id, "correct horse battery staple", actor=ADMIN)
    cookies, headers = _login_coach(client, email)
    return coach, cookies, headers


def _setup_round(db, year, afl_round):
    round_, entries = configured(db, year, afl_round)
    scope = _season_scope(db, round_.bbbffl_round_id)
    from app.competition_lifecycle import CompetitionLifecycleRepository

    lifecycle = CompetitionLifecycleRepository(db)
    lifecycle.create_ordinary_round(round_.bbbffl_round_id)
    lifecycle.transition(round_.bbbffl_round_id, "open")
    return round_, entries, scope, lifecycle


def _submit_lineup(db, scope, round_id, entry, players):
    from app.lineups import WeeklyLineupRepository

    lineups = WeeklyLineupRepository(db)
    positions = {"F1": players[0].season_player_id, "F2": players[1].season_player_id}
    draft = lineups.save_draft(
        scope["season_id"], scope["competition_id"], round_id, entry.season_entry_id, positions, expected_revision=0
    )
    return lineups.submit(draft.lineup_id, expected_draft_revision=draft.revision, expected_submission_version=0)


def test_scorer_can_view_and_apply_a_correction_end_to_end(correction_client):
    """Full browser-level workflow: select round/team, view current lineup,
    apply an atomic correction with a reason, and see the response already
    reflect the corrected authoritative state (issue #137's "reload all
    read surfaces" requirement)."""
    client = correction_client
    db = client.app.state.database
    round_, entries, scope, lifecycle = _setup_round(db, 2701, 2701)
    entry = entries[0]
    OwnershipRepository(db).configure_squad_limit(scope["season_id"], 10)
    p1 = _own(db, scope["season_id"], entry, 810001, "Player One")
    p2 = _own(db, scope["season_id"], entry, 810002, "Player Two")
    submitted = _submit_lineup(db, scope, round_.bbbffl_round_id, entry, [p1, p2])

    _operator, cookies, headers = _authenticate_scorer(client, scope["season_id"])

    listing = client.get(f"/api/admin/lineup-correction/{round_.bbbffl_round_id}", cookies=cookies, headers=headers)
    assert listing.status_code == 200
    assert entry.season_entry_id in {e["season_entry_id"] for e in listing.json()["entries"]}

    detail = client.get(
        f"/api/admin/lineup-correction/{round_.bbbffl_round_id}/{entry.season_entry_id}",
        cookies=cookies,
        headers=headers,
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["expected_submission_version"] == submitted.version
    f1_slot = next(s for s in body["slots"] if s["position"] == "F1")
    assert f1_slot["season_player_id"] == p1.season_player_id
    assert f1_slot["player_display_name"] == "Player One"
    assert body["correction_history"] == []

    correction = client.post(
        f"/api/admin/lineup-correction/{round_.bbbffl_round_id}/{entry.season_entry_id}/correct",
        json={
            "expected_submission_version": submitted.version,
            "position_changes": {"F1": p2.season_player_id, "F2": p1.season_player_id},
            "reason": "League chat confirmed the two forwards were transposed at delegated entry time",
        },
        cookies=cookies,
        headers=headers,
    )
    assert correction.status_code == 200
    corrected = correction.json()
    assert corrected["expected_submission_version"] == submitted.version + 1
    corrected_f1 = next(s for s in corrected["slots"] if s["position"] == "F1")
    assert corrected_f1["season_player_id"] == p2.season_player_id
    assert len(corrected["correction_history"]) == 1
    assert corrected["correction_history"][0]["reason"].startswith("League chat")

    # Reloading the read surface independently shows the same corrected state.
    reread = client.get(
        f"/api/admin/lineup-correction/{round_.bbbffl_round_id}/{entry.season_entry_id}",
        cookies=cookies,
        headers=headers,
    )
    assert reread.json()["expected_submission_version"] == submitted.version + 1


def test_unauthorized_coach_cannot_access_correction_endpoints(correction_client):
    """Acceptance #8: an ordinary authenticated coach, with no scorer/admin/
    replay-operator grant at all, is refused."""
    client = correction_client
    db = client.app.state.database
    round_, entries, scope, lifecycle = _setup_round(db, 2702, 2702)
    entry = entries[0]

    _coach, cookies, headers = _authenticate_plain_coach(client)

    listing = client.get(f"/api/admin/lineup-correction/{round_.bbbffl_round_id}", cookies=cookies, headers=headers)
    assert listing.status_code == 403

    correction = client.post(
        f"/api/admin/lineup-correction/{round_.bbbffl_round_id}/{entry.season_entry_id}/correct",
        json={"expected_submission_version": 0, "position_changes": {}, "reason": "should never be authorised"},
        cookies=cookies,
        headers=headers,
    )
    assert correction.status_code == 403


def test_wrong_season_scoped_grant_is_refused(correction_client):
    """Acceptance #9: a Scorer grant scoped to a different season must not
    confer authority over this round's season."""
    client = correction_client
    db = client.app.state.database
    round_, _entries, scope, _lifecycle = _setup_round(db, 2703, 2703)
    _other_round, _other_entries, other_scope, _other_lifecycle = _setup_round(db, 2704, 2704)
    assert other_scope["season_id"] != scope["season_id"]

    _operator, cookies, headers = _authenticate_scorer(client, other_scope["season_id"])

    response = client.get(f"/api/admin/lineup-correction/{round_.bbbffl_round_id}", cookies=cookies, headers=headers)
    assert response.status_code == 403


def test_csrf_failure_is_rejected(correction_client):
    """Acceptance #11: a valid authenticated/authorised session with a
    missing or wrong CSRF header is refused on the mutating endpoint."""
    client = correction_client
    db = client.app.state.database
    round_, entries, scope, lifecycle = _setup_round(db, 2705, 2705)
    entry = entries[0]
    OwnershipRepository(db).configure_squad_limit(scope["season_id"], 10)
    p1 = _own(db, scope["season_id"], entry, 810101, "Player One")
    p2 = _own(db, scope["season_id"], entry, 810102, "Player Two")
    submitted = _submit_lineup(db, scope, round_.bbbffl_round_id, entry, [p1, p2])

    _operator, cookies, _headers = _authenticate_scorer(client, scope["season_id"])

    response = client.post(
        f"/api/admin/lineup-correction/{round_.bbbffl_round_id}/{entry.season_entry_id}/correct",
        json={
            "expected_submission_version": submitted.version,
            "position_changes": {"F1": p2.season_player_id, "F2": p1.season_player_id},
            "reason": "attempted without a valid CSRF token",
        },
        cookies=cookies,
        headers={"X-CSRF-Token": "not-the-real-token"},
    )
    assert response.status_code == 403
    # Nothing was mutated: the effective version is unchanged.
    detail = client.get(
        f"/api/admin/lineup-correction/{round_.bbbffl_round_id}/{entry.season_entry_id}",
        cookies=cookies,
        headers={"X-CSRF-Token": "not-the-real-token"},
    )
    assert detail.status_code == 200
    assert detail.json()["expected_submission_version"] == submitted.version


def test_correction_page_renders_and_issues_csrf_cookie(correction_client):
    client = correction_client
    page = client.get("/scorer/lineup-correction")
    assert page.status_code == 200
    assert "bbbffl_csrf" in page.cookies
    assert "Locked Lineup Correction" in page.text
