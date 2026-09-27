"""Administrator browser workflow for coach credential provisioning/reset
(issue #238, closing readiness item 2 in
docs/2027-live-season-readiness.md).

Before this page existed, the only wired operation was
`POST /api/admin/coach-credential` (`app/routes/admin.py`) -- a JSON API
with no browser form, so onboarding every coach before their first login
required a script or `curl` invocation and (worse) typing/copying a raw
`coach_id` UUID. This module adds the missing browser surface, reusing the
same underlying service the JSON API already calls
(`app.auth.AuthenticationService.reset_password`) rather than inventing a
second credential-mutation path -- see `_submit` below.

## Authorization

Gated by `require_admin_principal`, the exact same authority the JSON
endpoint already requires (`app.routes.admin.require_admin`, which also
resolves through `app.authorization.resolve_principal` -- so a real,
session-authenticated Administrator and the legacy shared `X-Admin-Token`
both already work here, unchanged). The current capability model
(`app.authorization.CAPABILITIES`) grants no credential-management
capability to Scorer, and widening that is an authorization design change
this issue does not ask for -- see `tests/test_coach_credentials_api.py`'s
coverage of a Scorer principal being rejected the same way an unauthenticated
request is.

## Coach/team selection

`_coach_roster` below is a read-only presentation composition over
`app.identity.IdentityRepository.list_coaches`/`list_entries` and
`app.auth.CredentialRepository.has_credential` -- the same repositories
`app.admin_dashboard`/`app.season_centre` already read from, never a new
identity or credential model. Every coach is offered by display name (and,
where resolvable, current team/season), never a bare `coach_id`: the
`coach_id` only ever appears as a `<select>` option's value, so an operator
never types or copies one.

## Provenance

`_actor` mirrors the same pattern already used by
`app.routes.season_centre`/`app.routes.finals_preflight` for every other
authenticated-session administrative action: `actor_type` stays
`"anonymous_operator"` (app/audit.py's established convention -- a
delegated administrative action is never attributed to the `coach` actor
type), but `actor_id` carries the authenticated operator's own `coach_id`
when this principal was resolved from a real coach session, so the audit
trail records *which* operator performed the reset wherever that is known
(`None` for the legacy shared `X-Admin-Token`, which has no per-operator
identity).
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.audit import ActorContext
from app.auth import MIN_PASSWORD_LENGTH, WeakCredentialError
from app.authorization import Principal, require_admin_principal, resolve_principal
from app.config import BASE_DIR
from app.routes.auth import _attach_csrf_cookie, _issue_csrf_token, _parse_form, _verify_csrf

page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

_NOTICES = {
    "provisioned": "Password set. Share the new password with the coach out of band -- it is never shown again here.",
    "reset": (
        "Password reset. The coach's previous sessions have all been signed out, and the new password must be "
        "shared with them out of band -- it is never shown again here."
    ),
}


def require_admin_credentials(principal: Principal = Depends(resolve_principal)) -> Principal:
    """Route-level `Depends`-ready wrapper, matching `app.routes.admin`'s/
    `app.routes.admin_dashboard`'s established local-wrapper convention."""
    return require_admin_principal(principal)


def _actor(principal: Principal) -> ActorContext:
    return ActorContext(actor_type="anonymous_operator", actor_id=principal.coach_id, actor_role=principal.role.value)


def _coach_roster(request: Request) -> list[dict]:
    """Every coach identity, human-labelled with current team/season where
    one exists, and whether a credential already exists -- see module
    docstring. Never includes email/phone beyond the coach's own on-file
    email (already visible to any Administrator via the identity
    repository), and never a password hash or password material."""
    identities = request.app.state.identities
    credentials = request.app.state.credentials
    teams_by_coach: dict[str, list[str]] = {}
    for season in request.app.state.seasons.list_seasons():
        for entry in identities.list_entries(season.season_id):
            teams_by_coach.setdefault(entry.coach_id, []).append(f"{entry.team_name} ({season.label})")
    roster = []
    for coach in identities.list_coaches():
        roster.append(
            {
                "coach_id": coach.coach_id,
                "display_name": coach.display_name,
                "email": coach.email,
                "teams": teams_by_coach.get(coach.coach_id, []),
                "has_credential": credentials.has_credential(coach.coach_id),
            }
        )
    return roster


def _render(
    request: Request,
    *,
    error: str | None = None,
    selected_coach_id: str = "",
    reason: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    token = _issue_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "coach_credentials.html",
        {
            "roster": _coach_roster(request),
            "error": error,
            "notice": _NOTICES.get(request.query_params.get("notice")),
            "selected_coach_id": selected_coach_id,
            "reason": reason,
            "min_password_length": MIN_PASSWORD_LENGTH,
            "csrf_token": token,
        },
        status_code=status_code,
    )
    _attach_csrf_cookie(request, response, token)
    return response


@page_router.get("/admin/coach-credentials", response_class=HTMLResponse)
def coach_credentials_page(request: Request, principal: Principal = Depends(require_admin_credentials)):
    return _render(request)


@page_router.post("/admin/coach-credentials", response_class=HTMLResponse)
async def coach_credentials_submit(request: Request, principal: Principal = Depends(require_admin_credentials)):
    form = await _parse_form(request)
    coach_id = form.get("coach_id", "").strip()
    new_password = form.get("new_password", "")
    confirm_password = form.get("confirm_password", "")
    reason = form.get("reason", "").strip()
    csrf_submitted = form.get("csrf_token", "")

    if not _verify_csrf(request, csrf_submitted):
        return _render(
            request,
            error="Your form expired. Please try again.",
            selected_coach_id=coach_id,
            reason=reason,
            status_code=403,
        )

    coach = request.app.state.identities.get_coach(coach_id) if coach_id else None
    if coach is None:
        return _render(request, error="Choose a coach from the list.", status_code=400)

    if new_password != confirm_password:
        return _render(
            request,
            error="Password and confirmation do not match.",
            selected_coach_id=coach_id,
            reason=reason,
            status_code=400,
        )

    was_provisioned = request.app.state.credentials.has_credential(coach_id)
    try:
        request.app.state.auth_service.reset_password(
            coach_id, new_password, actor=_actor(principal), reason=reason or None
        )
    except WeakCredentialError as exc:
        return _render(request, error=str(exc), selected_coach_id=coach_id, reason=reason, status_code=400)

    notice = "reset" if was_provisioned else "provisioned"
    return RedirectResponse(f"/admin/coach-credentials?notice={notice}", status_code=303)
