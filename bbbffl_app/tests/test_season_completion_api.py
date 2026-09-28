"""Issue #240 HTTP/browser-flow coverage: the Season completion page and JSON
API (`app.routes.season_completion`) driven through real HTTP requests and
real Scorer/Administrator/Secretary/Coach sessions -- proves the wiring
(role boundary, season-scoped grants, CSRF, refusal shape, preview-versus-
confirmation semantics) works end-to-end, the same way `tests/test_season_
activation_api.py` proves `app.routes.season_activation`'s wiring rather
than only `app.season_activation`'s own functions. Domain/service-level
readiness, award and atomicity coverage lives in `tests/test_season_
completion.py`; this file focuses on the browser surface, authorization and
the read-only archival-verification endpoint."""

import json
import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import ActorContext, AuditEventRepository
from app.authorization import Role
from tests.finals_helpers import correct_official_result
from tests.finals_seeding_helpers import build_2026_replay_season
from tests.season_completion_helpers import build_completable_season

PASSWORD = "correct horse battery staple"  # noqa: S105 -- test fixture password, not a real credential


@pytest.fixture
def completion_client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as client:
        yield client
    db_path.unlink(missing_ok=True)


def _extract(pattern, html):
    match = re.search(pattern, html)
    assert match, pattern
    return match.group(1)


def _seed_completable(client, year, **kwargs):
    """A fully completable season (active, every finals week and SS1-SS4
    final), built directly against the running app's own database
    connection -- mirrors `test_season_activation_api.py`'s `_seed`."""
    return build_completable_season(database=client.app.state.database, year=year, **kwargs)


def _seed_not_started(client, year):
    """`active`, but no finals bracket or SuperScore stream exists yet --
    the concrete shape of "ordinary-season requirements incomplete" (see
    `tests/test_season_completion.py`'s identical reasoning)."""
    from app.season import SeasonRepository

    built = build_2026_replay_season(database=client.app.state.database, year=year)
    SeasonRepository(client.app.state.database).transition_lifecycle(
        built["season"].season_id, "active", actor=ActorContext.anonymous_operator("test"), reason="activate"
    )
    return built


