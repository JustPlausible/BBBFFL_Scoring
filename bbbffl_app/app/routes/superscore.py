"""SuperScore: opt-in, all-in leaderboard competition.

Reuses the same scoring engine, scorer-decision model, and lifecycle
(LIVE -> AWAITING_SCORER_SIGNOFF -> FINAL) as the Grand Final -- see
app/service.py's build_superscore_state, which itself calls the same
build_matchup_state() the Grand Final uses. This module only adds the
competition-layer concerns: validating against the SuperScore entry list
instead of the Grand Final's two teams, and ranking N entries into a
leaderboard instead of comparing two.

Every route here 404s when SuperScore is not configured (see config.py /
main.py) -- opt-in means the feature is invisible, not just inert, when
disabled.
"""

import dataclasses

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.routes.admin import (
    ADMIN_ACTOR,
    SCORER_ACTOR,
    DnpRequest,
    FinalizeRequest,
    InterchangeRequest,
    OverrideRequest,
    require_admin,
)
from app.scoring import ROSTER_SLOTS, SCORABLE_POSITIONS
from app.service import build_superscore_state, get_superscore_view
from app.superscore import superscore_round_label

router = APIRouter(prefix="/api")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def _require_superscore(request: Request) -> None:
    if request.app.state.superscore_config is None:
        raise HTTPException(status_code=404, detail="SuperScore is not enabled")


def _team_keys(request: Request) -> set[str]:
    return {e.team_key for e in request.app.state.superscore_config.entries}


def _ensure_valid_team(request: Request, team_key: str) -> None:
    if team_key not in _team_keys(request):
        raise HTTPException(status_code=404, detail=f"Unknown SuperScore team_key: {team_key}")


def _ensure_not_finalized(request: Request) -> None:
    if request.app.state.superscore_decisions.get_matchup_state().finalized:
        raise HTTPException(
            status_code=423, detail="SuperScore already finalised; decisions are locked"
        )


def _current_state(request: Request) -> dict:
    state = request.app.state
    config = state.superscore_config
    return get_superscore_view(
        state.afl_client, config.entries, state.superscore_decisions, config.season, config.afl_round,
        state.identity_cache,
    )


def serialize_public_superscore(state: dict) -> dict:
    """Trims full SuperScore state down to what the public leaderboard/team
    detail view needs -- effective scores only, mirroring
    routes/public.py's serialize_public() for the Grand Final."""
    return {
        "status": state["status"],
        "season": state["season"],
        "afl_round": state["afl_round"],
        "finalized_at": state["finalized_at"],
        "finalized_note": state["finalized_note"],
        "counts": state["counts"],
        "standings": state["standings"],
        "teams": [
            {
                "team_key": team["team_key"],
                "name": team["name"],
                "total_score": team["total_score"],
                "display_goals": team["display_goals"],
                "display_behinds": team["display_behinds"],
                "football_line": team["football_line"],
                "positions": [
                    {
                        "position": p["position"],
                        "player_name": p["player_name"],
                        "afl_club": p["afl_club"],
                        "match_state": p["match_state"],
                        "slot_source": p["slot_source"],
                        "effective_score": p["effective_score"],
                        "recommended_interchange": p["recommended_interchange"],
                        "starting_dnp": p["starting_dnp"],
                        "starting_player_name": p["starting_player_name"],
                        "display_goals": p["display_goals"],
                        "display_behinds": p["display_behinds"],
                        "display_is_actual_afl": p["display_is_actual_afl"],
                        "display_adjusted_by_override": p["display_adjusted_by_override"],
                        "football_line": p["football_line"],
                    }
                    for p in team["positions"]
                ],
                "interchange": {
                    "player_name": team["interchange"]["player_name"],
                    "afl_club": team["interchange"]["afl_club"],
                    "match_state": team["interchange"]["match_state"],
                    "dnp": team["interchange"]["dnp"],
                    "target_position": team["interchange"]["target_position"],
                    "potential_scores": team["interchange"]["potential_scores"],
                },
            }
            for team in state["teams"]
        ],
    }


# -- Public ---------------------------------------------------------------


