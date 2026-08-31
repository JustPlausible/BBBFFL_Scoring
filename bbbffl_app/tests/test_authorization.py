from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.authorization import (
    ANONYMOUS,
    CAPABILITIES,
    Principal,
    Role,
    require_admin_principal,
    require_capability,
    require_coach,
    require_entry_context,
    require_owned_season_entry,
    require_role_covers_season,
    require_scorer_or_admin,
    require_secretary_or_admin,
    resolve_principal,
)


@pytest.mark.parametrize(
    "principal,coach,scorer,admin,secretary",
    [
        (ANONYMOUS, 401, 401, 401, 401),
        (Principal(Role.COACH, "coach-a"), None, 403, 403, 403),
        (Principal(Role.SCORER), 403, None, 403, 403),
        (Principal(Role.ADMIN), 403, None, None, None),
        (Principal(Role.SECRETARY), 403, 403, 403, None),
        (Principal(Role.REPLAY_OPERATOR), 403, 403, 403, 403),
    ],
)
def test_permission_matrix(principal, coach, scorer, admin, secretary):
    checks = (
        (require_coach, coach),
        (require_scorer_or_admin, scorer),
        (require_admin_principal, admin),
        (require_secretary_or_admin, secretary),
    )
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


# -- Capability-based checks (roadmap package #107, issue #107) -------------


def test_every_role_capability_set_is_closed_and_admin_is_universal():
    for role in Role:
        assert role in CAPABILITIES
    assert "*" in CAPABILITIES[Role.ADMIN]


@pytest.mark.parametrize(
    "principal,capability,allowed",
    [
        (Principal(Role.SECRETARY), "season.manage", True),
        (Principal(Role.SECRETARY), "scoring.manage", False),
        (Principal(Role.SCORER), "scoring.manage", True),
        (Principal(Role.SCORER), "season.manage", False),
        (Principal(Role.ADMIN), "anything.at.all", True),
        (Principal(Role.COACH, "coach-a"), "season.manage", False),
    ],
)
def test_require_capability_matches_the_capability_matrix(principal, capability, allowed):
    check = require_capability(capability)
    if allowed:
        assert check(principal) is principal
    else:
        with pytest.raises(HTTPException) as exc:
            check(principal)
        assert exc.value.status_code == 403


def test_require_capability_rejects_an_unauthenticated_principal():
    with pytest.raises(HTTPException) as exc:
        require_capability("season.manage")(ANONYMOUS)
    assert exc.value.status_code == 401


# -- require_entry_context: coach-owned vs. delegated-represented -----------


def test_entry_context_for_coach_follows_ownership_rules():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(identities=_Identities())))
    assert require_entry_context(request, Principal(Role.COACH, "coach-a"), "entry-a") is None
    with pytest.raises(HTTPException) as exc:
        require_entry_context(request, Principal(Role.COACH, "coach-a"), "entry-b")
    assert exc.value.status_code == 404


def test_entry_context_for_a_delegated_role_requires_the_represented_entry_to_match():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(identities=_Identities())))
    principal = Principal(Role.SCORER, "coach-x", represented_season_entry_id="entry-a")
    assert require_entry_context(request, principal, "entry-a") is None
    with pytest.raises(HTTPException) as exc:
        require_entry_context(request, principal, "entry-b")
    assert exc.value.status_code == 404


def test_entry_context_for_a_delegated_role_with_no_represented_entry_is_denied():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(identities=_Identities())))
    principal = Principal(Role.SECRETARY, "coach-x")
    with pytest.raises(HTTPException) as exc:
        require_entry_context(request, principal, "entry-a")
    assert exc.value.status_code == 404


# -- require_role_covers_season (season-scoped grants) ----------------------


class _RoleGrants:
    def __init__(self, covered_season_id):
        self.covered_season_id = covered_season_id

    def role_covers_season(self, coach_id, role, season_id):
        return season_id == self.covered_season_id


def _season_scoped_request(covered_season_id):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(role_grants=_RoleGrants(covered_season_id))))


def test_role_covers_season_rejects_a_season_the_grant_does_not_cover():
    request = _season_scoped_request("season-a")
    principal = Principal(Role.SECRETARY, "coach-x")
    assert require_role_covers_season(request, principal, "season-a") is None
    with pytest.raises(HTTPException) as exc:
        require_role_covers_season(request, principal, "season-b")
    assert exc.value.status_code == 403


def test_role_covers_season_never_restricts_admin_or_coach_or_the_legacy_token():
    """Regression for a code-review finding: Administrator is always
    unscoped (`RoleGrantRepository.grant` refuses to create a scoped admin
    grant in the first place), Coach mode is entry-specific and governed
    elsewhere, and the legacy shared X-Admin-Token principal has no
    coach_id/role_grant row to scope at all."""
    request = _season_scoped_request("season-a")
    assert require_role_covers_season(request, Principal(Role.ADMIN, "coach-x"), "season-b") is None
    assert require_role_covers_season(request, Principal(Role.COACH, "coach-x"), "season-b") is None
    assert require_role_covers_season(request, Principal(Role.ADMIN), "season-b") is None  # no coach_id at all


