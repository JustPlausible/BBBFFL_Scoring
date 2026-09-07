"""Administrator Dashboard (issue #148) -- the role-aware governance/
readiness/navigation surface that answers "is the league/season correctly
configured and governed, what administrative issues need attention, and
where should the Administrator go next?".

This router is a thin HTTP translation over `app.admin_dashboard`'s read
model, the same shape as `app.routes.scorer_dashboard` over `app.
scorer_dashboard` (#147): every authorization decision happens here (never
inferred from a browser-supplied season/round identifier), and every
mutation the dashboard links to remains in its existing owning route
module -- nothing here writes anything.

## Role boundary

Unlike the Scorer Dashboard (Scorer/Replay-Operator/Administrator), this is
strictly the **Administrator's own role home** (issue #148): Coach,
Secretary, Scorer and Replay Operator must never reach Administrator-only
identity, authentication-provenance, audit or configuration information,
even where one of those roles also happens to hold Scorer capability --
that principal reaches Scorer information through `/scorer`, never through
here.

## Route choice: `/admin/dashboard`, not `/admin`

Issue #148 proposes `/admin` *or* `/admin/dashboard` and explicitly requires
resolving any conflict with the retained legacy Grand Final admin route
"explicitly ... do not silently merge legacy token authority with
session-native Administrator authority". `GET /admin` already exists
(`app.routes.admin.admin_page`): the legacy, `X-Admin-Token`-gated
Grand-Final/SuperScore scoring panel, predating coach authentication
entirely. Silently repurposing that path -- or redirecting it based on
which credential a request happens to carry -- would be exactly the kind
of silent merge the issue warns against. This dashboard therefore lives at
the deliberately distinct `GET /admin/dashboard`; `GET /admin` is
completely untouched. See docs/admin-dashboard.md.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.admin_dashboard import build_admin_dashboard, build_season_portfolio
from app.authorization import (
    SESSION_COOKIE_NAME,
    Principal,
    require_admin_principal,
    require_role_covers_season,
    resolve_principal,
)
from app.config import BASE_DIR
from app.csrf import issue_token

# Kept here, mirroring `app.routes.scorer_dashboard.SCORER_DASHBOARD_ROLE_
# PREFERENCE`, so `/account` can offer a role switch to whichever of these
# a coach identity actually holds without duplicating the authority
# decision itself. The Administrator Dashboard has exactly one qualifying
# role.
ADMIN_DASHBOARD_ROLE_PREFERENCE = ("admin",)

router = APIRouter(prefix="/api/admin/dashboard")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def require_admin_dashboard(principal: Principal = Depends(resolve_principal)) -> Principal:
    """Strict Administrator authority only -- see this module's docstring
    "Role boundary". `app.authorization.require_admin_principal` already
    encodes exactly this rule; this wrapper exists only so the dependency
    has a name specific to this dashboard, matching every other router's
    local `require_*` convention (see `app.routes.admin.require_admin`,
    `app.routes.scorer_dashboard.require_scorer_dashboard`)."""
    return require_admin_principal(principal)


def _has_valid_coach_session(request: Request) -> bool:
    """Whether the request's `bbbffl_session` cookie names a currently
    valid session (not merely present) -- mirrors `app.authorization.
    resolve_principal`'s own session/coach resolution exactly (Codex
    review, PR #160), since `resolve_principal` never performs this check
    itself once a legacy `X-Admin-Token` is also present (it returns
    before ever looking at the cookie -- see its own docstring). An
    expired, revoked or fabricated cookie must never be reported as a
    shadowed authenticated session."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return False
    state = request.app.state
    session = state.sessions.get_valid(token)
    if session is None:
        return False
    return state.identities.get_coach(session.coach_id) is not None


