"""Service-level coverage for the shared multi-role/acting-context model
(roadmap package #107, issue #107): role grants, and
`app.auth.ActingContextService`'s resolution/switching logic.
"""

import pytest

from app.audit import ActorContext, AuditEventRepository
from app.auth import (
    GRANTABLE_ROLES,
    ActingContextService,
    CredentialRepository,
    InvalidRoleError,
    RoleGrantRepository,
    SessionRepository,
    UnauthorizedContextSwitchError,
)
from app.identity import IdentityRepository
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection

ADMIN = ActorContext.anonymous_operator("admin")


@pytest.fixture
def conn():
    return migrated_connection()


@pytest.fixture
def identities(conn):
    return IdentityRepository(conn)


@pytest.fixture
def seasons(conn):
    return SeasonRepository(conn)


@pytest.fixture
def role_grants(conn):
    return RoleGrantRepository(conn)


@pytest.fixture
def sessions(conn):
    return SessionRepository(conn, session_lifetime_seconds=3600)


@pytest.fixture
def credentials(conn):
    return CredentialRepository(conn)


@pytest.fixture
def acting_context(identities, role_grants, sessions):
    return ActingContextService(identities, role_grants, sessions)


@pytest.fixture
def audit(conn):
    return AuditEventRepository(conn)


@pytest.fixture
def operator_coach(identities):
    """A coach identity with no season entry of its own -- a league officer/
    replay operator, never a playing coach."""
    return identities.create_coach("Operator", email="operator@example.com")


@pytest.fixture
def team_coach(identities, seasons):
    season = seasons.create_season(2027, "2027 Season")
    coach = identities.create_coach("Team Coach", email="team-coach@example.com")
    identities.create_entry(season.season_id, "TEAM-1", coach.coach_id, "The Coaches", actor=ADMIN)
    return coach, season


@pytest.fixture
def two_seasons_with_entries(identities, seasons):
    replay_season = seasons.create_season(2026, "2026 Replay")
    live_season = seasons.create_season(2027, "2027 Season")
    some_coach = identities.create_coach("Some Coach")
    replay_entry = identities.create_entry(
        replay_season.season_id, "REPLAY-1", some_coach.coach_id, "Replay Team", actor=ADMIN
    )
    live_entry = identities.create_entry(live_season.season_id, "LIVE-1", some_coach.coach_id, "Live Team", actor=ADMIN)
    return replay_season, live_season, replay_entry, live_entry


# -- RoleGrantRepository ------------------------------------------------


def test_grant_rejects_an_ungrantable_role(role_grants, operator_coach):
    with pytest.raises(InvalidRoleError):
        role_grants.grant(operator_coach.coach_id, "coach", actor=ADMIN)
    with pytest.raises(InvalidRoleError):
        role_grants.grant(operator_coach.coach_id, "spectator", actor=ADMIN)
    with pytest.raises(InvalidRoleError):
        role_grants.grant(operator_coach.coach_id, "not-a-role", actor=ADMIN)


def test_grant_rejects_a_season_scoped_administrator_grant(role_grants, operator_coach, two_seasons_with_entries):
    """Administrator authority is blanket by design: every existing
    `require_admin`/`require_admin_principal` check across the app (and any
    router this PR does not retrofit for season-awareness) treats
    `Role.ADMIN` as unscoped, so a season-scoped admin grant would silently
    stop meaning what it says the moment it reached one of those routes
    (found in code review). Refusing to create one at the source is safer
    than trying to make every admin-gated route season-aware."""
    replay_season, _, _, _ = two_seasons_with_entries
    with pytest.raises(InvalidRoleError):
        role_grants.grant(operator_coach.coach_id, "admin", actor=ADMIN, season_id=replay_season.season_id)
    # An unscoped admin grant remains perfectly valid.
    grant = role_grants.grant(operator_coach.coach_id, "admin", actor=ADMIN)
    assert grant.season_id is None