# -- resolve_principal ------------------------------------------------------


class _Sessions:
    def __init__(self, session=None):
        self.session = session

    def get_valid(self, token):
        return self.session if token == "valid-session" else None


class _CoachIdentities:
    def __init__(self, coach=None):
        self.coach = coach

    def get_coach(self, coach_id):
        return self.coach if self.coach and self.coach.coach_id == coach_id else None


class _ActingContext:
    """A minimal stand-in for `app.auth.ActingContextService` -- real,
    DB-backed coverage of the actual resolution logic lives in
    `tests/test_acting_context.py`; this fake only proves
    `resolve_principal` wires its three calls together correctly."""

    def __init__(self, available=frozenset({"coach"}), active_role="coach", represented_entry=None):
        self.available = available
        self.active_role = active_role
        self.represented_entry = represented_entry
        self.calls = []

    def available_roles(self, coach_id):
        self.calls.append(("available_roles", coach_id))
        return self.available

    def resolve_active_role(self, coach_id, stored):
        self.calls.append(("resolve_active_role", coach_id, stored))
        return self.active_role

    def resolve_represented_entry(self, coach_id, active_role, stored):
        self.calls.append(("resolve_represented_entry", coach_id, active_role, stored))
        return self.represented_entry


def _request(*, admin_token, cookies=None, session=None, coach=None, acting_context=None):
    state = SimpleNamespace(
        settings=SimpleNamespace(admin_token=admin_token),
        sessions=_Sessions(session),
        identities=_CoachIdentities(coach),
        acting_context=acting_context or _ActingContext(),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state), cookies=cookies or {})


def test_tokenless_development_mode_is_explicit_admin_authority():
    principal = resolve_principal(_request(admin_token=None), None, None)
    assert principal == Principal(Role.ADMIN, granted_roles=frozenset({Role.ADMIN}))


def test_configured_operator_token_is_required_and_can_be_narrowed_to_scorer():
    request = _request(admin_token="secret")
    assert resolve_principal(request, None, None) is ANONYMOUS
    assert resolve_principal(request, "secret", "scorer") == Principal(
        Role.SCORER, granted_roles=frozenset({Role.SCORER})
    )
    with pytest.raises(HTTPException) as exc:
        resolve_principal(request, "wrong", None)
    assert exc.value.status_code == 401


def test_coach_session_never_acquires_tokenless_operator_authority():
    coach = SimpleNamespace(coach_id="coach-a", display_name="Coach A")
    session = SimpleNamespace(
        session_id="session-1", coach_id="coach-a", active_role=None, represented_season_entry_id=None
    )
    principal = resolve_principal(
        _request(admin_token=None, cookies={"bbbffl_session": "valid-session"}, session=session, coach=coach),
        None,
        None,
    )
    assert principal == Principal(
        Role.COACH, "coach-a", "Coach A", granted_roles=frozenset({Role.COACH}), session_id="session-1"
    )


def test_multi_role_coach_session_resolves_active_role_and_represented_entry_via_acting_context():
    coach = SimpleNamespace(coach_id="coach-a", display_name="Coach A")
    session = SimpleNamespace(
        session_id="session-1", coach_id="coach-a", active_role="scorer", represented_season_entry_id="entry-x"
    )
    acting_context = _ActingContext(
        available=frozenset({"coach", "scorer", "secretary"}), active_role="scorer", represented_entry="entry-x"
    )
    principal = resolve_principal(
        _request(
            admin_token=None,
            cookies={"bbbffl_session": "valid-session"},
            session=session,
            coach=coach,
            acting_context=acting_context,
        ),
        None,
        None,
    )
    assert principal.role is Role.SCORER
    assert principal.coach_id == "coach-a"
    assert principal.granted_roles == frozenset({Role.COACH, Role.SCORER, Role.SECRETARY})
    assert principal.represented_season_entry_id == "entry-x"
    assert principal.session_id == "session-1"
    # resolve_principal never trusts the session row's stored fields
    # directly -- every field flows back through ActingContextService.
    assert ("resolve_active_role", "coach-a", "scorer") in acting_context.calls
    assert ("resolve_represented_entry", "coach-a", "scorer", "entry-x") in acting_context.calls


def test_replay_operator_active_role_is_flagged_for_the_ui():
    coach = SimpleNamespace(coach_id="coach-a", display_name="Coach A")
    session = SimpleNamespace(
        session_id="session-1", coach_id="coach-a", active_role="replay_operator", represented_season_entry_id=None
    )
    acting_context = _ActingContext(available=frozenset({"coach", "replay_operator"}), active_role="replay_operator")
    principal = resolve_principal(
        _request(
            admin_token=None,
            cookies={"bbbffl_session": "valid-session"},
            session=session,
            coach=coach,
            acting_context=acting_context,
        ),
        None,
        None,
    )
    assert principal.role is Role.REPLAY_OPERATOR
    assert principal.is_replay_context is True
    assert Principal(Role.SCORER).is_replay_context is False
