from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.authorization import (
    ANONYMOUS,
    Principal,
    Role,
    require_admin_principal,
    require_coach,
    require_owned_season_entry,
    require_scorer_or_admin,
    resolve_principal,
)


@pytest.mark.parametrize(
    "principal,coach,scorer,admin",
    [
        (ANONYMOUS, 401, 401, 401),
        (Principal(Role.COACH, "coach-a"), None, 403, 403),
        (Principal(Role.SCORER), 403, None, 403),
        (Principal(Role.ADMIN), 403, None, None),
    ],
)
def test_permission_matrix(principal, coach, scorer, admin):
    checks = ((require_coach, coach), (require_scorer_or_admin, scorer), (require_admin_principal, admin))
    for check, expected in checks:
        if expected is None:
            assert check(principal) is principal
        else:
            with pytest.raises(HTTPException) as exc:
                check(principal)
            assert exc.value.status_code == expected


class _Identities:
    def coach_owns_entry(self, coach_id, entry_id):
        return coach_id == "coach-a" and entry_id == "entry-a"


@pytest.mark.parametrize("entry_id", ["entry-b", "does-not-exist"])
def test_foreign_and_nonexistent_private_entries_are_enumeration_safe(entry_id):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(identities=_Identities())))
    with pytest.raises(HTTPException) as exc:
        require_owned_season_entry(request, Principal(Role.COACH, "coach-a"), entry_id)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Private resource not found"


def test_owner_can_access_own_season_entry():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(identities=_Identities())))
    assert require_owned_season_entry(request, Principal(Role.COACH, "coach-a"), "entry-a") is None


class _Auth:
    def __init__(self, coach=None):
        self.coach = coach

    def resolve(self, token):
        return self.coach if token == "valid-session" else None


def _request(*, admin_token, cookies=None, coach=None):
    state = SimpleNamespace(settings=SimpleNamespace(admin_token=admin_token), auth_service=_Auth(coach))
    return SimpleNamespace(app=SimpleNamespace(state=state), cookies=cookies or {})


def test_tokenless_development_mode_is_explicit_admin_authority():
    principal = resolve_principal(_request(admin_token=None), None, None)
    assert principal == Principal(Role.ADMIN)


def test_configured_operator_token_is_required_and_can_be_narrowed_to_scorer():
    request = _request(admin_token="secret")
    assert resolve_principal(request, None, None) is ANONYMOUS
    assert resolve_principal(request, "secret", "scorer") == Principal(Role.SCORER)
    with pytest.raises(HTTPException) as exc:
        resolve_principal(request, "wrong", None)
    assert exc.value.status_code == 401


def test_coach_session_never_acquires_tokenless_operator_authority():
    coach = SimpleNamespace(coach_id="coach-a", display_name="Coach A")
    principal = resolve_principal(
        _request(admin_token=None, cookies={"bbbffl_session": "valid-session"}, coach=coach), None, None
    )
    assert principal == Principal(Role.COACH, "coach-a", "Coach A")