@pytest.mark.parametrize("role", sorted(GRANTABLE_ROLES))
def test_grant_and_list_active(role_grants, operator_coach, role):
    grant = role_grants.grant(operator_coach.coach_id, role, actor=ADMIN, reason="setup")
    assert grant.role == role
    assert grant.revoked_at is None
    active = role_grants.list_active_for_coach(operator_coach.coach_id)
    assert [g.grant_id for g in active] == [grant.grant_id]


def test_revoke_is_idempotent_and_removes_from_active_list(role_grants, operator_coach):
    grant = role_grants.grant(operator_coach.coach_id, "scorer", actor=ADMIN)
    assert role_grants.revoke(grant.grant_id, actor=ADMIN) is True
    assert role_grants.list_active_for_coach(operator_coach.coach_id) == []
    assert role_grants.revoke(grant.grant_id, actor=ADMIN) is False
    assert role_grants.revoke("unknown-grant-id", actor=ADMIN) is False
    # Revoked grants remain visible in the full history.
    assert len(role_grants.list_all_for_coach(operator_coach.coach_id)) == 1


def test_role_covers_season_semantics(role_grants, operator_coach, two_seasons_with_entries):
    replay_season, live_season, _, _ = two_seasons_with_entries
    global_grant = role_grants.grant(operator_coach.coach_id, "scorer", actor=ADMIN)
    scoped_grant = role_grants.grant(
        operator_coach.coach_id, "replay_operator", actor=ADMIN, season_id=replay_season.season_id
    )

    assert role_grants.is_role_granted(operator_coach.coach_id, "scorer")
    assert role_grants.is_role_granted(operator_coach.coach_id, "replay_operator")
    assert role_grants.role_covers_season(operator_coach.coach_id, "scorer", replay_season.season_id)
    assert role_grants.role_covers_season(operator_coach.coach_id, "scorer", live_season.season_id)
    assert role_grants.role_covers_season(operator_coach.coach_id, "replay_operator", replay_season.season_id)
    assert not role_grants.role_covers_season(operator_coach.coach_id, "replay_operator", live_season.season_id)

    role_grants.revoke(global_grant.grant_id, actor=ADMIN)
    role_grants.revoke(scoped_grant.grant_id, actor=ADMIN)
    assert not role_grants.is_role_granted(operator_coach.coach_id, "scorer")
    assert not role_grants.role_covers_season(operator_coach.coach_id, "replay_operator", replay_season.season_id)


def test_grant_audits_provenance(role_grants, operator_coach, audit):
    grant = role_grants.grant(operator_coach.coach_id, "secretary", actor=ADMIN, reason="onboarding")
    events = audit.list_events(entity_type="identity.role_grant", entity_id=grant.grant_id)
    assert len(events) == 1
    assert events[0].action == "identity.role_grant.created"
    assert events[0].after_state["coach_id"] == operator_coach.coach_id
    assert events[0].after_state["role"] == "secretary"
    assert events[0].reason == "onboarding"

    role_grants.revoke(grant.grant_id, actor=ADMIN, reason="offboarding")
    events = audit.list_events(entity_type="identity.role_grant", entity_id=grant.grant_id)
    assert [e.action for e in events] == ["identity.role_grant.created", "identity.role_grant.revoked"]


# -- ActingContextService.available_roles --------------------------------


def test_available_roles_for_a_plain_team_coach_is_just_coach(acting_context, team_coach):
    coach, _ = team_coach
    assert acting_context.available_roles(coach.coach_id) == frozenset({"coach"})


def test_available_roles_for_an_operator_with_no_team_excludes_coach(acting_context, operator_coach, role_grants):
    role_grants.grant(operator_coach.coach_id, "secretary", actor=ADMIN)
    assert acting_context.available_roles(operator_coach.coach_id) == frozenset({"secretary"})