def _login(client, email):
    login_page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "email": email,
            "password": PASSWORD,
            "csrf_token": _extract(r'name="csrf_token" value="([^"]+)"', login_page.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def _role_session(client, role: Role, *, email, season_id=None):
    state = client.app.state
    coach = state.identities.create_coach(f"Test {role.value}", email=email)
    state.credentials.set_password(coach.coach_id, PASSWORD, actor=ActorContext.anonymous_operator("admin"))
    state.role_grants.grant(
        coach.coach_id, role.value, season_id=season_id, actor=ActorContext.anonymous_operator("admin")
    )
    _login(client, email)
    account = client.get("/account")
    switch = client.post(
        "/api/context/role",
        json={"role": role.value},
        headers={"X-CSRF-Token": _extract(r'name="csrf_token" value="([^"]+)"', account.text)},
    )
    assert switch.status_code == 200, switch.text
    return coach.coach_id


def _plain_coach_session(client, *, email):
    state = client.app.state
    coach = state.identities.create_coach("Plain Coach", email=email)
    state.credentials.set_password(coach.coach_id, PASSWORD, actor=ActorContext.anonymous_operator("admin"))
    _login(client, email)
    return coach.coach_id


def _completion_csrf(client, season_id):
    page = client.get(f"/scorer/season-completion/{season_id}")
    assert page.status_code == 200
    assert "Season completion" in page.text
    return {"X-CSRF-Token": json.loads(_extract(r"const csrf=(\"[^\"]+\")", page.text))}


def _lifecycle_state(client, season_id):
    return client.app.state.seasons.get_season(season_id).lifecycle_state


def _audit_count(client, season_id):
    return len(AuditEventRepository(client.app.state.database).list_events(entity_type="season", entity_id=season_id))


# -- Page and readiness preview -----------------------------------------------


def test_completion_page_is_reachable(completion_client):
    client = completion_client
    season_id = _seed_completable(client, 8001)["season"].season_id
    page = client.get(f"/scorer/season-completion/{season_id}")
    assert page.status_code == 200
    assert "Season completion" in page.text


def test_readiness_preview_reports_ready_for_a_fully_completable_season(completion_client):
    client = completion_client
    season_id = _seed_completable(client, 8002)["season"].season_id
    response = client.get(f"/api/scorer/season-completion/{season_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ready"] is True
    assert body["lifecycle_state"] == "active"
    assert body["diagnostic"] is None
    assert body["round_states"]
    assert all(state == "final" for state in body["round_states"].values())


def test_readiness_preview_reports_a_diagnostic_when_ordinary_season_is_incomplete(completion_client):
    """No finals bracket exists yet -- the concrete shape of "ordinary-
    season requirements incomplete", since a finals bracket can only be
    created once every regular-season round is final."""
    client = completion_client
    season_id = _seed_not_started(client, 8003)["season"].season_id
    response = client.get(f"/api/scorer/season-completion/{season_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ready"] is False
    assert "no finals bracket" in body["diagnostic"]


def test_unknown_season_is_404_everywhere(completion_client):
    client = completion_client
    assert client.get("/api/scorer/season-completion/not-a-real-season").status_code == 404
    assert (
        client.post("/api/scorer/season-completion/not-a-real-season/complete", json={"reason": "x"}).status_code == 404
    )
    assert client.get("/api/scorer/season-completion/not-a-real-season/archival-verification").status_code == 404


# -- Browser preview never mutates; only an explicit confirmation does -------


def test_visiting_the_page_and_reading_the_preview_repeatedly_never_completes_the_season(completion_client):
    client = completion_client
    season_id = _seed_completable(client, 8004)["season"].season_id
    before_events = _audit_count(client, season_id)

    for _ in range(3):
        client.get(f"/scorer/season-completion/{season_id}")
        response = client.get(f"/api/scorer/season-completion/{season_id}")
        assert response.json()["ready"] is True

    assert _lifecycle_state(client, season_id) == "active"
    assert _audit_count(client, season_id) == before_events

    complete = client.post(f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "now confirm"})
    assert complete.status_code == 200, complete.text
    assert _lifecycle_state(client, season_id) == "completed"


# -- Successful completion -----------------------------------------------------


