"""Single request-level authorization and privacy boundary.

Authentication establishes identity; this module turns request credentials into
one of the authorities understood by HTTP routes. Domain services remain
usable without HTTP and continue to enforce their own invariants.

## Active role vs. authenticated identity (roadmap package #107, issue #107)

`Principal.coach_id`/`display_name` are the authenticated actor -- the real
signed-in person -- and never change while a session is switching roles.
`Principal.role` is the *active authority role* that session is currently
exercising, one of `Principal.granted_roles` (everything that coach identity
is currently authorised to activate: "coach" plus any `app.auth.
GRANTABLE_ROLES` granted through `app.auth.RoleGrantRepository`).
`Principal.represented_season_entry_id` is the optional delegated team/
season-entry context a non-"coach" active role is currently acting for.
These three concepts are computed fresh on every request by
`resolve_principal` (via `app.auth.ActingContextService`) from
server-authoritative state (`coach_session`, `role_grant`) -- never trusted
from a client-supplied role name, team ID, query parameter or form value.
See docs/acting-context.md.
"""

from dataclasses import dataclass
from enum import StrEnum

from fastapi import Depends, Header, HTTPException, Request


class Role(StrEnum):
    SPECTATOR = "spectator"
    COACH = "coach"
    SCORER = "scorer"
    SECRETARY = "secretary"
    ADMIN = "admin"
    REPLAY_OPERATOR = "replay_operator"


@dataclass(frozen=True)
class Principal:
    role: Role
    coach_id: str | None = None
    display_name: str | None = None
    # Roadmap package #107: every role this authenticated identity may
    # currently activate (see `app.auth.ActingContextService.available_roles`).
    # Empty for the legacy X-Admin-Token/spectator paths beyond `role` itself.
    granted_roles: frozenset[Role] = frozenset()
    # Roadmap package #107: the season entry a delegated (non-"coach")
    # active role is currently representing, re-validated on every request
    # -- see `app.auth.ActingContextService.resolve_represented_entry`.
    represented_season_entry_id: str | None = None
    # Roadmap package #107: the coach_session row this principal was
    # resolved from, if any -- `app.routes.context` uses it to target a
    # context switch. Never present for the legacy X-Admin-Token path.
    session_id: str | None = None

    @property
    def authenticated(self) -> bool:
        return self.role is not Role.SPECTATOR

    @property
    def is_replay_context(self) -> bool:
        """Whether the *active* role is Replay Operator -- the signal the
        UI uses to make replay actions visually unmistakable from live-
        season work (issue #107's UI requirement). Not a privilege check by
        itself."""
        return self.role is Role.REPLAY_OPERATOR


ANONYMOUS = Principal(Role.SPECTATOR)
SESSION_COOKIE_NAME = "bbbffl_session"


def resolve_principal(
    request: Request,
    x_admin_token: str | None = Header(default=None),
    x_authority_role: str | None = Header(default=None),
) -> Principal:
    """Resolve operator credentials before coach credentials, without combining them.

    The legacy admin token remains admin by default.  Operators may explicitly
    narrow it to ``scorer``; no other role value is accepted.  A coach cookie can
    never supplement an operator credential or acquire operator capabilities.

    A coach session's *active* role/represented entry (roadmap package #107)
    is recomputed here on every call via `app.auth.ActingContextService` --
    never read as a bare trusted value off the session row -- so a role
    grant revoked mid-session, or a represented entry that stops being
    authorised, stops conferring authority on the very next request.
    """
    configured = request.app.state.settings.admin_token
    if x_admin_token is not None:
        # An unset token is the repository's established development/test
        # "open operator" mode.  Production configuration rejects an unset
        # token before the app starts; do not silently redesign that contract
        # here.  The ambient authority is still represented explicitly.
        if configured and x_admin_token != configured:
            raise HTTPException(status_code=401, detail="Invalid X-Admin-Token")
        if x_authority_role not in (None, Role.ADMIN, Role.SCORER):
            raise HTTPException(status_code=403, detail="Unknown operator authority")
        role = Role(x_authority_role or Role.ADMIN)
        return Principal(role, granted_roles=frozenset({role}))

    state = request.app.state
    token = request.cookies.get(SESSION_COOKIE_NAME)
    session = state.sessions.get_valid(token) if token else None
    coach = state.identities.get_coach(session.coach_id) if session is not None else None
    if coach is not None:
        acting_context = state.acting_context
        granted = acting_context.available_roles(coach.coach_id)
        active_role = acting_context.resolve_active_role(coach.coach_id, session.active_role)
        represented_entry = acting_context.resolve_represented_entry(
            coach.coach_id, active_role, session.represented_season_entry_id
        )
        return Principal(
            Role(active_role),
            coach.coach_id,
            coach.display_name,
            granted_roles=frozenset(Role(r) for r in granted),
            represented_season_entry_id=represented_entry,
            session_id=session.session_id,
        )
    if configured is None:
        return Principal(Role.ADMIN, granted_roles=frozenset({Role.ADMIN}))
    return ANONYMOUS


