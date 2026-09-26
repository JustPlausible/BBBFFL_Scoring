"""Season Setup (issue #237): the Scorer/Secretary/Administrator browser
surface for production-safe fresh-season and phase initialization -- player
pool population from live afl-api, ordinary competition structure, Opening
Round compensating-bye rules, squad limit and preseason draft order, and
later the Finals bracket and SuperScore structure.

Thin HTTP translation only: every prerequisite, idempotency and conflict
rule lives in `app.season_setup` (and the domain boundaries it calls).
Every endpoint requires `roundsetup.manage` (Scorer, Secretary or
Administrator) plus `require_role_covers_season`, and every write requires
an explicit reason and -- for a cookie-authenticated session -- the
double-submit CSRF token, exactly like `app.routes.fixture_setup`.
Mutations are attributed to the acting operator's own identity.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import Principal, require_capability, require_role_covers_season
from app.config import BASE_DIR
from app.csrf import issue_token, verify_token
from app.season_setup import (
    accept_draft_order,
    accept_opening_round_rules,
    build_season_setup,
    configure_squad_limit,
    initialize_finals,
    initialize_ordinary_competition,
    initialize_superscore,
    list_afl_seasons,
    preview_opening_round,
    refresh_player_pool,
)

router = APIRouter(prefix="/api/admin/season-setup")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
require_setup_operator = require_capability("roundsetup.manage")


class ReasonRequest(BaseModel):
    reason: str | None = None


class PlayerPoolRequest(ReasonRequest):
    afl_season_id: int


class SquadLimitRequest(ReasonRequest):
    squad_limit: int


class DraftOrderRequest(ReasonRequest):
    ordered_entry_ids: list[str]


class OpeningRoundTarget(BaseModel):
    afl_club_id: int
    bbbffl_round_number: int


class OpeningRoundRequest(ReasonRequest):
    afl_season_id: int
    targets: list[OpeningRoundTarget]


def _actor(principal: Principal) -> ActorContext:
    return ActorContext(actor_type="anonymous_operator", actor_id=principal.coach_id, actor_role=principal.role.value)


def _require_session_csrf(request: Request, principal: Principal) -> None:
    """Cookie-authenticated writes need CSRF; header-token writes do not."""
    if principal.session_id is not None and not verify_token(
        request.app.state.settings.session_secret,
        request.cookies.get("bbbffl_csrf"),
        request.headers.get("X-CSRF-Token"),
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _authorise(request: Request, principal: Principal, season_id: str, *, write: bool = False) -> None:
    if request.app.state.seasons.get_season(season_id) is None:
        raise HTTPException(status_code=404, detail="Unknown season")
    require_role_covers_season(request, principal, season_id)
    if write:
        _require_session_csrf(request, principal)


def _result(request: Request, season_id: str, result: dict) -> dict:
    return {"result": result, "setup": build_season_setup(request.app.state.database, season_id)}


@router.get("/{season_id}")
def season_setup(season_id: str, request: Request, principal: Principal = Depends(require_setup_operator)):
    _authorise(request, principal, season_id)
    return build_season_setup(request.app.state.database, season_id)


@router.get("/{season_id}/afl-seasons")
def afl_seasons(season_id: str, request: Request, principal: Principal = Depends(require_setup_operator)):
    _authorise(request, principal, season_id)
    state = request.app.state
    return {"seasons": list_afl_seasons(state.database, state.afl_client, season_id)}


@router.post("/{season_id}/player-pool")
def player_pool(
    season_id: str, payload: PlayerPoolRequest, request: Request, principal: Principal = Depends(require_setup_operator)
):
    _authorise(request, principal, season_id, write=True)
    state = request.app.state
    result = refresh_player_pool(
        state.database,
        state.afl_client,
        season_id,
        payload.afl_season_id,
        actor=_actor(principal),
        reason=payload.reason,
    )
    return _result(request, season_id, result)


@router.post("/{season_id}/ordinary-competition")
def ordinary_competition(
    season_id: str, payload: ReasonRequest, request: Request, principal: Principal = Depends(require_setup_operator)
):
    _authorise(request, principal, season_id, write=True)
    result = initialize_ordinary_competition(
        request.app.state.database, season_id, actor=_actor(principal), reason=payload.reason
    )
    return _result(request, season_id, result)


@router.get("/{season_id}/opening-round")
def opening_round_preview(
    season_id: str, afl_season_id: int, request: Request, principal: Principal = Depends(require_setup_operator)
):
    _authorise(request, principal, season_id)
    state = request.app.state
    return preview_opening_round(state.database, state.afl_client, season_id, afl_season_id)


@router.post("/{season_id}/opening-round")
def opening_round_accept(
    season_id: str,
    payload: OpeningRoundRequest,
    request: Request,
    principal: Principal = Depends(require_setup_operator),
):
    _authorise(request, principal, season_id, write=True)
    targets: dict[int, int] = {}
    for target in payload.targets:
        if target.afl_club_id in targets:
            raise HTTPException(status_code=400, detail=f"club {target.afl_club_id} is listed more than once")
        targets[target.afl_club_id] = target.bbbffl_round_number
    state = request.app.state
    result = accept_opening_round_rules(
        state.database,
        state.afl_client,
        season_id,
        payload.afl_season_id,
        targets,
        actor=_actor(principal),
        reason=payload.reason,
    )
    return _result(request, season_id, result)


@router.post("/{season_id}/squad-limit")
def squad_limit(
    season_id: str, payload: SquadLimitRequest, request: Request, principal: Principal = Depends(require_setup_operator)
):
    _authorise(request, principal, season_id, write=True)
    result = configure_squad_limit(
        request.app.state.database, season_id, payload.squad_limit, actor=_actor(principal), reason=payload.reason
    )
    return _result(request, season_id, result)


@router.post("/{season_id}/draft-order")
def draft_order(
    season_id: str, payload: DraftOrderRequest, request: Request, principal: Principal = Depends(require_setup_operator)
):
    _authorise(request, principal, season_id, write=True)
    state = request.app.state
    result = accept_draft_order(
        state.database,
        state.afl_client,
        season_id,
        payload.ordered_entry_ids,
        actor=_actor(principal),
        reason=payload.reason,
    )
    return _result(request, season_id, result)


@router.post("/{season_id}/finals")
def finals(
    season_id: str, payload: ReasonRequest, request: Request, principal: Principal = Depends(require_setup_operator)
):
    _authorise(request, principal, season_id, write=True)
    result = initialize_finals(request.app.state.database, season_id, actor=_actor(principal), reason=payload.reason)
    return _result(request, season_id, result)


@router.post("/{season_id}/superscore")
def superscore(
    season_id: str, payload: ReasonRequest, request: Request, principal: Principal = Depends(require_setup_operator)
):
    _authorise(request, principal, season_id, write=True)
    result = initialize_superscore(
        request.app.state.database, season_id, actor=_actor(principal), reason=payload.reason
    )
    return _result(request, season_id, result)


@page_router.get("/admin/season-setup/{season_id}", response_class=HTMLResponse)
def season_setup_page(season_id: str, request: Request):
    token = issue_token(request.app.state.settings.session_secret)
    response = templates.TemplateResponse(request, "season_setup.html", {"season_id": season_id, "csrf_token": token})
    response.set_cookie(
        "bbbffl_csrf",
        token,
        max_age=3600,
        httponly=True,
        secure=request.app.state.settings.is_production,
        samesite="lax",
    )
    return response
