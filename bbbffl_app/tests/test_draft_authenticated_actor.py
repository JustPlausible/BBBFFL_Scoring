"""Regression coverage for issue #128: authenticated, represented-entry
Draft Board selections (roadmap package #107's active-role/represented-entry
context reaching `app.routes.draft`) failed *after* authentication, active-
role and represented-entry authorization all succeeded, because
`_pick_actor` emitted the invalid audit actor type `"coach_identity"` --
`app.audit.append_event`'s `KNOWN_ACTOR_TYPES` allowlist correctly rejected
it inside `DraftRepository.execute_pick`'s transaction, so the whole pick
(including the player-ownership acquisition already written in that same
transaction) rolled back.

These tests exercise the real HTTP endpoint end-to-end -- login, role
activation, represented-entry selection, then `POST /api/admin/draft/
{season_id}/pick` -- exactly as `docs/acting-context.md`'s browser workflow
does, rather than calling `_pick_actor` in isolation, so a regression here
would only be caught once the complete authorization path is exercised
(matching how the original bug was only visible in production, not in any
unit-level check of the actor construction alone).
"""

import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.audit import AuditEventRepository
from app.identity import IdentityRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.season import SeasonRepository

PASSWORD = "correct horse battery staple"
ENTRIES = 3
SQUAD_LIMIT = 1


@pytest.fixture
def client(monkeypatch):
    db_path = Path(tempfile.mkstemp(suffix=".db")[1])
    monkeypatch.setenv("BBBFFL_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("BBBFFL_ENVIRONMENT", "test")
    monkeypatch.delenv("BBBFFL_ADMIN_TOKEN", raising=False)

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    db_path.unlink(missing_ok=True)


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf_token hidden field not found in rendered page"
    return match.group(1)


def _register_coach(app, *, email, name, password=PASSWORD):
    coach = app.state.identities.create_coach(name, email=email)
    from app.audit import ActorContext

    app.state.credentials.set_password(coach.coach_id, password, actor=ActorContext.anonymous_operator("admin"))
    return coach


def _login(client, *, email, password=PASSWORD):
    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.text)
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": csrf_token},
        cookies=login_page.cookies,
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    session_cookie = response.cookies.get("bbbffl_session")
    assert session_cookie
    return session_cookie


def _csrf_for_session(client, session_cookie):
    account = client.get("/account", cookies={"bbbffl_session": session_cookie})
    assert account.status_code == 200
    return _extract_csrf(account.text), account.cookies.get("bbbffl_csrf")


def _grant_role(client, *, coach_id, role, season_id=None):
    response = client.post("/api/admin/role-grants", json={"coach_id": coach_id, "role": role, "season_id": season_id})
    assert response.status_code == 200, response.text
    return response.json()


def _seed_draft_ready_season(database, *, year, label):
    """A minimal accepted/frozen draft with an active current pick,
    mirroring `tests/test_scorer_draft_workflow.py`'s `_seed_season` but
    trimmed to the smallest fixture that still has a real next pick."""
    season = SeasonRepository(database).create_season(year, label)
    identities = IdentityRepository(database)
    entries = [
        identities.create_entry(
            season.season_id,
            f"entry-{number}",
            identities.create_coach(f"Team Coach {number}", email=f"team-coach-{number}@example.com").coach_id,
            f"Team {number}",
        )
        for number in range(ENTRIES)
    ]
    OwnershipRepository(database).configure_squad_limit(season.season_id, SQUAD_LIMIT)
    pool = PlayerPoolRepository(database)
    players = [
        pool.refresh_player(season.season_id, 3000 + number, f"Selectable Player {number}") for number in range(6)
    ]

    from app.draft import DraftRepository

    DraftRepository(database).accept_order(season.season_id, [entry.season_entry_id for entry in entries])
    return season, entries, players


