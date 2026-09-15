"""Issue #208: `app.routes.delegated_operations._scope` generalised from
`ordinary`-only ("Unknown ordinary BBBFFL round" for anything else) to
`ordinary`/`finals`/`superscore`. This proves it now resolves a finals/
SuperScore round with a human-readable label, applies the exact same
stream-specific participation boundary the coach lineup surface enforces
(a delegate must not gain authority over an invalid finals participant
merely because the route now accepts finals rounds), and that the full
`view_lineup` route still returns a working lineup view. Ordinary-round
behaviour is asserted unchanged."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.authorization import Principal, Role
from app.identity import IdentityRepository
from app.routes import delegated_operations
from tests.test_carry_forward import context
from tests.test_coach_lineup_finals_superscore import _open_finals_week1, _open_superscore1, _StubAflClient


def _request(database, afl_client):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(database=database, afl_client=afl_client, identities=IdentityRepository(database))
        )
    )


def _principal(entry_id):
    return Principal(Role.ADMIN, coach_id="operator-1", display_name="Operator", represented_season_entry_id=entry_id)


def test_scope_resolves_a_valid_finals_participant_with_human_readable_label():
    built = _open_finals_week1()
    database = built["database"]
    participant = built["entries"][1]  # seed 2 -- plays in the Week 1 qualifying final
    request = _request(database, _StubAflClient())

    scope = delegated_operations._scope(request, _principal(participant.season_entry_id), built["week1_round_id"])

    assert scope["round_label"] == "Finals Week 1"
    assert scope["season_entry_id"] == participant.season_entry_id
    assert scope["team_name"]


def test_view_lineup_route_works_for_a_valid_finals_participant():
    built = _open_finals_week1()
    database = built["database"]
    participant = built["entries"][1]
    request = _request(database, _StubAflClient())

    view = delegated_operations.view_lineup(built["week1_round_id"], request, _principal(participant.season_entry_id))

    assert view["round"]["label"] == "Finals Week 1"
    assert view["acting_context"]["team_name"]
    assert set(row["position"] for row in view["lock_state"]) == set(delegated_operations.COACH_LINEUP_POSITIONS)


def test_scope_rejects_a_non_participating_finals_team():
    """An eliminated/non-qualifying team (seed 10, no Week 1 pairing) must
    not gain delegated authority over a finals round merely because the
    route now accepts finals rounds."""
    built = _open_finals_week1()
    database = built["database"]
    eliminated = built["entries"][9]
    request = _request(database, _StubAflClient())

    with pytest.raises(HTTPException) as excinfo:
        delegated_operations._scope(request, _principal(eliminated.season_entry_id), built["week1_round_id"])
    assert excinfo.value.status_code == 404


def test_scope_resolves_a_valid_superscore_entry_with_human_readable_label():
    built = _open_superscore1()
    database = built["database"]
    entry_obj = built["entries"][7]
    request = _request(database, _StubAflClient())

    scope = delegated_operations._scope(request, _principal(entry_obj.season_entry_id), built["ss1_round_id"])

    assert scope["round_label"] == "SuperScore 1"
    assert scope["season_entry_id"] == entry_obj.season_entry_id


def test_scope_accepts_all_ten_superscore_entries():
    built = _open_superscore1()
    database = built["database"]
    request = _request(database, _StubAflClient())
    for entry_obj in built["entries"]:
        scope = delegated_operations._scope(request, _principal(entry_obj.season_entry_id), built["ss1_round_id"])
        assert scope["season_entry_id"] == entry_obj.season_entry_id


def test_scope_ordinary_round_behaviour_is_unchanged():
    db, _, rounds, entries, scope_row, _pool, _ownership = context(rounds=1)
    entry = entries[0]
    raw_label = db.execute("SELECT label FROM bbbffl_round WHERE bbbffl_round_id=?", (rounds[0],)).fetchone()["label"]
    request = _request(db, _StubAflClient())

    scope = delegated_operations._scope(request, _principal(entry.season_entry_id), rounds[0])

    assert scope["round_label"] == raw_label
    assert scope["season_entry_id"] == entry.season_entry_id
