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

from contextlib import nullcontext

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.routes.admin import (
    DnpRequest,
    FinalizeRequest,
    InterchangeRequest,
    OverrideRequest,
    require_admin,
)
from app.scorer_decisions import finalize as finalize_result
from app.scorer_decisions import set_dnp as apply_dnp_decision
from app.scorer_decisions import set_interchange as apply_interchange_decision
from app.scorer_decisions import set_override as apply_override_decision
from app.scoring import SCORABLE_POSITIONS
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


def _current_state(request: Request) -> dict:
    state = request.app.state
    config = state.superscore_config
    return get_superscore_view(
        state.afl_client,
        config.entries,
        state.superscore_decisions,
        config.season,
        config.afl_round,
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
                    "dnp_ruling": team["interchange"]["dnp_ruling"],
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


@router.get("/admin/superscore/state", dependencies=[Depends(_require_superscore), Depends(require_admin)])
def admin_superscore_state(request: Request):
    return _current_state(request)


@router.post("/admin/superscore/dnp", dependencies=[Depends(_require_superscore), Depends(require_admin)])
def set_superscore_dnp(payload: DnpRequest, request: Request):
    apply_dnp_decision(
        request.app.state.superscore_decisions,
        _team_keys(request),
        payload.team_key,
        payload.slot,
        payload.dnp,
        reason=payload.reason,
    )
    return _current_state(request)


@router.post(
    "/admin/superscore/interchange",
    dependencies=[Depends(_require_superscore), Depends(require_admin)],
)
def set_superscore_interchange(payload: InterchangeRequest, request: Request):
    apply_interchange_decision(
        request.app.state.superscore_decisions,
        _team_keys(request),
        payload.team_key,
        payload.target_position,
        reason=payload.reason,
    )
    return _current_state(request)


@router.post("/admin/superscore/override", dependencies=[Depends(_require_superscore), Depends(require_admin)])
def set_superscore_override(payload: OverrideRequest, request: Request):
    apply_override_decision(
        request.app.state.superscore_decisions,
        _team_keys(request),
        payload.team_key,
        payload.position,
        payload.override_score,
        payload.reason,
    )
    return _current_state(request)


@router.post("/admin/superscore/finalize", dependencies=[Depends(_require_superscore), Depends(require_admin)])
def finalize_superscore(payload: FinalizeRequest, request: Request):
    state = request.app.state
    config = state.superscore_config
    afl_client = state.afl_client
    # See app/routes/admin.py's finalize handler for why this scopes the
    # freshness check to an evidence_batch() rather than
    # is_evidence_fresh() alone.
    evidence_batch = getattr(afl_client, "evidence_batch", None)
    scope = evidence_batch() if callable(evidence_batch) else nullcontext(afl_client)
    with scope as evidence:
        # Computed once and passed to finalize_result() as the frozen
        # snapshot -- see app.scorer_decisions.finalize's docstring for why
        # a second afl-api round trip after the write commits would be
        # unsafe.
        result = build_superscore_state(
            afl_client,
            config.entries,
            state.superscore_decisions,
            config.season,
            config.afl_round,
            state.identity_cache,
        )
        finalize_result(result, state.superscore_decisions, payload.note, afl_client=evidence)
    return _current_state(request)


@page_router.get("/admin/superscore", response_class=HTMLResponse, dependencies=[Depends(_require_superscore)])
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