def _authenticate_delegated_operator(client, *, season_id, represented_entry_id, role="replay_operator"):
    """Registers an operator coach identity, grants it `role` scoped to
    `season_id`, logs in, activates the role, and represents
    `represented_entry_id` -- the complete #107 acting-context path a real
    browser session drives before ever reaching the Draft Board."""
    from app.main import app

    operator = _register_coach(app, email="delegated-operator@example.com", name="Delegated Operator")
    _grant_role(client, coach_id=operator.coach_id, role=role, season_id=season_id)
    session_cookie = _login(client, email="delegated-operator@example.com")
    csrf_token, csrf_cookie = _csrf_for_session(client, session_cookie)
    cookies = {"bbbffl_session": session_cookie, "bbbffl_csrf": csrf_cookie}
    headers = {"X-CSRF-Token": csrf_token}

    activated = client.post("/api/context/role", json={"role": role}, cookies=cookies, headers=headers)
    assert activated.status_code == 200, activated.text

    represented = client.post(
        "/api/context/represented-entry",
        json={"season_entry_id": represented_entry_id},
        cookies=cookies,
        headers=headers,
    )
    assert represented.status_code == 200, represented.text

    return operator, cookies


def test_authenticated_delegated_pick_succeeds_and_audits_anonymous_operator(client):
    database = client.app.state.database
    season, entries, players = _seed_draft_ready_season(database, year=2101, label="Delegated pick success")
    current_pick_owner = entries[0]

    operator, cookies = _authenticate_delegated_operator(
        client, season_id=season.season_id, represented_entry_id=current_pick_owner.season_entry_id
    )

    api = f"/api/admin/draft/{season.season_id}"
    board_before = client.get(f"{api}/board", cookies=cookies).json()
    current_pick = board_before["current_pick"]
    assert current_pick["current_season_entry_id"] == current_pick_owner.season_entry_id
    completed_before = board_before["status"]["completed_picks"]
    chosen_player = players[0]

    response = client.post(
        f"{api}/pick",
        json={
            "season_entry_id": current_pick_owner.season_entry_id,
            "season_player_id": chosen_player.season_player_id,
        },
        cookies=cookies,
    )
    assert response.status_code == 200, response.text

    board_after = response.json()
    assert board_after["status"]["completed_picks"] == completed_before + 1

    completed_pick = board_after["completed_picks"][0]
    assert completed_pick["draft_pick_id"] == current_pick["draft_pick_id"]
    assert completed_pick["selected_season_player_id"] == chosen_player.season_player_id
    assert completed_pick["current_season_entry_id"] == current_pick_owner.season_entry_id

    next_current = board_after["current_pick"]
    assert next_current is not None
    assert next_current["overall_number"] == current_pick["overall_number"] + 1

    # -- Ownership: exactly one acquisition, for the represented (owning)
    # entry -- never the operator's own identity, which has no entry here.
    ownership = OwnershipRepository(database)
    squad = ownership.squad_at(current_pick_owner.season_entry_id, "9999-12-31")
    assert [row.season_player_id for row in squad] == [chosen_player.season_player_id]

    players_view = client.get(f"{api}/players", cookies=cookies).json()
    claimed = next(p for p in players_view if p["season_player_id"] == chosen_player.season_player_id)
    assert claimed["availability"] == "owned"
    assert claimed["owner_season_entry_id"] == current_pick_owner.season_entry_id
    available_ids = {p["season_player_id"] for p in client.get(f"{api}/available-players", cookies=cookies).json()}
    assert chosen_player.season_player_id not in available_ids

    # -- Exactly one draft completion audit event, correctly attributed:
    # `anonymous_operator` (the delegated-write convention), the
    # authenticated operator's own stable coach_id as pure provenance, the
    # active delegated role, and the draft pick (not the operator) as the
    # audited entity.
    events = AuditEventRepository(database).list_events(action="draft.pick.completed")
    matching = [event for event in events if event.entity_id == current_pick["draft_pick_id"]]
    assert len(matching) == 1
    event = matching[0]
    assert event.actor_type == "anonymous_operator"
    assert event.actor_id == operator.coach_id
    assert event.actor_role == "replay_operator"
    assert event.entity_type == "draft.pick"
    assert event.entity_id == current_pick["draft_pick_id"]
    assert event.after_state["season_entry_id"] == current_pick_owner.season_entry_id


