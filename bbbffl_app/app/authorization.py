"""Single request-level authorization and privacy boundary.

Authentication establishes identity; this module turns request credentials into
one of the four authorities understood by HTTP routes.  Domain services remain
usable without HTTP and continue to enforce their own invariants.
"""

from dataclasses import dataclass
from enum import StrEnum

from fastapi import Header, HTTPException, Request


class Role(StrEnum):
    SPECTATOR = "spectator"
    COACH = "coach"
    SCORER = "scorer"
    ADMIN = "admin"


@dataclass(frozen=True)
class Principal:
    role: Role
    coach_id: str | None = None
    display_name: str | None = None

    @property
    def authenticated(self) -> bool:
        return self.role is not Role.SPECTATOR


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
    """
    configured = request.app.state.settings.admin_token
    if x_admin_token is not None:
        if not configured or x_admin_token != configured:
            raise HTTPException(status_code=401, detail="Invalid X-Admin-Token")
        if x_authority_role not in (None, Role.ADMIN, Role.SCORER):
            raise HTTPException(status_code=403, detail="Unknown operator authority")
        return Principal(Role(x_authority_role or Role.ADMIN))

    token = request.cookies.get(SESSION_COOKIE_NAME)
    coach = request.app.state.auth_service.resolve(token)
    if coach is not None:
        return Principal(Role.COACH, coach.coach_id, coach.display_name)
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


def require_owned_season_entry(request: Request, principal: Principal, season_entry_id: str) -> None:
    """404 for both absent and foreign entries to prevent private ID enumeration."""
    require_coach(principal)
    if not request.app.state.identities.coach_owns_entry(principal.coach_id, season_entry_id):
        raise HTTPException(status_code=404, detail="Private resource not found")