@page_router.get("/superscore", response_class=HTMLResponse, dependencies=[Depends(_require_superscore)])
def superscore_page(request: Request):
    settings = request.app.state.settings
    config = request.app.state.superscore_config
    return templates.TemplateResponse(
        request,
        "superscore.html",
        {
            "poll_interval_seconds": settings.poll_interval_seconds,
            "superscore_round_label": superscore_round_label(config.afl_round),
        },
    )


@router.get("/public/superscore/state", dependencies=[Depends(_require_superscore)])
def public_superscore_state(request: Request):
    return serialize_public_superscore(_current_state(request))


# -- Admin ------------------------------------------------------------------


@router.get(
    "/admin/superscore/state", dependencies=[Depends(_require_superscore), Depends(require_admin)]
)
def admin_superscore_state(request: Request):
    return _current_state(request)


@router.post(
    "/admin/superscore/dnp", dependencies=[Depends(_require_superscore), Depends(require_admin)]
)
def set_superscore_dnp(payload: DnpRequest, request: Request):
    _ensure_not_finalized(request)
    _ensure_valid_team(request, payload.team_key)
    if payload.slot not in ROSTER_SLOTS:
        raise HTTPException(status_code=400, detail=f"Unknown slot: {payload.slot}")
    request.app.state.superscore_decisions.set_dnp(
        payload.team_key, payload.slot, payload.dnp, actor=SCORER_ACTOR
    )
    return _current_state(request)


@router.post(
    "/admin/superscore/interchange",
    dependencies=[Depends(_require_superscore), Depends(require_admin)],
)
def set_superscore_interchange(payload: InterchangeRequest, request: Request):
    _ensure_not_finalized(request)
    _ensure_valid_team(request, payload.team_key)
    if payload.target_position is not None and payload.target_position not in SCORABLE_POSITIONS:
        raise HTTPException(
            status_code=400, detail=f"Invalid target_position: {payload.target_position}"
        )
    request.app.state.superscore_decisions.set_interchange_assignment(
        payload.team_key, payload.target_position, actor=SCORER_ACTOR
    )
    return _current_state(request)


@router.post(
    "/admin/superscore/override", dependencies=[Depends(_require_superscore), Depends(require_admin)]
)
def set_superscore_override(payload: OverrideRequest, request: Request):
    _ensure_not_finalized(request)
    _ensure_valid_team(request, payload.team_key)
    if payload.position not in SCORABLE_POSITIONS:
        raise HTTPException(status_code=400, detail=f"Invalid position: {payload.position}")
    request.app.state.superscore_decisions.set_override(
        payload.team_key, payload.position, payload.override_score, payload.reason, actor=SCORER_ACTOR
    )
    return _current_state(request)


@router.post(
    "/admin/superscore/finalize", dependencies=[Depends(_require_superscore), Depends(require_admin)]
)
def finalize_superscore(payload: FinalizeRequest, request: Request):
    state = request.app.state
    config = state.superscore_config
    # Computed once and reused as the frozen snapshot, same rationale as the
    # Grand Final's finalize endpoint: a second afl-api round trip after
    # finalize() commits would risk reporting failure for an
    # already-irreversible finalisation.
    result = build_superscore_state(
        state.afl_client, config.entries, state.superscore_decisions, config.season, config.afl_round,
        state.identity_cache,
    )
    if result.status != "AWAITING_SCORER_SIGNOFF":
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot finalise until all relevant AFL matches are complete "
                "(status must be AWAITING_SCORER_SIGNOFF)."
            ),
        )
    snapshot = dataclasses.asdict(result)
    state.superscore_decisions.finalize(payload.note, snapshot, actor=ADMIN_ACTOR)
    return _current_state(request)


@page_router.get(
    "/admin/superscore", response_class=HTMLResponse, dependencies=[Depends(_require_superscore)]
)
def admin_superscore_page(request: Request):
    config = request.app.state.superscore_config
    return templates.TemplateResponse(
        request,
        "admin_superscore.html",
        {
            "positions": list(SCORABLE_POSITIONS),
            "entries": config.entries,
            "superscore_round_label": superscore_round_label(config.afl_round),
        },
    )
