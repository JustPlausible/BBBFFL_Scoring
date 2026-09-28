"""Season activation (issue #239): the Scorer/Administrator browser gate for
the supported `setup -> active` season lifecycle transition.

Thin HTTP translation only -- every readiness rule, refusal and the
atomic lifecycle transition itself live in `app.season_activation`. This
is deliberately a *stricter* authority than `app.routes.season_setup`'s
`roundsetup.manage` capability (which also grants a Secretary):
activation is available only to Scorer or Administrator authority
(`app.authorization.require_scorer_or_admin`), per issue #239's own
authorization requirement, plus the existing season-scoped
`require_role_covers_season` check every other season-model route uses.
Every write requires an explicit reason and -- for a cookie-authenticated
session -- the double-submit CSRF token, exactly like
`app.routes.season_setup`. Mutations are attributed to the acting
operator's own identity.
"""

from __future__ import annotations

import dataclasses

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import Principal, require_role_covers_season, require_scorer_or_admin, resolve_principal
from app.config import BASE_DIR
from app.csrf import issue_token, verify_token
from app.season_activation import ActivationReadiness, activate_season, preview_activate_season

router = APIRouter(prefix="/api/scorer/season-activation")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


class ActivateRequest(BaseModel):
    reason: str | None = None


def require_activation_operator(principal: Principal = Depends(resolve_principal)) -> Principal:
    """Route-level `Depends`-ready wrapper, matching `app.routes.admin`'s/
    `app.routes.season_centre`'s established local-wrapper convention."""
    return require_scorer_or_admin(principal)


def _actor(principal: Principal) -> ActorContext:
    return ActorContext(actor_type="anonymous_operator", actor_id=principal.coach_id, actor_role=principal.role.value)


def _require_session_csrf(request: Request, principal: Principal) -> None:
    """Cookie-authenticated writes need CSRF; header-token writes do not --
    see `app.routes.season_setup`'s identical helper."""
    if principal.session_id is not None and not verify_token(
        request.app.state.settings.session_secret,
        request.cookies.get("bbbffl_csrf"),
        request.headers.get("X-CSRF-Token"),
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _require_known_season(request: Request, season_id: str) -> None:
    """404 for an unknown season before any role-scope check -- matches
    `app.routes.season_setup`'s `_authorise` convention, so an unknown
    `season_id` is never distinguishable from one a season-scoped grant
    simply does not cover."""
    if request.app.state.seasons.get_season(season_id) is None:
        raise HTTPException(status_code=404, detail="Unknown season")


def _readiness_payload(readiness: ActivationReadiness) -> dict:
    return {
        "season_id": readiness.season_id,
        "lifecycle_state": readiness.lifecycle_state,
        "ready": readiness.ready,
        "diagnostic": readiness.diagnostic,
        "checks": [dataclasses.asdict(check) for check in readiness.checks],
    }


@router.get("/{season_id}")
def season_activation_readiness(
    season_id: str, request: Request, principal: Principal = Depends(require_activation_operator)
):
    _require_known_season(request, season_id)
    require_role_covers_season(request, principal, season_id)
    readiness = preview_activate_season(request.app.state.database, season_id)
    return _readiness_payload(readiness)


@router.post("/{season_id}/activate")
def season_activation_activate(
    season_id: str,
    payload: ActivateRequest,
    request: Request,
    principal: Principal = Depends(require_activation_operator),
):
    _require_known_season(request, season_id)
    require_role_covers_season(request, principal, season_id)
    _require_session_csrf(request, principal)
    database = request.app.state.database
    result = activate_season(database, season_id, actor=_actor(principal), reason=payload.reason)
    return {
        "season": dataclasses.asdict(result.season),
        "previous_lifecycle_state": result.previous_lifecycle_state,
        "activation": _readiness_payload(preview_activate_season(database, season_id)),
    }


@page_router.get("/scorer/season-activation/{season_id}", response_class=HTMLResponse)
def season_activation_page(season_id: str, request: Request):
    token = issue_token(request.app.state.settings.session_secret)
    response = templates.TemplateResponse(
        request, "season_activation.html", {"season_id": season_id, "csrf_token": token}
    )
    response.set_cookie(
        "bbbffl_csrf",
        token,
        max_age=3600,
        httponly=True,
        secure=request.app.state.settings.is_production,
        samesite="lax",
    )
    return response
