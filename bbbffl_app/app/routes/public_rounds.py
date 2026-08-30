"""Anonymous ordinary-season Round Centre and ladder routes."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.public_rounds import build_public_ladder, build_public_round

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


@router.get("/", response_class=HTMLResponse)
def regular_season_home(request: Request):
    """Enter the latest persisted ordinary season without requiring a UUID.

    ``list_ordinary_rounds`` is ordered newest AFL season/round first. Linking
    to the season URL deliberately delegates round choice to
    :func:`season_overview`, keeping one owner for that policy.
    """
    rounds = request.app.state.lifecycle.list_ordinary_rounds()
    if rounds:
        return RedirectResponse(f"/seasons/{rounds[0].season_id}", status_code=302)
    return templates.TemplateResponse(
        request,
        "regular_season_empty.html",
        {"superscore_enabled": request.app.state.superscore_config is not None},
    )


def _round(request, season_id, round_id):
    state = request.app.state
    try:
        result = build_public_round(state.database, state.lifecycle, state.round_review, state.identities, round_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Public round not found") from exc
    if result["season_id"] != season_id:
        raise HTTPException(status_code=404, detail="Public round not found")
    return result


@router.get("/api/public/seasons/{season_id}/rounds/{round_id}")
def round_state(season_id: str, round_id: str, request: Request):
    return _round(request, season_id, round_id)


@router.get("/api/public/seasons/{season_id}/rounds/{round_id}/ladder")
def ladder_state(season_id: str, round_id: str, request: Request):
    public_round = _round(request, season_id, round_id)
    round_ = request.app.state.lifecycle.get_round(round_id)
    return build_public_ladder(
        request.app.state.ladder,
        request.app.state.identities,
        round_.competition_id,
        public_round["round_number"],
    )


@router.get("/seasons/{season_id}")
def season_overview(season_id: str, request: Request):
    rounds = [r for r in request.app.state.lifecycle.list_ordinary_rounds() if r.season_id == season_id]
    if not rounds:
        raise HTTPException(status_code=404, detail="Public season not found")
    return RedirectResponse(f"/seasons/{season_id}/rounds/{rounds[0].bbbffl_round_id}", status_code=302)


@router.get("/seasons/{season_id}/rounds/{round_id}", response_class=HTMLResponse)
def round_page(season_id: str, round_id: str, request: Request):
    _round(request, season_id, round_id)
    return templates.TemplateResponse(
        request,
        "public_round_centre.html",
        {
            "season_id": season_id,
            "round_id": round_id,
            "poll_interval_seconds": request.app.state.settings.poll_interval_seconds,
        },
    )
