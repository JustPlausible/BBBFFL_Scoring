"""Shared active-context API (roadmap package #107, issue #107).

Two things live here, both built on `app.auth.ActingContextService`:

- **Self-service context switching** (`/api/context*`) -- a signed-in coach
  identity inspects which roles/represented entries it may currently use,
  and switches its session's active role or represented season entry. This
  is the one reusable mechanism #101-#105 and the existing weekly coach/
  scorer surfaces should all drive their "Acting as ..." presentation from
  (see `app.authorization.Principal`) -- never page-specific state.
- **Role-grant administration** (`/api/admin/role-grants*`) -- granting or
  revoking a coach identity's Scorer/Secretary/Administrator/Replay
  Operator authority is itself an account/role-administration operation,
  so it requires strict Administrator authority (`require_admin`), never
  Secretary -- matching the issue's "Secretary/League Manager ... should
  not require blanket Administrator authority for ordinary league setup,
  but role administration is exactly the kind of exceptional/privileged
  operation Administrator authority remains for.

Context-switch endpoints require a *coach-session* principal specifically
(`principal.coach_id is not None`) -- the legacy shared `X-Admin-Token`
credential has no persistent per-user session row to switch, and is not
this package's concern (see docs/acting-context.md and
docs/coach-authentication.md).
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import Principal, require_admin_principal, resolve_principal
from app.routes.auth import _verify_csrf

router = APIRouter()


class ActivateRoleRequest(BaseModel):
    role: str


class SetRepresentedEntryRequest(BaseModel):
    season_entry_id: str | None = None


class GrantRoleRequest(BaseModel):
    coach_id: str
    role: str
    season_id: str | None = None
    reason: str | None = None


class RevokeRoleRequest(BaseModel):
    reason: str | None = None


def _require_coach_session(principal: Principal = Depends(resolve_principal)) -> Principal:
    """A signed-in coach identity specifically -- not the legacy shared
    admin-token credential, which has no session row to switch (see module
    docstring)."""
    if principal.coach_id is None or principal.session_id is None:
        raise HTTPException(status_code=401, detail="A coach session is required to switch active context")
    return principal


def _require_csrf(request: Request, csrf_token: str | None) -> None:
    if not _verify_csrf(request, csrf_token or ""):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _context_view(request: Request, principal: Principal) -> dict:
    represented = None
    if principal.represented_season_entry_id:
        entry = request.app.state.identities.get_public_team(principal.represented_season_entry_id)
        if entry is not None:
            represented = {
                "season_entry_id": entry.season_entry_id,
                "season_id": entry.season_id,
                "team_name": entry.team_name,
            }
    return {
        "coach_id": principal.coach_id,
        "display_name": principal.display_name,
        "active_role": principal.role.value,
        "granted_roles": sorted(r.value for r in principal.granted_roles),
        "represented_season_entry": represented,
        "is_replay_context": principal.is_replay_context,
    }


@router.get("/api/context")
def get_context(request: Request, principal: Principal = Depends(resolve_principal)):
    return _context_view(request, principal)


@router.post("/api/context/role")
def activate_role(
    payload: ActivateRoleRequest,
    request: Request,
    principal: Principal = Depends(_require_coach_session),
):
    _require_csrf(request, request.headers.get("X-CSRF-Token"))
    request.app.state.acting_context.activate_role(
        coach_id=principal.coach_id,
        session_id=principal.session_id,
        role=payload.role,
        actor=ActorContext.coach(principal.coach_id),
    )
    refreshed = resolve_principal(request, x_admin_token=None, x_authority_role=None)
    return _context_view(request, refreshed)


@router.post("/api/context/represented-entry")
def set_represented_entry(
    payload: SetRepresentedEntryRequest,
    request: Request,
    principal: Principal = Depends(_require_coach_session),
):
    _require_csrf(request, request.headers.get("X-CSRF-Token"))
    request.app.state.acting_context.set_represented_entry(
        coach_id=principal.coach_id,
        session_id=principal.session_id,
        active_role=principal.role.value,
        season_entry_id=payload.season_entry_id,
        actor=ActorContext.coach(principal.coach_id),
    )
    refreshed = resolve_principal(request, x_admin_token=None, x_authority_role=None)
    return _context_view(request, refreshed)


@router.get("/api/context/entries")
def representable_entries(season_id: str, request: Request, principal: Principal = Depends(_require_coach_session)):
    entries = request.app.state.acting_context.representable_entries(
        principal.coach_id, principal.role.value, season_id
    )
    return [
        {"season_entry_id": e.season_entry_id, "team_name": e.team_name, "coach_display_name": e.coach_display_name}
        for e in entries
    ]


# -- Administrator-only role-grant management --------------------------


def require_admin(principal: Principal = Depends(resolve_principal)) -> Principal:
    return require_admin_principal(principal)


@router.get("/api/admin/role-grants", dependencies=[Depends(require_admin)])
def list_role_grants(coach_id: str, request: Request):
    return [
        {
            "grant_id": g.grant_id,
            "coach_id": g.coach_id,
            "role": g.role,
            "season_id": g.season_id,
            "granted_at": g.granted_at,
            "revoked_at": g.revoked_at,
            "reason": g.reason,
        }
        for g in request.app.state.role_grants.list_all_for_coach(coach_id)
    ]


@router.post("/api/admin/role-grants", dependencies=[Depends(require_admin)])
def grant_role(payload: GrantRoleRequest, request: Request, principal: Principal = Depends(require_admin)):
    # `RoleGrantRepository.grant` itself validates `role` against
    # `GRANTABLE_ROLES` (raising `InvalidRoleError`, a `ValueError` mapped
    # to HTTP 400 by app/main.py's existing generic handler) -- no
    # duplicate check needed here.
    actor = ActorContext(actor_type="anonymous_operator", actor_id=principal.coach_id, actor_role=principal.role.value)
    grant = request.app.state.role_grants.grant(
        payload.coach_id, payload.role, actor=actor, season_id=payload.season_id, reason=payload.reason
    )
    return {"grant_id": grant.grant_id, "coach_id": grant.coach_id, "role": grant.role, "season_id": grant.season_id}


@router.post("/api/admin/role-grants/{grant_id}/revoke", dependencies=[Depends(require_admin)])
def revoke_role(
    grant_id: str,
    request: Request,
    payload: RevokeRoleRequest = RevokeRoleRequest(),
    principal: Principal = Depends(require_admin),
):
    actor = ActorContext(actor_type="anonymous_operator", actor_id=principal.coach_id, actor_role=principal.role.value)
    revoked = request.app.state.role_grants.revoke(grant_id, actor=actor, reason=payload.reason)
    if not revoked:
        raise HTTPException(status_code=404, detail="Unknown or already-revoked role grant")
    return {"grant_id": grant_id, "status": "revoked"}
