"""Scorer Operations Dashboard (issue #147) -- the role-aware aggregation/
navigation surface that answers "what requires scorer attention now, what
is waiting on somebody else, and what is the next safe action?".

This router is a thin HTTP translation over `app.scorer_dashboard`'s read
model: every authorization decision happens here (never inferred from a
browser-supplied season/round identifier), and every mutation the
dashboard links to remains in its existing owning route module -- nothing
here writes anything.

Authorization mirrors `app/routes/round_review.py`'s `require_round_reviewer`
exactly (Scorer/Replay-Operator/Administrator; never Coach or Secretary),
plus per-season scoping via `app.authorization.require_role_covers_season`
so a Replay-Operator or season-scoped Scorer grant can never view a season
outside its own grant.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.authorization import Principal, Role, require_authenticated, require_role_covers_season, resolve_principal
from app.config import BASE_DIR
from app.scorer_dashboard import build_scorer_dashboard

router = APIRouter(prefix="/api/scorer/dashboard")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def require_scorer_dashboard(principal: Principal = Depends(resolve_principal)) -> Principal:
    """Same authority boundary as `app.routes.round_review.
    require_round_reviewer`: Scorer, Replay Operator or Administrator only.
    Coach must never gain scorer information/actions (issue #147); Secretary
    -- ordinary league-season setup authority -- has no scoring/round-review
    capability either and is deliberately excluded here too."""
    require_authenticated(principal)
    if principal.role not in (Role.SCORER, Role.REPLAY_OPERATOR, Role.ADMIN):
        raise HTTPException(status_code=403, detail="Scorer dashboard authority required")
    return principal


def _authorized_season_ids(request: Request, principal: Principal) -> set[str] | None:
    """The season IDs `principal`'s *active role* is granted for, or
    ``None`` meaning every season (Administrator, or the legacy shared
    admin-token credential, are never season-scoped -- see
    `app.authorization.require_role_covers_season`'s own docstring)."""
    if principal.role is Role.ADMIN or principal.coach_id is None:
        return None
    grants = request.app.state.role_grants.list_active_for_coach(principal.coach_id)
    season_ids: set[str] = set()
    for grant in grants:
        if grant.role != principal.role.value:
            continue
        if grant.season_id is None:
            return None
        season_ids.add(grant.season_id)
    return season_ids


def _authorized_seasons(request: Request, principal: Principal) -> list[dict]:
    covered = _authorized_season_ids(request, principal)
    seasons = request.app.state.seasons.list_seasons()
    if covered is not None:
        seasons = [season for season in seasons if season.season_id in covered]
    return [{"season_id": season.season_id, "year": season.year, "label": season.label} for season in seasons]


def _resolve_season_id(request: Request, principal: Principal, requested: str | None) -> str | None:
    """Never trusts a browser-supplied season id as authority by itself:
    an explicit `requested` id must still pass `require_role_covers_season`
    below. Falls back to the most recent season this principal's active
    role actually covers when none was requested (or the requested one is
    not authorised)."""
    authorized = _authorized_seasons(request, principal)
    if requested is not None:
        require_role_covers_season(request, principal, requested)
        return requested
    return authorized[0]["season_id"] if authorized else None


@router.get("")
def get_dashboard(
    request: Request,
    season_id: str | None = None,
    round_id: str | None = None,
    principal: Principal = Depends(require_scorer_dashboard),
):
    resolved_season_id = _resolve_season_id(request, principal, season_id)
    seasons = _authorized_seasons(request, principal)
    if resolved_season_id is None:
        return {
            "acting_context": _acting_context(principal),
            "seasons": seasons,
            "dashboard": None,
        }
    state = request.app.state
    dashboard = build_scorer_dashboard(
        state.database,
        state.lifecycle,
        state.identities,
        state.seasons,
        state.round_review,
        state.audit_events,
        state.afl_client,
        resolved_season_id,
        round_id=round_id,
    )
    return {
        "acting_context": _acting_context(principal),
        "seasons": seasons,
        "dashboard": dashboard,
    }


def _acting_context(principal: Principal) -> dict:
    return {
        "coach_id": principal.coach_id,
        "display_name": principal.display_name,
        "active_role": principal.role.value,
        "is_replay_context": principal.is_replay_context,
    }


@page_router.get("/scorer", response_class=HTMLResponse)
def scorer_home_page(request: Request):
    """The discoverable Scorer operational home (issue #147): an
    authenticated visitor with no active Scorer/Replay-Operator/
    Administrator role sees a clear authorization message here rather than
    the raw 403 the JSON API returns -- Coach must never reach the
    dashboard's private data at all, even transiently."""
    return templates.TemplateResponse(request, "scorer_dashboard.html", {})


@page_router.get("/scorer/dashboard", response_class=HTMLResponse)
def scorer_dashboard_alias_page(request: Request):
    return RedirectResponse("/scorer", status_code=307)