def require_authenticated(principal: Principal = None) -> Principal:
    # ``None`` makes direct unit use fail closed; FastAPI always injects the
    # resolver through the route-level wrappers below.
    if principal is None or not principal.authenticated:
        raise HTTPException(status_code=401, detail="Authentication required")
    return principal


def require_coach(principal: Principal) -> Principal:
    require_authenticated(principal)
    if principal.role is not Role.COACH:
        raise HTTPException(status_code=403, detail="Coach authority required")
    return principal


def require_scorer_or_admin(principal: Principal) -> Principal:
    require_authenticated(principal)
    if principal.role not in (Role.SCORER, Role.ADMIN):
        raise HTTPException(status_code=403, detail="Scorer authority required")
    return principal


def require_admin_principal(principal: Principal) -> Principal:
    require_authenticated(principal)
    if principal.role is not Role.ADMIN:
        raise HTTPException(status_code=403, detail="Administrator authority required")
    return principal


def require_secretary_or_admin(principal: Principal) -> Principal:
    """Secretary/League Manager is a genuine operational authority in its
    own right (issue #107): ordinary league-season setup (season entries,
    team names, preseason preparation, draft/fixture/round setup) must not
    require blanket Administrator authority. Administrator remains a
    strict superset for exceptional/privileged operations."""
    require_authenticated(principal)
    if principal.role not in (Role.SECRETARY, Role.ADMIN):
        raise HTTPException(status_code=403, detail="Secretary or administrator authority required")
    return principal


# Capability-based checks (roadmap package #107, issue #107): later Season
# Operations UI pages (#101-#105) should express their authorization
# requirement as a capability an active role does or does not carry, rather
# than repeating raw role-name comparisons in every route/template.
# Administrator implicitly carries every capability; every other role's set
# is closed and explicit here, in one place, rather than scattered.
_WILDCARD_CAPABILITY = "*"
CAPABILITIES: dict[Role, frozenset[str]] = {
    Role.SPECTATOR: frozenset(),
    Role.COACH: frozenset({"own_team.manage"}),
    Role.SCORER: frozenset({"scoring.manage", "lineup.proxy", "draft.participate", "round.review", "player_pool.read"}),
    Role.SECRETARY: frozenset(
        {
            "season.manage",
            "draft.manage",
            "fixture.manage",
            "roundsetup.manage",
            "player_pool.read",
            "player_pool.manage",
        }
    ),
    Role.REPLAY_OPERATOR: frozenset({"draft.participate", "player_pool.read"}),
    Role.ADMIN: frozenset({_WILDCARD_CAPABILITY}),
}


def principal_has_capability(principal: Principal, capability: str) -> bool:
    granted = CAPABILITIES.get(principal.role, frozenset())
    return _WILDCARD_CAPABILITY in granted or capability in granted


def require_capability(capability: str):
    """A ready `Depends(...)`-compatible dependency factory:
    ``Depends(require_capability("season.manage"))``. The one authorization
    primitive #101-#105 and later Season Operations UI pages should build
    on, rather than each inventing its own role check that a future
    permission-matrix change would need to find and update individually."""

    def dependency(principal: Principal = Depends(resolve_principal)) -> Principal:
        require_authenticated(principal)
        if not principal_has_capability(principal, capability):
            raise HTTPException(status_code=403, detail=f"{capability!r} authority required")
        return principal

    return dependency


def require_owned_season_entry(request: Request, principal: Principal, season_entry_id: str) -> None:
    """404 for both absent and foreign entries to prevent private ID enumeration."""
    require_coach(principal)
    if not request.app.state.identities.coach_owns_entry(principal.coach_id, season_entry_id):
        raise HTTPException(status_code=404, detail="Private resource not found")


def require_entry_context(request: Request, principal: Principal, season_entry_id: str) -> None:
    """The one reusable "may this principal act on behalf of this season
    entry" check (roadmap package #107, issue #107), for #101-#105 and any
    future route that writes to a specific season entry: a Coach acting as
    themselves must own it (`require_owned_season_entry`'s existing rule
    unchanged); any delegated active role (Scorer/Secretary/Administrator/
    Replay Operator) must currently be *representing* it -- i.e.
    `principal.represented_season_entry_id`, which only
    `app.routes.context`'s validated switch can set, never a raw request
    parameter. Both branches 404 (never 403) for an entry the principal may
    not act on, matching `require_owned_season_entry`'s enumeration-safe
    posture: a delegated role probing arbitrary entry IDs learns nothing
    about which ones exist outside its own represented context."""
    if principal.role is Role.COACH:
        require_owned_season_entry(request, principal, season_entry_id)
        return
    require_authenticated(principal)
    if principal.represented_season_entry_id != season_entry_id:
        raise HTTPException(status_code=404, detail="Private resource not found")