def test_ambient_operator_can_complete_a_ready_season_and_response_shows_terminal_state(completion_client):
    client = completion_client
    built = _seed_completable(client, 8005)
    season_id = built["season"].season_id

    response = client.post(f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "results are final"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["season"]["lifecycle_state"] == "completed"
    assert body["completed_season_version"] > 0
    assert body["completion_event_id"]
    assert body["premiership"]["season_entry_id"]
    assert body["premiership"]["team_name"]
    assert body["wooden_spoon"]["season_entry_id"]
    assert body["wooden_spoon"]["team_name"]
    assert _lifecycle_state(client, season_id) == "completed"

    # The completion-event identity is stable and independently re-derivable
    # from the read-only archival verifier.
    verify = client.get(f"/api/scorer/season-completion/{season_id}/archival-verification")
    assert verify.status_code == 200, verify.text
    assert verify.json()["completion_event_id"] == body["completion_event_id"]
    assert verify.json()["completed_season_version"] == body["completed_season_version"]


def test_scorer_session_can_complete_with_reason_and_csrf(completion_client):
    client = completion_client
    season_id = _seed_completable(client, 8006)["season"].season_id
    _role_session(client, Role.SCORER, email="scorer-8006@example.com", season_id=season_id)
    headers = _completion_csrf(client, season_id)
    before_events = _audit_count(client, season_id)

    response = client.post(
        f"/api/scorer/season-completion/{season_id}/complete",
        json={"reason": "Scorer confirms Finals and SuperScore are final"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert _lifecycle_state(client, season_id) == "completed"
    # Completion, plus the premiership and wooden-spoon recording events.
    assert _audit_count(client, season_id) > before_events


def test_administrator_session_can_complete(completion_client):
    client = completion_client
    season_id = _seed_completable(client, 8007)["season"].season_id
    _role_session(client, Role.ADMIN, email="admin-8007@example.com")
    headers = _completion_csrf(client, season_id)

    response = client.post(
        f"/api/scorer/season-completion/{season_id}/complete",
        json={"reason": "Administrator confirms completion"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert _lifecycle_state(client, season_id) == "completed"


# -- Refusal: ordinary/Finals/SuperScore incomplete, unchanged, no mutation --


def test_ordinary_season_incomplete_returns_409_and_changes_nothing(completion_client):
    client = completion_client
    season_id = _seed_not_started(client, 8008)["season"].season_id
    before_events = _audit_count(client, season_id)

    response = client.post(f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "too early"})

    assert response.status_code == 409, response.text
    assert "no finals bracket" in response.json()["detail"]
    assert _lifecycle_state(client, season_id) == "active"
    assert _audit_count(client, season_id) == before_events


def test_finals_incomplete_returns_409_and_changes_nothing(completion_client):
    client = completion_client
    built = _seed_completable(client, 8009)
    database, season_id = built["database"], built["season"].season_id
    week3_round_id = database.execute(
        "SELECT bbbffl_round_id FROM finals_bracket_week WHERE bracket_id=? AND week_number=3",
        (built["bracket"].bracket_id,),
    ).fetchone()["bbbffl_round_id"]
    from sqlalchemy import text

    with database.engine.begin() as conn:
        conn.execute(
            text("UPDATE bbbffl_round_lifecycle SET state='review' WHERE bbbffl_round_id=:rid"), {"rid": week3_round_id}
        )
    before_events = _audit_count(client, season_id)

    response = client.post(f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "attempt anyway"})

    assert response.status_code == 409, response.text
    assert "not yet final" in response.json()["detail"]
    assert _lifecycle_state(client, season_id) == "active"
    assert _audit_count(client, season_id) == before_events


def test_superscore_incomplete_returns_409_and_changes_nothing(completion_client):
    client = completion_client
    built = _seed_completable(client, 8010)
    database, season_id = built["database"], built["season"].season_id
    ss2_round_id = built["superscore_rounds"][2]
    from sqlalchemy import text

    with database.engine.begin() as conn:
        conn.execute(
            text("UPDATE bbbffl_round_lifecycle SET state='review' WHERE bbbffl_round_id=:rid"), {"rid": ss2_round_id}
        )
    before_events = _audit_count(client, season_id)

    response = client.post(f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "attempt anyway"})

    assert response.status_code == 409, response.text
    assert _lifecycle_state(client, season_id) == "active"
    assert _audit_count(client, season_id) == before_events


def test_internal_consistency_tie_returns_409_not_500_and_changes_nothing(completion_client):
    """A `SeasonAwardError` (here `UnresolvedWoodenSpoonTieError`) raised
    mid-transaction by `complete_season`'s step 3 must surface as a
    resolvable 409 through this route, not an internal-server error -- and
    the whole transaction (including the already-inserted premiership) must
    roll back. See `tests/test_season_completion.py`'s identical domain-
    level construction for why forcing every match either bottom entry
    played to a fixed score guarantees an exact tie regardless of the
    underlying fixture."""
    client = completion_client
    built = _seed_completable(client, 8011)
    database, season_id = built["database"], built["season"].season_id
    last_place_entry = built["entries"][9].season_entry_id
    second_last_entry = built["entries"][8].season_entry_id
    pair = {last_place_entry, second_last_entry}

    matches = database.execute(
        "SELECT m.matchup_id, m.home_season_entry_id, m.away_season_entry_id FROM bbbffl_matchup m "
        "JOIN bbbffl_round_lifecycle l ON l.bbbffl_round_id = m.bbbffl_round_id "
        "WHERE l.competition_id=? AND l.fixture_round_number<=20 "
        "AND (m.home_season_entry_id IN (?,?) OR m.away_season_entry_id IN (?,?))",
        (built["ordinary_competition_id"], last_place_entry, second_last_entry, last_place_entry, second_last_entry),
    ).fetchall()
    assert matches
    for match in matches:
        home, away = match["home_season_entry_id"], match["away_season_entry_id"]
        if home in pair and away in pair:
            correct_official_result(database, match["matchup_id"], 40, 40, reason="forced tie: head-to-head")
        elif home in pair:
            correct_official_result(database, match["matchup_id"], 40, 45, reason="forced tie: external")
        else:
            correct_official_result(database, match["matchup_id"], 45, 40, reason="forced tie: external")

    before_events = _audit_count(client, season_id)

    response = client.post(
        f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "attempt despite tie"}
    )

    assert response.status_code == 409, response.text
    assert _lifecycle_state(client, season_id) == "active"
    assert _audit_count(client, season_id) == before_events
    assert (
        client.app.state.database.execute("SELECT 1 FROM season_award WHERE season_id=?", (season_id,)).fetchone()
        is None
    )


# -- Idempotency: repeated completion, missing reason ------------------------


def test_repeated_completion_returns_409_and_changes_nothing_further(completion_client):
    client = completion_client
    season_id = _seed_completable(client, 8012)["season"].season_id
    first = client.post(f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "first completion"})
    assert first.status_code == 200, first.text
    events_after_first = _audit_count(client, season_id)

    second = client.post(f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "second attempt"})

    assert second.status_code == 409, second.text
    assert _lifecycle_state(client, season_id) == "completed"
    assert _audit_count(client, season_id) == events_after_first


