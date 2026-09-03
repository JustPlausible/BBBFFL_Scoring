"""Secretary/League Manager (or Administrator) Season Centre (issue #100) --
an operator surface over the season-model domain established by roadmap
packages 09-10 (issues #19/#20): season identity/lifecycle, private coach
records, and public season-entry team identity. It is the human replay
operator's way to establish and inspect recognisable BBBFFL season state
(real coach names, real BBBFFL team names) before draft/fixture/round
setup, without SQL or direct database manipulation.

Every rule -- non-empty names, valid coach/season references, licence
uniqueness -- lives in `app.season_centre` (called through its thin
read-model/command wrappers, which in turn call `app.identity`/`app.season`).
Nothing here decides any of that. Like `app.routes.round_review`, this module
imports `app.season_centre` directly (see `tests/test_architecture.py`'s
`SEASON_CENTRE` group) rather than reaching into `app.identity`/`app.season`
itself, and every read rebuilds its response from the database through the
already-constructed repositories on `request.app.state`.

This module deliberately does not implement draft, fixture-generation, round
-configuration or weekly scoring/selection behaviour -- see `app.routes.
draft` and `app.routes.round_review` for those; this router only links to
their existing pages once their own state says they are reachable.

## Retrofit for roadmap package #107 (issue #107)

Season Centre is ordinary league-season setup -- season entries, team
names, coach records -- exactly the kind of operation issue #107 says must
not require blanket Administrator authority. Every endpoint below now
requires `require_secretary_or_admin` (Secretary/League Manager authority
or Administrator, either satisfies it) rather than strict Administrator
authority alone: an authorised Secretary can run this page without also
being granted Administrator. `require_secretary_or_admin` accepts a
principal resolved from either the legacy shared `X-Admin-Token` (an
ambient credential, still narrowable to `scorer`, unaffected by this
package -- but note it can never satisfy this dependency, since a bare
`X-Admin-Token` resolves to `admin`/`scorer`, both handled independently
by `Role`, not `secretary`, unless narrowed to `admin`) or an authenticated
coach session whose active role (`app.authorization.Principal.role`) has
been switched to Secretary/Administrator via `app.routes.context` -- see
docs/acting-context.md. The page shell (`season_centre_index_page`/
`season_centre_page`) now also issues the double-submit CSRF cookie/token
(`app.routes.auth._issue_csrf_token`/`_attach_csrf_cookie`) so its JS can
call the CSRF-protected `/api/context/*` endpoints when rendering the
context bar -- its own admin-token-authenticated endpoints are unaffected
(a custom header, not a cookie, is what authorises them).
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import (
    Principal,
    principal_has_capability,
    require_role_covers_season,
    require_secretary_or_admin,
    resolve_principal,
)
from app.config import BASE_DIR
from app.routes.auth import _attach_csrf_cookie, _issue_csrf_token
from app.season_centre import build_season_centre, list_coaches_overview, list_seasons_overview
from app.season_centre import create_coach as create_coach_service
from app.season_centre import create_entry as create_entry_service
from app.season_centre import create_season as create_season_service
from app.season_centre import rename_team as rename_team_service
from app.season_centre import transfer_entry as transfer_entry_service
from app.season_centre import update_coach as update_coach_service

router = APIRouter(prefix="/api/admin/season-centre")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def _season_view(state, season_id: str, principal: Principal):
    view = build_season_centre(
        state.seasons,
        state.identities,
        state.draft,
        state.preseason,
        state.player_pool,
        state.lifecycle,
        season_id,
        state.database,
    )
    if not principal_has_capability(principal, "draft.participate"):
        view["links"]["draft"] = None
    # Opening Round Operations requires `opening_round.nominate` (Scorer,
    # Replay Operator, or Admin) -- a Secretary lacks it, so hide the link
    # rather than send them to a page whose data request would 403 (issue
    # #131 PR review finding), mirroring the `draft` filtering above.
    if not principal_has_capability(principal, "opening_round.nominate"):
        view["links"]["opening_round"] = None
    return view


class CreateSeasonRequest(BaseModel):
    year: int
    label: str
    regular_season_round_count: int = 20


class CreateCoachRequest(BaseModel):
    display_name: str
    email: str | None = None
    phone: str | None = None
    profile_notes: str | None = None


class UpdateCoachRequest(BaseModel):
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    profile_notes: str | None = None
    reason: str | None = None


class CreateEntryRequest(BaseModel):
    coach_id: str
    team_name: str
    licence_key: str | None = None
    reason: str | None = None


class RenameTeamRequest(BaseModel):
    team_name: str
    reason: str | None = None


class TransferCoachRequest(BaseModel):
    coach_id: str
    reason: str | None = None


def require_secretary(principal: Principal = Depends(resolve_principal)) -> Principal:
    """Route-level `Depends`-ready wrapper, matching `app.routes.admin`'s
    established local-wrapper convention for its own `require_admin`/
    `require_scorer`."""
    return require_secretary_or_admin(principal)


def _actor(principal: Principal) -> ActorContext:
    """Provenance authority comes from the resolved credential -- the same
    shape as `app.routes.round_review`'s `_actor` helper, extended for
    roadmap package #107 (issue #107): `actor_id` now carries the
    authenticated operator's `coach_id` when this principal was resolved
    from a real coach session (Secretary/Administrator acting under their
    own login), so the audit trail distinguishes *which* operator performed
    a Season Centre change -- never the affected coach/season entry, and
    never anything beyond what `ActorContext.anonymous_operator` already
    permits (`actor_type` stays `"anonymous_operator"`; see app/audit.py's
    module docstring, "Actor convention"). Remains `None` for the legacy
    shared `X-Admin-Token` credential, which has no per-operator identity to
    attribute."""
    return ActorContext(actor_type="anonymous_operator", actor_id=principal.coach_id, actor_role=principal.role.value)


@router.get("/seasons", dependencies=[Depends(require_secretary)])
def list_seasons(request: Request):
    return list_seasons_overview(request.app.state.seasons)


@router.post("/seasons", dependencies=[Depends(require_secretary)])
def create_season(payload: CreateSeasonRequest, request: Request):
    return create_season_service(
        request.app.state.seasons,
        payload.year,
        payload.label,
        regular_season_round_count=payload.regular_season_round_count,
    )


@router.get("/coaches", dependencies=[Depends(require_secretary)])
def list_coaches(request: Request):
    return list_coaches_overview(request.app.state.identities)


@router.post("/coaches", dependencies=[Depends(require_secretary)])
def create_coach(payload: CreateCoachRequest, request: Request):
    return create_coach_service(
        request.app.state.identities,
        payload.display_name,
        email=payload.email,
        phone=payload.phone,
        profile_notes=payload.profile_notes,
    )


@router.post("/coaches/{coach_id}", dependencies=[Depends(require_secretary)])
def update_coach(
    coach_id: str, payload: UpdateCoachRequest, request: Request, principal: Principal = Depends(require_secretary)
):
    # Forward only the fields the caller actually sent -- an omitted field
    # must leave that value unchanged, while an explicit `null` in the
    # request body must clear it. `payload.model_fields_set` is how these
    # are told apart; passing every field unconditionally (including
    # unset ones, which pydantic defaults to `None`) would make every
    # omitted field look like an explicit clear. See
    # `app.season_centre.update_coach`'s docstring and `app.identity.UNSET`.
    fields = {
        field: getattr(payload, field)
        for field in ("display_name", "email", "phone", "profile_notes")
        if field in payload.model_fields_set
    }
    return update_coach_service(
        request.app.state.identities,
        coach_id,
        actor=_actor(principal),
        reason=payload.reason,
        **fields,
    )


@router.get("/{season_id}", dependencies=[Depends(require_secretary)])
def season_centre(season_id: str, request: Request, principal: Principal = Depends(require_secretary)):
    require_role_covers_season(request, principal, season_id)
    state = request.app.state
    return _season_view(state, season_id, principal)


@router.post("/{season_id}/entries", dependencies=[Depends(require_secretary)])
def create_entry(
    season_id: str, payload: CreateEntryRequest, request: Request, principal: Principal = Depends(require_secretary)
):
    require_role_covers_season(request, principal, season_id)
    create_entry_service(
        request.app.state.identities,
        season_id,
        payload.coach_id,
        payload.team_name,
        licence_key=payload.licence_key,
        actor=_actor(principal),
        reason=payload.reason,
    )
    state = request.app.state
    return _season_view(state, season_id, principal)


def _require_entry_season_covered(request: Request, principal: Principal, entry_id: str):
    """Resolve `entry_id`'s season *before* mutating it, so a season-scoped
    Secretary/Scorer grant (e.g. one confined to the 2026 replay season)
    can be checked against the entry's real season -- never trusted from
    the client, and never deferred until after the write already
    happened. 404 for an unknown entry, matching this router's existing
    not-found convention (see `app.authorization.require_owned_season_entry`)."""
    entry = request.app.state.identities.get_public_team(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown season entry")
    require_role_covers_season(request, principal, entry.season_id)
    return entry


@router.post("/entries/{entry_id}/team-name", dependencies=[Depends(require_secretary)])
def rename_team(
    entry_id: str, payload: RenameTeamRequest, request: Request, principal: Principal = Depends(require_secretary)
):
    entry = _require_entry_season_covered(request, principal, entry_id)
    state = request.app.state
    rename_team_service(state.identities, entry_id, payload.team_name, actor=_actor(principal), reason=payload.reason)
    return _season_view(state, entry.season_id, principal)


@router.post("/entries/{entry_id}/coach", dependencies=[Depends(require_secretary)])
def transfer_entry(
    entry_id: str, payload: TransferCoachRequest, request: Request, principal: Principal = Depends(require_secretary)
):
    entry = _require_entry_season_covered(request, principal, entry_id)
    state = request.app.state
    transfer_entry_service(state.identities, entry_id, payload.coach_id, actor=_actor(principal), reason=payload.reason)
    return _season_view(state, entry.season_id, principal)


def _render_page(request: Request, season_id: str | None) -> HTMLResponse:
    # Issues the double-submit CSRF cookie/token for this admin-token-
    # authenticated shell too (roadmap package #107, issue #107): the shell
    # itself stays reachable without a header/cookie (see app/authorization
    # -and-privacy.md, "non-sensitive token-entry/application shells"), but
    # its embedded context bar now also calls the CSRF-protected
    # `/api/context/*` endpoints when the visitor is a signed-in coach
    # identity, not just the admin-token flow those endpoints never use.
    token = _issue_csrf_token(request)
    response = templates.TemplateResponse(request, "season_centre.html", {"season_id": season_id, "csrf_token": token})
    _attach_csrf_cookie(request, response, token)
    return response


@page_router.get("/admin/season-centre", response_class=HTMLResponse)
def season_centre_index_page(request: Request):
    return _render_page(request, None)


@page_router.get("/admin/season-centre/{season_id}", response_class=HTMLResponse)
def season_centre_page(season_id: str, request: Request):
    return _render_page(request, season_id)
