"""Scorer/admin Season Centre (issue #100) -- an operator surface over the
season-model domain established by roadmap packages 09-10 (issues #19/#20):
season identity/lifecycle, private coach records, and public season-entry
team identity. It is the human replay operator's way to establish and
inspect recognisable BBBFFL season state (real coach names, real BBBFFL team
names) before draft/fixture/round setup, without SQL or direct database
manipulation.

Every rule -- non-empty names, valid coach/season references, licence
uniqueness -- lives in `app.season_centre` (called through its thin
read-model/command wrappers, which in turn call `app.identity`/`app.season`).
Nothing here decides any of that. Like `app.routes.round_review`, this module
imports `app.season_centre` directly (see `tests/test_architecture.py`'s
`SEASON_CENTRE` group) rather than reaching into `app.identity`/`app.season`
itself, and every read rebuilds its response from the database through the
already-constructed repositories on `request.app.state`.

This module deliberately does not implement draft, fixture-generation, round
-configuration or weekly scoring/selection behaviour -- see `app.routes.
draft` and `app.routes.round_review` for those; this router only links to
their existing pages once their own state says they are reachable.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import Principal
from app.config import BASE_DIR
from app.routes.admin import require_admin
from app.season_centre import build_season_centre, list_coaches_overview, list_seasons_overview
from app.season_centre import create_coach as create_coach_service
from app.season_centre import create_entry as create_entry_service
from app.season_centre import create_season as create_season_service
from app.season_centre import rename_team as rename_team_service
from app.season_centre import transfer_entry as transfer_entry_service
from app.season_centre import update_coach as update_coach_service

router = APIRouter(prefix="/api/admin/season-centre")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


class CreateSeasonRequest(BaseModel):
    year: int
    label: str
    regular_season_round_count: int = 20


class CreateCoachRequest(BaseModel):
    display_name: str
    email: str | None = None
    phone: str | None = None
    profile_notes: str | None = None


class UpdateCoachRequest(BaseModel):
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    profile_notes: str | None = None
    reason: str | None = None


class CreateEntryRequest(BaseModel):
    coach_id: str
    team_name: str
    licence_key: str | None = None
    reason: str | None = None


class RenameTeamRequest(BaseModel):
    team_name: str
    reason: str | None = None


class TransferCoachRequest(BaseModel):
    coach_id: str
    reason: str | None = None


def _actor(principal: Principal) -> ActorContext:
    """Provenance authority comes only from the resolved credential -- see
    `app.routes.round_review`'s identical `_actor` helper, which this
    mirrors."""
    return ActorContext(actor_type="anonymous_operator", actor_id=None, actor_role=principal.role.value)


@router.get("/seasons", dependencies=[Depends(require_admin)])
def list_seasons(request: Request):
    return list_seasons_overview(request.app.state.seasons)


@router.post("/seasons", dependencies=[Depends(require_admin)])
def create_season(payload: CreateSeasonRequest, request: Request):
    return create_season_service(
        request.app.state.seasons,
        payload.year,
        payload.label,
        regular_season_round_count=payload.regular_season_round_count,
    )


@router.get("/coaches", dependencies=[Depends(require_admin)])
def list_coaches(request: Request):
    return list_coaches_overview(request.app.state.identities)


@router.post("/coaches", dependencies=[Depends(require_admin)])
def create_coach(payload: CreateCoachRequest, request: Request):
    return create_coach_service(
        request.app.state.identities,
        payload.display_name,
        email=payload.email,
        phone=payload.phone,
        profile_notes=payload.profile_notes,
    )


@router.post("/coaches/{coach_id}", dependencies=[Depends(require_admin)])
def update_coach(
    coach_id: str, payload: UpdateCoachRequest, request: Request, principal: Principal = Depends(require_admin)
):
    return update_coach_service(
        request.app.state.identities,
        coach_id,
        display_name=payload.display_name,
        email=payload.email,
        phone=payload.phone,
        profile_notes=payload.profile_notes,
        actor=_actor(principal),
        reason=payload.reason,
    )


@router.get("/{season_id}", dependencies=[Depends(require_admin)])
def season_centre(season_id: str, request: Request):
    state = request.app.state
    return build_season_centre(
        state.seasons, state.identities, state.draft, state.preseason, state.player_pool, state.lifecycle, season_id
    )


@router.post("/{season_id}/entries", dependencies=[Depends(require_admin)])
def create_entry(
    season_id: str, payload: CreateEntryRequest, request: Request, principal: Principal = Depends(require_admin)
):
    create_entry_service(
        request.app.state.identities,
        season_id,
        payload.coach_id,
        payload.team_name,
        licence_key=payload.licence_key,
        actor=_actor(principal),
        reason=payload.reason,
    )
    state = request.app.state
    return build_season_centre(
        state.seasons, state.identities, state.draft, state.preseason, state.player_pool, state.lifecycle, season_id
    )


@router.post("/entries/{entry_id}/team-name", dependencies=[Depends(require_admin)])
def rename_team(
    entry_id: str, payload: RenameTeamRequest, request: Request, principal: Principal = Depends(require_admin)
):
    state = request.app.state
    rename_team_service(state.identities, entry_id, payload.team_name, actor=_actor(principal), reason=payload.reason)
    entry = state.identities.get_public_team(entry_id)
    return build_season_centre(
        state.seasons,
        state.identities,
        state.draft,
        state.preseason,
        state.player_pool,
        state.lifecycle,
        entry.season_id,
    )


@router.post("/entries/{entry_id}/coach", dependencies=[Depends(require_admin)])
def transfer_entry(
    entry_id: str, payload: TransferCoachRequest, request: Request, principal: Principal = Depends(require_admin)
):
    state = request.app.state
    transfer_entry_service(state.identities, entry_id, payload.coach_id, actor=_actor(principal), reason=payload.reason)
    entry = state.identities.get_public_team(entry_id)
    return build_season_centre(
        state.seasons,
        state.identities,
        state.draft,
        state.preseason,
        state.player_pool,
        state.lifecycle,
        entry.season_id,
    )


@page_router.get("/admin/season-centre", response_class=HTMLResponse)
def season_centre_index_page(request: Request):
    return templates.TemplateResponse(request, "season_centre.html", {"season_id": None})


@page_router.get("/admin/season-centre/{season_id}", response_class=HTMLResponse)
def season_centre_page(season_id: str, request: Request):
    return templates.TemplateResponse(request, "season_centre.html", {"season_id": season_id})