def test_available_roles_for_a_multi_role_replay_operator(acting_context, role_grants, team_coach):
    coach, _ = team_coach
    role_grants.grant(coach.coach_id, "scorer", actor=ADMIN)
    role_grants.grant(coach.coach_id, "secretary", actor=ADMIN)
    role_grants.grant(coach.coach_id, "replay_operator", actor=ADMIN)
    assert acting_context.available_roles(coach.coach_id) == frozenset(
        {"coach", "scorer", "secretary", "replay_operator"}
    )


def test_available_roles_excludes_revoked_grants(acting_context, role_grants, operator_coach):
    grant = role_grants.grant(operator_coach.coach_id, "admin", actor=ADMIN)
    assert "admin" in acting_context.available_roles(operator_coach.coach_id)
    role_grants.revoke(grant.grant_id, actor=ADMIN)
    assert acting_context.available_roles(operator_coach.coach_id) == frozenset()


# -- ActingContextService.can_represent / representable_entries ----------


def test_coach_role_never_represents_any_entry(acting_context, team_coach):
    coach, season = team_coach
    entries = identities_entries(acting_context, season)
    assert not acting_context.can_represent(coach.coach_id, "coach", entries[0])


def identities_entries(acting_context, season):
    return [e.season_entry_id for e in acting_context.identities.list_entries(season.season_id)]


def test_can_represent_is_false_for_an_unknown_entry(acting_context, operator_coach, role_grants):
    role_grants.grant(operator_coach.coach_id, "scorer", actor=ADMIN)
    assert not acting_context.can_represent(operator_coach.coach_id, "scorer", "does-not-exist")


def test_a_global_grant_can_represent_entries_in_any_season(
    acting_context, role_grants, operator_coach, two_seasons_with_entries
):
    _, _, replay_entry, live_entry = two_seasons_with_entries
    role_grants.grant(operator_coach.coach_id, "scorer", actor=ADMIN)
    assert acting_context.can_represent(operator_coach.coach_id, "scorer", replay_entry.season_entry_id)
    assert acting_context.can_represent(operator_coach.coach_id, "scorer", live_entry.season_entry_id)


def test_a_season_scoped_replay_operator_grant_cannot_represent_a_live_season_entry(
    acting_context, role_grants, operator_coach, two_seasons_with_entries
):
    replay_season, _, replay_entry, live_entry = two_seasons_with_entries
    role_grants.grant(operator_coach.coach_id, "replay_operator", actor=ADMIN, season_id=replay_season.season_id)
    assert acting_context.can_represent(operator_coach.coach_id, "replay_operator", replay_entry.season_entry_id)
    assert not acting_context.can_represent(operator_coach.coach_id, "replay_operator", live_entry.season_entry_id)


def test_representable_entries_is_empty_rather_than_an_error_when_the_role_does_not_cover_the_season(
    acting_context, role_grants, operator_coach, two_seasons_with_entries
):
    replay_season, live_season, _, _ = two_seasons_with_entries
    role_grants.grant(operator_coach.coach_id, "replay_operator", actor=ADMIN, season_id=replay_season.season_id)
    assert acting_context.representable_entries(operator_coach.coach_id, "replay_operator", live_season.season_id) == []
    live_only = acting_context.representable_entries(
        operator_coach.coach_id, "replay_operator", replay_season.season_id
    )
    assert len(live_only) == 1


def test_representable_entries_for_coach_role_is_always_empty(acting_context, team_coach):
    coach, season = team_coach
    assert acting_context.representable_entries(coach.coach_id, "coach", season.season_id) == []


# -- resolve_active_role / resolve_represented_entry (self-healing) -------


def test_resolve_active_role_defaults_to_coach_when_never_switched(acting_context, team_coach):
    coach, _ = team_coach
    assert acting_context.resolve_active_role(coach.coach_id, None) == "coach"


