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