def _authentication_provenance(request: Request, principal: Principal) -> dict:
    """Issue #148's "warn clearly if a legacy shared token is taking
    precedence over the authenticated session ... never expose secrets or
    token values". `app.authorization.resolve_principal` resolves the
    legacy `X-Admin-Token` credential *before* ever looking at a coach
    session cookie (see its own docstring) -- so a request that carries
    both silently gets legacy-token authority with no signal that a real
    signed-in session was shadowed. This never reads or returns the token
    value itself, only whether one was present.

    The precedence warning is only ever raised for a *currently valid*
    session (Codex review, PR #160): an expired, revoked or fabricated
    `bbbffl_session` cookie alongside a valid token is not a shadowed
    session at all -- warning about it would tell the operator to remove
    the token, which would leave them unauthenticated."""
    legacy_token_active = principal.coach_id is None and principal.authenticated
    valid_session_present = _has_valid_coach_session(request)
    if legacy_token_active:
        provenance = "legacy_shared_token"
    elif principal.coach_id is not None:
        provenance = "authenticated_session"
    else:
        provenance = "unauthenticated"
    return {
        "provenance": provenance,
        "legacy_token_precedence_warning": legacy_token_active and valid_session_present,
    }


def _environment_view(settings) -> dict:
    return {"afl_mode": settings.afl_mode, "is_production": settings.is_production}


def _acting_context(principal: Principal) -> dict:
    return {
        "coach_id": principal.coach_id,
        "display_name": principal.display_name,
        "active_role": principal.role.value,
        "granted_roles": sorted(role.value for role in principal.granted_roles),
        "represented_season_entry_id": principal.represented_season_entry_id,
        "is_replay_context": principal.is_replay_context,
    }


@router.get("")
def get_dashboard(
    request: Request,
    season_id: str | None = None,
    round_id: str | None = None,
    principal: Principal = Depends(require_admin_dashboard),
):
    state = request.app.state
    # Administrator authority is never season-scoped
    # (`RoleGrantRepository.grant` refuses to create a season-scoped
    # "admin" grant -- see docs/acting-context.md), so every season is
    # always in scope here; `require_role_covers_season` below is still
    # called for an explicit `season_id` purely as defence in depth
    # (issue #148's "browser-supplied season identifiers never confer
    # authority") -- it always passes for `Role.ADMIN`, but a future
    # change to that rule must not silently widen this route.
    portfolio = build_season_portfolio(
        state.seasons, state.identities, state.draft, state.preseason, state.fixtures, state.database
    )
    resolved_season_id = season_id
    if resolved_season_id is not None:
        require_role_covers_season(request, principal, resolved_season_id)
        if state.seasons.get_season(resolved_season_id) is None:
            raise HTTPException(status_code=404, detail="Unknown season")
    elif portfolio:
        resolved_season_id = portfolio[0]["season_id"]

    dashboard = None
    if resolved_season_id is not None:
        dashboard = build_admin_dashboard(
            state.database,
            state.seasons,
            state.identities,
            state.draft,
            state.preseason,
            state.player_pool,
            state.lifecycle,
            state.fixtures,
            state.round_review,
            state.audit_events,
            state.role_grants,
            state.afl_client,
            resolved_season_id,
            round_id=round_id,
        )
    return {
        "acting_context": _acting_context(principal),
        "authentication": {
            **_authentication_provenance(request, principal),
            "environment": _environment_view(state.settings),
        },
        "portfolio": portfolio,
        "selected_season_id": resolved_season_id,
        "dashboard": dashboard,
    }


@page_router.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard_page(request: Request):
    """The discoverable Administrator role home (issue #148): an
    authenticated visitor with no active Administrator role sees a clear
    authorization message here rather than the raw 403 the JSON API
    returns -- Coach/Secretary/Scorer/Replay-Operator must never reach the
    dashboard's private data at all, even transiently.

    Issues a CSRF double-submit token/cookie unconditionally (mirroring
    `app.routes.scorer_dashboard.scorer_home_page`) so the page's own
    client-side "switch to Administrator" recovery can call the existing
    CSRF-protected `POST /api/context/role` without a separate page visit
    -- a freshly authenticated session's active role is always "coach"
    even when Administrator authority is granted (see
    `app.auth.ActingContextService`), so the advertised
    `/admin/dashboard` link would otherwise 403 until switched elsewhere."""
    token = issue_token(request.app.state.settings.session_secret)
    response = templates.TemplateResponse(
        request,
        "admin_dashboard.html",
        {"csrf_token": token, "admin_dashboard_roles": list(ADMIN_DASHBOARD_ROLE_PREFERENCE)},
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