def test_resolve_active_role_keeps_a_still_granted_role(acting_context, role_grants, operator_coach):
    role_grants.grant(operator_coach.coach_id, "secretary", actor=ADMIN)
    assert acting_context.resolve_active_role(operator_coach.coach_id, "secretary") == "secretary"


def test_resolve_active_role_falls_back_to_coach_once_the_grant_is_revoked(acting_context, role_grants, team_coach):
    coach, _ = team_coach
    grant = role_grants.grant(coach.coach_id, "scorer", actor=ADMIN)
    assert acting_context.resolve_active_role(coach.coach_id, "scorer") == "scorer"
    role_grants.revoke(grant.grant_id, actor=ADMIN)
    assert acting_context.resolve_active_role(coach.coach_id, "scorer") == "coach"


def test_resolve_represented_entry_is_none_for_coach_role(acting_context, team_coach):
    coach, season = team_coach
    entry_id = identities_entries(acting_context, season)[0]
    assert acting_context.resolve_represented_entry(coach.coach_id, "coach", entry_id) is None


def test_resolve_represented_entry_falls_back_to_none_once_unauthorised(
    acting_context, role_grants, operator_coach, two_seasons_with_entries
):
    replay_season, live_season, replay_entry, _ = two_seasons_with_entries
    role_grants.grant(operator_coach.coach_id, "replay_operator", actor=ADMIN, season_id=replay_season.season_id)
    assert (
        acting_context.resolve_represented_entry(
            operator_coach.coach_id, "replay_operator", replay_entry.season_entry_id
        )
        == replay_entry.season_entry_id
    )
    # A stale entry from a season this grant no longer (or never did) cover
    # resolves to None rather than continuing to grant access.
    assert (
        acting_context.resolve_represented_entry(operator_coach.coach_id, "replay_operator", "some-other-entry") is None
    )


# -- activate_role / set_represented_entry (validated writes) -------------


def test_activate_role_rejects_an_ungranted_role(acting_context, operator_coach):
    with pytest.raises(UnauthorizedContextSwitchError):
        acting_context.activate_role(
            coach_id=operator_coach.coach_id, session_id="session-1", role="admin", actor=ADMIN
        )


def test_activate_role_rejects_an_unknown_role_name(acting_context, operator_coach):
    with pytest.raises(UnauthorizedContextSwitchError):
        acting_context.activate_role(
            coach_id=operator_coach.coach_id, session_id="session-1", role="superuser", actor=ADMIN
        )


def test_activate_role_succeeds_and_is_reflected_by_get_valid(acting_context, role_grants, sessions, team_coach):
    coach, _ = team_coach
    role_grants.grant(coach.coach_id, "scorer", actor=ADMIN)
    issued = sessions.create(coach.coach_id, actor=ActorContext.coach(coach.coach_id))
    acting_context.activate_role(
        coach_id=coach.coach_id,
        session_id=issued.session.session_id,
        role="scorer",
        actor=ActorContext.coach(coach.coach_id),
    )
    refreshed = sessions.get_valid(issued.token)
    assert refreshed.active_role == "scorer"


def test_activate_role_clears_a_previously_represented_entry(
    acting_context, role_grants, sessions, operator_coach, two_seasons_with_entries
):
    replay_season, _, replay_entry, _ = two_seasons_with_entries
    role_grants.grant(operator_coach.coach_id, "replay_operator", actor=ADMIN, season_id=replay_season.season_id)
    role_grants.grant(operator_coach.coach_id, "scorer", actor=ADMIN)
    issued = sessions.create(operator_coach.coach_id, actor=ActorContext.coach(operator_coach.coach_id))
    actor = ActorContext.coach(operator_coach.coach_id)
    acting_context.activate_role(
        coach_id=operator_coach.coach_id, session_id=issued.session.session_id, role="replay_operator", actor=actor
    )
    acting_context.set_represented_entry(
        coach_id=operator_coach.coach_id,
        session_id=issued.session.session_id,
        active_role="replay_operator",
        season_entry_id=replay_entry.season_entry_id,
        actor=actor,
    )
    assert sessions.get_valid(issued.token).represented_season_entry_id == replay_entry.season_entry_id

    # Switching role, even to another granted role, drops the previously
    # represented entry -- it must never silently carry into a different
    # role's context.
    acting_context.activate_role(
        coach_id=operator_coach.coach_id, session_id=issued.session.session_id, role="scorer", actor=actor
    )
    assert sessions.get_valid(issued.token).represented_season_entry_id is None


