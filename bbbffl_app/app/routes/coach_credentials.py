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
second credential-mutation *path* -- this module's own `/api/admin/
coach-credentials` (plural) JSON endpoint exists only because the browser
form needs things the legacy endpoint deliberately does not (a
`confirm_password` field, CSRF protection); both endpoints still resolve
to the one service call.

## Page shell vs. JSON API (Codex review, PR #252, P1)

`GET /admin/coach-credentials` renders an **unauthenticated** page shell --
no coach data, no principal check -- exactly like every other admin
browser page reachable by the legacy shared `X-Admin-Token`
(`app.routes.season_centre.season_centre_index_page`, `app.routes.
admin.admin_page`, `app.routes.round_preflight`, ...): a normal HTML page
load cannot attach a custom header, so a page gated on `require_admin_
principal` at the HTTP level would be unreachable for an operator whose
only authority is the legacy token (exactly the production-bootstrap case
this issue exists to unblock, before any coach has ever been granted the
Administrator role). Instead, the page's own JS calls `GET`/`POST
/api/admin/coach-credentials` -- gated by `require_admin_credentials` --
which accepts authority the same way `app.routes.season_centre`'s API does:
a same-origin coach-session cookie sent automatically, *or* an
`X-Admin-Token` header the page's JS attaches from `localStorage` (the
same `bbbffl_admin_token` key/`token-bar` UI every other legacy-token
admin page already uses).

## Authorization

Gated by `require_admin_principal`, the exact same authority the JSON
endpoint already requires (`app.routes.admin.require_admin`, which also
resolves through `app.authorization.resolve_principal`). The current
capability model (`app.authorization.CAPABILITIES`) grants no credential-
management capability to Scorer, and widening that is an authorization
design change this issue does not ask for -- see
`tests/test_coach_credentials_api.py`'s coverage of a Scorer principal
being rejected the same way an unauthenticated request is.

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

## Self-reset (Codex review, PR #252, P2)

`AuthenticationService.reset_password` revokes every currently-valid
session for the affected coach -- including, if an Administrator resets
their *own* credential, the very session cookie authenticating this
request. That is harmless here specifically because the JSON API returns
its response synchronously from the already-authorized request (FastAPI
never re-checks authorization to *send* a response it already computed);
the page's JS then updates the same page in place rather than triggering a
fresh navigation to a `require_admin_principal`-gated URL, so there is no
follow-up request for the just-revoked session to fail.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.auth import MIN_PASSWORD_LENGTH
from app.authorization import Principal, require_admin_principal, resolve_principal
from app.config import BASE_DIR
from app.routes.auth import _attach_csrf_cookie, _issue_csrf_token, _verify_csrf

router = APIRouter(prefix="/api/admin/coach-credentials")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


class SetCoachCredentialRequest(BaseModel):
    coach_id: str
    new_password: str
    confirm_password: str
    reason: str | None = None


def require_admin_credentials(principal: Principal = Depends(resolve_principal)) -> Principal:
    """Route-level `Depends`-ready wrapper, matching `app.routes.admin`'s/
    `app.routes.admin_dashboard`'s established local-wrapper convention."""
    return require_admin_principal(principal)


def _actor(principal: Principal) -> ActorContext:
    return ActorContext(actor_type="anonymous_operator", actor_id=principal.coach_id, actor_role=principal.role.value)


def _require_csrf(request: Request, csrf_token: str | None) -> None:
    if not _verify_csrf(request, csrf_token or ""):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


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


@router.get("", dependencies=[Depends(require_admin_credentials)])
def get_coach_roster(request: Request) -> dict:
    return {"roster": _coach_roster(request)}


@router.post("", dependencies=[Depends(require_admin_credentials)])
def set_coach_credential(
    payload: SetCoachCredentialRequest, request: Request, principal: Principal = Depends(require_admin_credentials)
) -> dict:
    _require_csrf(request, request.headers.get("X-CSRF-Token"))

    coach = request.app.state.identities.get_coach(payload.coach_id)
    if coach is None:
        raise HTTPException(status_code=404, detail="Unknown coach")
    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="Password and confirmation do not match")

    was_provisioned = request.app.state.credentials.has_credential(payload.coach_id)
    request.app.state.auth_service.reset_password(
        payload.coach_id, payload.new_password, actor=_actor(principal), reason=payload.reason
    )
    return {"coach_id": payload.coach_id, "status": "reset" if was_provisioned else "provisioned"}


@page_router.get("/admin/coach-credentials", response_class=HTMLResponse)
def coach_credentials_page(request: Request):
    """Deliberately unauthenticated at the page-shell level -- see module
    docstring, "Page shell vs. JSON API". Never embeds coach data
    server-side; the page's own JS fetches it from the CSRF/authorization-
    protected JSON API above."""
    token = _issue_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "coach_credentials.html",
        {"csrf_token": token, "min_password_length": MIN_PASSWORD_LENGTH},
    )
    _attach_csrf_cookie(request, response, token)
    return response
