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

from app.authorization import (
    Principal,
    Role,
    principal_has_capability,
    require_authenticated,
    require_role_covers_season,
    resolve_principal,
)
from app.config import BASE_DIR
from app.csrf import issue_token
from app.scorer_dashboard import build_scorer_dashboard

# The active roles that can reach this dashboard at all (issue #147) --
# `require_scorer_dashboard` below is the enforced version of this same
# set; kept here too so the account page / dashboard shell can offer a
# role switch to whichever of these a coach identity actually holds,
# without duplicating the authority decision itself.
SCORER_DASHBOARD_ROLE_PREFERENCE = ("scorer", "replay_operator", "admin")

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


def _annotate_actionability(dashboard: dict, principal: Principal) -> None:
    """Mark every next-action/attention item with whether the *current
    active role* actually holds the capability it needs (issue #147, Codex
    review on PR #159): a Replay Operator, for instance, is admitted to
    this dashboard but does not hold `roundsetup.manage`, so a next action
    that links to Round Preflight must say so rather than advertise a link
    that will 403. This never changes which workflow is linked -- only
    whether it is presented as something *this* principal can act on."""

    def actionable(capability: str | None) -> bool:
        return capability is None or principal_has_capability(principal, capability)

    next_action = dashboard.get("next_action")
    if next_action is not None:
        next_action["actionable_by_you"] = actionable(next_action.get("capability"))
    for item in dashboard.get("attention", ()):
        item["actionable_by_you"] = actionable(item.get("capability"))


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
    _annotate_actionability(dashboard, principal)
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
    dashboard's private data at all, even transiently.

    Issues a CSRF double-submit token/cookie unconditionally (like every
    other coach-session-authenticated page shell in this codebase) so the
    page's own client-side "switch to a qualifying role" recovery (Codex
    review on PR #159: a freshly authenticated session always starts
    active-role `coach` even when granted Scorer/Replay-Operator/
    Administrator authority, so the advertised `/scorer` link would
    otherwise 403 until switched elsewhere) can call the existing,
    CSRF-protected `POST /api/context/role` without a separate page visit.
    """
    token = issue_token(request.app.state.settings.session_secret)
    response = templates.TemplateResponse(
        request,
        "scorer_dashboard.html",
        {"csrf_token": token, "scorer_dashboard_roles": list(SCORER_DASHBOARD_ROLE_PREFERENCE)},
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


@page_router.get("/scorer/dashboard", response_class=HTMLResponse)
def scorer_dashboard_alias_page(request: Request):
    return RedirectResponse("/scorer", status_code=307)