def test_missing_reason_is_a_409_and_changes_nothing(completion_client):
    client = completion_client
    season_id = _seed_completable(client, 8013)["season"].season_id
    before_events = _audit_count(client, season_id)

    response = client.post(f"/api/scorer/season-completion/{season_id}/complete", json={})

    assert response.status_code == 409, response.text
    assert _lifecycle_state(client, season_id) == "active"
    assert _audit_count(client, season_id) == before_events


# -- Archival verification: read-only, production-safe -----------------------


def test_archival_verification_before_completion_is_409_and_reads_nothing_mutated(completion_client):
    client = completion_client
    season_id = _seed_completable(client, 8014)["season"].season_id

    response = client.get(f"/api/scorer/season-completion/{season_id}/archival-verification")

    assert response.status_code == 409, response.text
    assert "not completed" in response.json()["detail"]
    assert _lifecycle_state(client, season_id) == "active"


def test_archival_verification_after_completion_reports_the_completion_identity(completion_client):
    client = completion_client
    season_id = _seed_completable(client, 8015)["season"].season_id
    complete = client.post(f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "complete"})
    completion_event_id = complete.json()["completion_event_id"]
    before_events = _audit_count(client, season_id)

    for _ in range(3):
        response = client.get(f"/api/scorer/season-completion/{season_id}/archival-verification")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["season_id"] == season_id
        assert body["completion_event_id"] == completion_event_id
        assert body["completed_season_version"] == complete.json()["completed_season_version"]

    # Read-only: calling it repeatedly changes neither lifecycle state nor
    # the audit trail.
    assert _lifecycle_state(client, season_id) == "completed"
    assert _audit_count(client, season_id) == before_events


def test_archival_verification_with_a_mismatched_expected_event_id_is_409(completion_client):
    client = completion_client
    season_id = _seed_completable(client, 8016)["season"].season_id
    client.post(f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "complete"})

    response = client.get(
        f"/api/scorer/season-completion/{season_id}/archival-verification",
        params={"expected_completion_event_id": "not-the-real-event-id"},
    )

    assert response.status_code == 409, response.text
    assert "not-the-real-event-id" in response.json()["detail"]


def test_archival_verification_with_the_matching_expected_event_id_succeeds(completion_client):
    client = completion_client
    season_id = _seed_completable(client, 8017)["season"].season_id
    completion_event_id = client.post(
        f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "complete"}
    ).json()["completion_event_id"]

    response = client.get(
        f"/api/scorer/season-completion/{season_id}/archival-verification",
        params={"expected_completion_event_id": completion_event_id},
    )

    assert response.status_code == 200, response.text
    assert response.json()["completion_event_id"] == completion_event_id


# -- Authorization: Scorer/Administrator only --------------------------------


