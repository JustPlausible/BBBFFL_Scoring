"""Anonymous ordinary-season public landing page, round browser and Round
Centre/ladder routes (issue #161 extends issue #78's original Round Centre
with a season-wide round browser; both stay allow-listed DTOs built
entirely from :mod:`app.public_rounds`)."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.public_rounds import (
    build_public_ladder,
    build_public_round,
    build_public_round_by_number,
    build_public_season_rounds,
    latest_ordinary_season_id,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


@router.get("/", response_class=HTMLResponse)
def regular_season_home(request: Request):
    """Enter the latest ordinary season without requiring a UUID.

    Linking to the season URL deliberately delegates round choice to
    :func:`season_overview`, keeping one owner for that policy.
    """
    state = request.app.state
    season_id = latest_ordinary_season_id(state.database, state.seasons)
    if season_id:
        return RedirectResponse(f"/seasons/{season_id}", status_code=302)
    return templates.TemplateResponse(
        request,
        "regular_season_empty.html",
        {"superscore_enabled": request.app.state.superscore_config is not None},
    )


def _season_rounds(request, season_id):
    try:
        return build_public_season_rounds(request.app.state.database, request.app.state.seasons, season_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Public season not found") from exc


def _round(request, season_id, round_id):
    state = request.app.state
    try:
        result = build_public_round(state.database, state.lifecycle, state.round_review, state.identities, round_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Public round not found") from exc
    if result["season_id"] != season_id:
        raise HTTPException(status_code=404, detail="Public round not found")
    return result


@router.get("/api/public/seasons/{season_id}/rounds")
def season_round_index(season_id: str, request: Request):
    index = _season_rounds(request, season_id)
    # `competition_id` is an internal join key (never previously part of any
    # public payload); the round-by-number/ladder routes below use it
    # server-side only, so it is deliberately not echoed to the client here.
    return {
        "season_id": index["season_id"],
        "rounds": index["rounds"],
        "default_round_number": index["default_round_number"],
    }


@router.get("/api/public/seasons/{season_id}/rounds/{round_number:int}")
def round_by_number(season_id: str, round_number: int, request: Request):
    index = _season_rounds(request, season_id)
    total = len(index["rounds"])
    if not 1 <= round_number <= total:
        raise HTTPException(status_code=404, detail="Public round not found")
    state = request.app.state
    return build_public_round_by_number(
        state.database,
        state.lifecycle,
        state.round_review,
        state.identities,
        state.fixtures,
        season_id,
        index["competition_id"],
        round_number,
        total,
    )


@router.get("/api/public/seasons/{season_id}/rounds/{round_number:int}/ladder")
def ladder_by_number(season_id: str, round_number: int, request: Request):
    index = _season_rounds(request, season_id)
    if not 1 <= round_number <= len(index["rounds"]):
        raise HTTPException(status_code=404, detail="Public round not found")
    return build_public_ladder(
        request.app.state.ladder, request.app.state.identities, index["competition_id"], round_number
    )


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
    """The season landing page: redirect to the current (else most recently
    published) round's canonical, round-number-keyed URL -- see
    ``build_public_season_rounds``' default-round policy."""
    index = _season_rounds(request, season_id)
    if index["default_round_number"] is None:
        raise HTTPException(status_code=404, detail="Public season not found")
    return RedirectResponse(f"/seasons/{season_id}/rounds/{index['default_round_number']}", status_code=302)


@router.get("/seasons/{season_id}/rounds/{round_number:int}", response_class=HTMLResponse)
def season_round_browser_page(season_id: str, round_number: int, request: Request):
    """Canonical season/round browser page (issue #161): round selector,
    previous/next navigation, the round's five matchups (scheduled preview
    or published result) and the ladder as it stood after the selected
    published round."""
    index = _season_rounds(request, season_id)
    if not 1 <= round_number <= len(index["rounds"]):
        raise HTTPException(status_code=404, detail="Public round not found")
    return templates.TemplateResponse(
        request,
        "public_season_rounds.html",
        {"season_id": season_id, "round_number": round_number},
    )


@router.get("/seasons/{season_id}/rounds/{round_id}", response_class=HTMLResponse)
def round_page(season_id: str, round_id: str, request: Request):
    """The existing detailed public result/player-evidence view (issue
    #78), unchanged: the season/round browser links each matchup through to
    this page for its full lineup breakdown."""
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
