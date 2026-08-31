"""Operator fixture-number draw UI over the authoritative fixture repository."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import Principal, require_capability, require_role_covers_season
from app.config import BASE_DIR
from app.routes.auth import _attach_csrf_cookie, _issue_csrf_token

router = APIRouter(prefix="/api/admin/fixture-setup")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
require_fixture_manager = require_capability("fixture.manage")


class FixtureAssignmentRequest(BaseModel):
    entries_by_fixture_number: list[str]


def _actor(principal: Principal) -> ActorContext:
    return ActorContext(actor_type="anonymous_operator", actor_id=principal.coach_id, actor_role=principal.role.value)


def _view(request: Request, season_id: str) -> dict:
    state = request.app.state
    season = state.seasons.get(season_id)
    if season is None:
        raise HTTPException(status_code=404, detail="Unknown season")
    entries = state.identities.list_entries(season_id)
    draw = state.fixtures.get_draw(season_id)
    number_map = state.fixtures.fixture_numbers(season_id)
    names = {entry.season_entry_id: entry.team_name for entry in entries}
    rounds: dict[int, list[dict[str, str]]] = {}
    for matchup in state.fixtures.list_matchups(season_id):
        rounds.setdefault(matchup.bbbffl_round_number, []).append(
            {
                "home_season_entry_id": matchup.home_season_entry_id,
                "away_season_entry_id": matchup.away_season_entry_id,
                "home_team_name": names.get(matchup.home_season_entry_id, "Unknown team"),
                "away_team_name": names.get(matchup.away_season_entry_id, "Unknown team"),
            }
        )
    return {
        "season": {"season_id": season.season_id, "year": season.year, "label": season.label},
        "entries": [entry.__dict__ for entry in entries],
        "draw": draw.__dict__ if draw else None,
        "fixture_numbers": number_map,
        "rounds": [{"round_number": number, "matchups": matchups} for number, matchups in rounds.items()],
        "next_step_url": f"/admin/round-review?season_id={season_id}",
    }


@router.get("/{season_id}")
def fixture_setup(season_id: str, request: Request, principal: Principal = Depends(require_fixture_manager)):
    require_role_covers_season(request, principal, season_id)
    return _view(request, season_id)


@router.post("/{season_id}/preview")
def preview_fixture(
    season_id: str,
    payload: FixtureAssignmentRequest,
    request: Request,
    principal: Principal = Depends(require_fixture_manager),
):
    require_role_covers_season(request, principal, season_id)
    try:
        request.app.state.fixtures.save_draft(
            season_id,
            payload.entries_by_fixture_number,
            actor=_actor(principal),
            reason="Fixture-number draw proposed in operator UI",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown season") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _view(request, season_id)


@router.post("/{season_id}/freeze")
def freeze_fixture(season_id: str, request: Request, principal: Principal = Depends(require_fixture_manager)):
    require_role_covers_season(request, principal, season_id)
    try:
        request.app.state.fixtures.freeze(
            season_id, actor=_actor(principal), reason="Fixture explicitly accepted and frozen in operator UI"
        )
    except KeyError as exc:
        raise HTTPException(status_code=422, detail="Preview a complete fixture before accepting it") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _view(request, season_id)


@page_router.get("/admin/fixture-setup/{season_id}", response_class=HTMLResponse)
def fixture_setup_page(season_id: str, request: Request):
    token = _issue_csrf_token(request)
    response = templates.TemplateResponse(request, "fixture_setup.html", {"season_id": season_id, "csrf_token": token})
    _attach_csrf_cookie(request, response, token)
    return response