def test_authenticated_delegated_pick_for_a_represented_entry_that_does_not_own_the_pick_is_a_404(client):
    database = client.app.state.database
    season, entries, players = _seed_draft_ready_season(database, year=2102, label="Represented entry mismatch")
    current_pick_owner = entries[0]
    other_entry = entries[1]

    operator, cookies = _authenticate_delegated_operator(
        client, season_id=season.season_id, represented_entry_id=other_entry.season_entry_id
    )

    api = f"/api/admin/draft/{season.season_id}"
    board_before = client.get(f"{api}/board", cookies=cookies).json()
    completed_before = board_before["status"]["completed_picks"]
    current_pick = board_before["current_pick"]

    # The represented context (`other_entry`) does not own the current
    # pick; submitting a payload for the entry that *does* own it must
    # still be refused, because the acting session is not representing it
    # -- this must not leak whether `current_pick_owner` exists as a
    # privately-scoped entry via a 403 vs. 404 distinction.
    response = client.post(
        f"{api}/pick",
        json={
            "season_entry_id": current_pick_owner.season_entry_id,
            "season_player_id": players[0].season_player_id,
        },
        cookies=cookies,
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Private resource not found"

    board_after = client.get(f"{api}/board", cookies=cookies).json()
    assert board_after["status"]["completed_picks"] == completed_before
    assert board_after["current_pick"]["draft_pick_id"] == current_pick["draft_pick_id"]

    available_ids = {p["season_player_id"] for p in client.get(f"{api}/available-players", cookies=cookies).json()}
    assert players[0].season_player_id in available_ids

    ownership = OwnershipRepository(database)
    assert ownership.squad_at(current_pick_owner.season_entry_id, "9999-12-31") == []

    events = AuditEventRepository(database).list_events(action="draft.pick.completed")
    assert events == []


def test_stale_repeated_submission_after_a_successful_authenticated_pick_cannot_duplicate_it(client):
    database = client.app.state.database
    season, entries, players = _seed_draft_ready_season(database, year=2103, label="Stale replay")
    current_pick_owner = entries[0]

    operator, cookies = _authenticate_delegated_operator(
        client, season_id=season.season_id, represented_entry_id=current_pick_owner.season_entry_id
    )

    api = f"/api/admin/draft/{season.season_id}"
    current_pick = client.get(f"{api}/board", cookies=cookies).json()["current_pick"]
    chosen_player = players[0]

    payload = {
        "draft_pick_id": current_pick["draft_pick_id"],
        "season_entry_id": current_pick_owner.season_entry_id,
        "season_player_id": chosen_player.season_player_id,
    }
    first = client.post(f"{api}/pick", json=payload, cookies=cookies)
    assert first.status_code == 200, first.text

    # A replay of the exact same (now-stale) request -- the same pick id,
    # the same player, the same represented entry -- must not create a
    # second completed pick or a second ownership row. `execute_pick`
    # raises `DraftPickCompletedError`, which the app maps to 409; the
    # existing conflict/error response, not a new status code.
    second = client.post(f"{api}/pick", json=payload, cookies=cookies)
    assert second.status_code == 409

    completion_events = [
        event
        for event in AuditEventRepository(database).list_events(action="draft.pick.completed")
        if event.entity_id == current_pick["draft_pick_id"]
    ]
    assert len(completion_events) == 1

    ownership = OwnershipRepository(database)
    squad = ownership.squad_at(current_pick_owner.season_entry_id, "9999-12-31")
    assert [row.season_player_id for row in squad] == [chosen_player.season_player_id]

    board_after = client.get(f"{api}/board", cookies=cookies).json()
    assert board_after["status"]["completed_picks"] == 1


def test_legacy_shared_token_pick_still_succeeds_with_anonymous_scorer_provenance(client):
    """No coach session, no represented-entry acting context -- the older
    shared-token compatibility path (`_scorer_actor`) must keep working
    unmodified by this fix."""
    database = client.app.state.database
    season, entries, players = _seed_draft_ready_season(database, year=2104, label="Legacy compatibility")
    current_pick_owner = entries[0]

    api = f"/api/admin/draft/{season.season_id}"
    current_pick = client.get(f"{api}/board").json()["current_pick"]
    assert current_pick["current_season_entry_id"] == current_pick_owner.season_entry_id

    response = client.post(
        f"{api}/pick",
        json={
            "season_entry_id": current_pick_owner.season_entry_id,
            "season_player_id": players[0].season_player_id,
            "scorer_name": "Legacy Scorer",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"]["completed_picks"] == 1

    events = AuditEventRepository(database).list_events(action="draft.pick.completed")
    matching = [event for event in events if event.entity_id == current_pick["draft_pick_id"]]
    assert len(matching) == 1
    assert matching[0].actor_type == "anonymous_operator"
    assert matching[0].actor_id == "Legacy Scorer"
    assert matching[0].actor_role == "scorer"
