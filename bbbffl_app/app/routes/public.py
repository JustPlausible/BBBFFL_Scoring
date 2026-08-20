"""Public, read-only Grand Final scoreboard.

The public page and API deliberately expose only the effective (official)
score -- a scorer recommendation is shown as a flag, never folded silently
into the official total.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.service import get_matchup_view

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def serialize_public(state: dict) -> dict:
    """Trims the full state (dataclasses.asdict shape, or a stored FINAL
    snapshot in that same shape) down to what the public page needs --
    effective scores only, never the calculated/override breakdown."""
    return {
        "status": state["status"],
        "finalized_at": state["finalized_at"],
        "leader_team_key": state["leader_team_key"],
        "margin": state["margin"],
        "counts": state["counts"],
        "teams": [
            {
                "team_key": team["team_key"],
                "name": team["name"],
                "total_score": team["total_score"],
                "positions": [
                    {
                        "position": p["position"],
                        "player_name": p["player_name"],
                        "afl_club": p["afl_club"],
                        "match_state": p["match_state"],
                        "slot_source": p["slot_source"],
                        "effective_score": p["effective_score"],
                        "recommended_interchange": p["recommended_interchange"],
                    }
                    for p in team["positions"]
                ],
                "interchange": {
                    "player_name": team["interchange"]["player_name"],
                    "afl_club": team["interchange"]["afl_club"],
                    "dnp": team["interchange"]["dnp"],
                    "target_position": team["interchange"]["target_position"],
                },
            }
            for team in state["teams"]
        ],
    }


def _build_state(request: Request) -> dict:
    state = request.app.state
    return get_matchup_view(state.afl_client, state.teams, state.decisions, state.identity_cache)


@router.get("/", response_class=HTMLResponse)
def public_page(request: Request):
    settings = request.app.state.settings
    return templates.TemplateResponse(
        request,
        "public.html",
        {"poll_interval_seconds": settings.poll_interval_seconds},
    )


@router.get("/api/public/state")
def public_state(request: Request):
    return serialize_public(_build_state(request))