def test_set_represented_entry_rejects_coach_role(acting_context, sessions, team_coach):
    coach, season = team_coach
    entry_id = identities_entries(acting_context, season)[0]
    issued = sessions.create(coach.coach_id, actor=ActorContext.coach(coach.coach_id))
    with pytest.raises(UnauthorizedContextSwitchError):
        acting_context.set_represented_entry(
            coach_id=coach.coach_id,
            session_id=issued.session.session_id,
            active_role="coach",
            season_entry_id=entry_id,
            actor=ActorContext.coach(coach.coach_id),
        )


def test_set_represented_entry_rejects_an_entry_outside_the_grants_scope(
    acting_context, role_grants, sessions, operator_coach, two_seasons_with_entries
):
    replay_season, _, _, live_entry = two_seasons_with_entries
    role_grants.grant(operator_coach.coach_id, "replay_operator", actor=ADMIN, season_id=replay_season.season_id)
    issued = sessions.create(operator_coach.coach_id, actor=ActorContext.coach(operator_coach.coach_id))
    with pytest.raises(UnauthorizedContextSwitchError):
        acting_context.set_represented_entry(
            coach_id=operator_coach.coach_id,
            session_id=issued.session.session_id,
            active_role="replay_operator",
            season_entry_id=live_entry.season_entry_id,
            actor=ActorContext.coach(operator_coach.coach_id),
        )


def test_set_represented_entry_can_be_cleared(
    acting_context, role_grants, sessions, operator_coach, two_seasons_with_entries
):
    replay_season, _, replay_entry, _ = two_seasons_with_entries
    role_grants.grant(operator_coach.coach_id, "replay_operator", actor=ADMIN, season_id=replay_season.season_id)
    issued = sessions.create(operator_coach.coach_id, actor=ActorContext.coach(operator_coach.coach_id))
    actor = ActorContext.coach(operator_coach.coach_id)
    acting_context.set_represented_entry(
        coach_id=operator_coach.coach_id,
        session_id=issued.session.session_id,
        active_role="replay_operator",
        season_entry_id=replay_entry.season_entry_id,
        actor=actor,
    )
    acting_context.set_represented_entry(
        coach_id=operator_coach.coach_id,
        session_id=issued.session.session_id,
        active_role="replay_operator",
        season_entry_id=None,
        actor=actor,
    )
    assert sessions.get_valid(issued.token).represented_season_entry_id is None


def test_context_switch_events_are_audited_as_the_authenticated_actor(
    acting_context, role_grants, sessions, team_coach, audit
):
    coach, _ = team_coach
    role_grants.grant(coach.coach_id, "scorer", actor=ADMIN)
    issued = sessions.create(coach.coach_id, actor=ActorContext.coach(coach.coach_id))
    acting_context.activate_role(
        coach_id=coach.coach_id,
        session_id=issued.session.session_id,
        role="scorer",
        actor=ActorContext.coach(coach.coach_id),
    )
    events = audit.list_events(entity_type="auth.session", entity_id=issued.session.session_id)
    activated = [e for e in events if e.action == "auth.context.role_activated"]
    assert len(activated) == 1
    assert activated[0].actor_type == "coach"
    assert activated[0].actor_id == coach.coach_id
    assert activated[0].after_state == {"active_role": "scorer"}