def test_secretary_is_denied_completion_and_changes_nothing(completion_client):
    client = completion_client
    season_id = _seed_completable(client, 8018)["season"].season_id
    _role_session(client, Role.SECRETARY, email="secretary-8018@example.com", season_id=season_id)
    before_events = _audit_count(client, season_id)

    assert client.get(f"/api/scorer/season-completion/{season_id}").status_code == 403
    response = client.post(f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "secretary tries"})

    assert response.status_code == 403, response.text
    assert client.get(f"/api/scorer/season-completion/{season_id}/archival-verification").status_code == 403
    assert _lifecycle_state(client, season_id) == "active"
    assert _audit_count(client, season_id) == before_events


def test_replay_operator_is_denied_completion(completion_client):
    client = completion_client
    season_id = _seed_completable(client, 8019)["season"].season_id
    _role_session(client, Role.REPLAY_OPERATOR, email="replay-8019@example.com", season_id=season_id)

    assert client.get(f"/api/scorer/season-completion/{season_id}").status_code == 403
    response = client.post(f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "replay operator"})
    assert response.status_code == 403, response.text
    assert _lifecycle_state(client, season_id) == "active"


def test_plain_coach_without_a_grant_is_denied_and_changes_nothing(completion_client):
    client = completion_client
    season_id = _seed_completable(client, 8020)["season"].season_id
    _plain_coach_session(client, email="coach-8020@example.com")
    before_events = _audit_count(client, season_id)

    assert client.get(f"/api/scorer/season-completion/{season_id}").status_code == 403
    response = client.post(f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "coach tries"})

    assert response.status_code == 403, response.text
    assert _lifecycle_state(client, season_id) == "active"
    assert _audit_count(client, season_id) == before_events


def test_unauthenticated_spectator_is_denied(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.setenv("BBBFFL_ADMIN_TOKEN", "a-real-configured-token")
    monkeypatch.setenv("BBBFFL_SESSION_SECRET", "a-real-configured-session-secret")
    from app.main import app

    try:
        with TestClient(app) as client:
            season_id = _seed_completable(client, 8021)["season"].season_id
            response = client.get(f"/api/scorer/season-completion/{season_id}")
            assert response.status_code == 401
            mutation = client.post(f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "spectator"})
            assert mutation.status_code == 401
            archival = client.get(f"/api/scorer/season-completion/{season_id}/archival-verification")
            assert archival.status_code == 401
            assert _lifecycle_state(client, season_id) == "active"
    finally:
        db_path.unlink(missing_ok=True)


def test_scorer_grant_scoped_to_a_different_season_is_denied(completion_client):
    client = completion_client
    other_season_id = _seed_completable(client, 8022)["season"].season_id
    season_id = _seed_completable(client, 8023)["season"].season_id
    _role_session(client, Role.SCORER, email="scoped-scorer-8023@example.com", season_id=other_season_id)

    assert client.get(f"/api/scorer/season-completion/{season_id}").status_code == 403
    response = client.post(f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "wrong season"})
    assert response.status_code == 403, response.text
    assert _lifecycle_state(client, season_id) == "active"

    # The same grant does cover the season it was actually issued for.
    assert client.get(f"/api/scorer/season-completion/{other_season_id}").status_code == 200


def test_session_write_without_csrf_token_is_refused(completion_client):
    client = completion_client
    season_id = _seed_completable(client, 8024)["season"].season_id
    _role_session(client, Role.SCORER, email="csrf-scorer-8024@example.com", season_id=season_id)

    response = client.post(f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "no csrf"})

    assert response.status_code == 403, response.text
    assert _lifecycle_state(client, season_id) == "active"


def test_archival_verification_read_needs_no_csrf(completion_client):
    """The archival-verification GET never mutates, so it carries no CSRF
    requirement -- proven by a Scorer session (which does need CSRF for the
    POST above) succeeding on this GET with no `X-CSRF-Token` header."""
    client = completion_client
    season_id = _seed_completable(client, 8025)["season"].season_id
    _role_session(client, Role.SCORER, email="scorer-8025@example.com", season_id=season_id)
    headers = _completion_csrf(client, season_id)
    client.post(f"/api/scorer/season-completion/{season_id}/complete", json={"reason": "complete"}, headers=headers)

    response = client.get(f"/api/scorer/season-completion/{season_id}/archival-verification")

    assert response.status_code == 200, response.text
