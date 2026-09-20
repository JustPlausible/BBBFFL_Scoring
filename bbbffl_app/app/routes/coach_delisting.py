"""Coach self-service mid-season delisting workflow (issue #226).

Issue #164/#181 already built the authoritative mid-season delisting
domain (`app.midseason_draft.MidseasonDraftRepository.submit_delisting`/
`withdraw_delisting`) and its Scorer/Admin proxy surface
(`app.routes.midseason_draft`'s `/delisting`/`/delisting/{id}/withdraw`,
gated behind `midseason_draft.manage`). This module adds no new domain
behaviour: it is a thin, Coach-facing JSON/HTML surface over the exact
same repository calls, discoverable from the normal Coach Account page
(`/account`) instead of requiring a season id, the admin operations
route, or any UUID.

Authorization follows the same established "coach owns it, or a
delegated role is currently representing it" pattern as
`app.routes.shortlist`/`app.routes.midseason_draft.submit_pick`'s
`_authorise_pick`: `require_entry_context` -- never a client-supplied
`season_entry_id` trusted on its own -- 404s (enumeration-safe) any entry
a Coach does not own. The Scorer/Admin proxy path is untouched and
remains the exceptional/audited route for support cases; nothing here
weakens or duplicates its authority.
"""

import dataclasses

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import Principal, Role, require_capability, require_entry_context
from app.config import BASE_DIR
from app.draft_board import player_browse_view

router = APIRouter(prefix="/api/account/delisting")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

participate = require_capability("midseason_draft.participate")


class SubmitDelistingRequest(BaseModel):
    season_player_id: str
    reason: str | None = None


class WithdrawDelistingRequest(BaseModel):
    reason: str | None = None


def _actor(principal: Principal) -> ActorContext:
    # A genuine Coach self-service action is `actor_type="coach"`, never
    # `anonymous_operator` (see app/audit.py's module docstring and
    # app/routes/midseason_draft.py's own `_actor`) -- reserved for the
    # delegated-role proxy case, which never has an active role of "coach".
    if principal.role is Role.COACH and principal.coach_id is not None:
        return ActorContext.coach(principal.coach_id)
    return ActorContext(actor_type="anonymous_operator", actor_id=principal.coach_id, actor_role=principal.role.value)


def _status_view(request: Request, season_entry_id: str) -> dict:
    identities = request.app.state.identities
    midseason = request.app.state.midseason_draft
    team = identities.get_public_team(season_entry_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Private resource not found")
    draft = midseason.get_draft(team.season_id)
    squad = player_browse_view(
        request,
        team.season_id,
        draft_kind="midseason",
        availability="owned",
        owner_season_entry_id=season_entry_id,
    )
    squad.sort(key=lambda item: (item["display_name"].casefold(), str(item["canonical_player_id"])))
    delistings = (
        [
            dataclasses.asdict(item)
            for item in midseason.list_delistings(team.season_id, include_withdrawn=False)
            if item.season_entry_id == season_entry_id
        ]
        if draft is not None
        else []
    )
    delisted_player_ids = {item["season_player_id"] for item in delistings}
    for item in squad:
        item["delisted"] = item["season_player_id"] in delisted_player_ids
    return {
        "season_entry_id": season_entry_id,
        "team_name": team.team_name,
        "draft_state": draft.state if draft is not None else None,
        "is_open": draft is not None and draft.state == "delisting_open",
        "squad": squad,
        "delistings": delistings,
    }


@router.get("/{season_entry_id}/status")
def status(season_entry_id: str, request: Request, principal: Principal = Depends(participate)):
    require_entry_context(request, principal, season_entry_id)
    return _status_view(request, season_entry_id)


@router.post("/{season_entry_id}/submit")
def submit(
    season_entry_id: str,
    payload: SubmitDelistingRequest,
    request: Request,
    principal: Principal = Depends(participate),
):
    """Submit a delisting for a player the caller's own team currently
    owns. `season_entry_id` comes from the URL, but `require_entry_context`
    -- not this route -- decides whether the caller may act for it, so a
    Coach can never delist a player on another team's behalf by naming a
    different entry here; `MidseasonDraftRepository.submit_delisting`
    itself re-validates lifecycle state and current ownership before
    writing anything."""
    require_entry_context(request, principal, season_entry_id)
    team = request.app.state.identities.get_public_team(season_entry_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Private resource not found")
    request.app.state.midseason_draft.submit_delisting(
        team.season_id, season_entry_id, payload.season_player_id, actor=_actor(principal), reason=payload.reason
    )
    return _status_view(request, season_entry_id)


@router.post("/{season_entry_id}/{delisting_id}/withdraw")
def withdraw(
    season_entry_id: str,
    delisting_id: str,
    payload: WithdrawDelistingRequest,
    request: Request,
    principal: Principal = Depends(participate),
):
    """Withdraw (or, by resubmitting afterwards, change) the caller's own
    team's delisting. Ownership of `season_entry_id` is enforced by
    `require_entry_context` exactly as `submit` above; in addition, the
    named `delisting_id` must actually belong to that same entry -- a
    Coach naming another team's delisting id here 404s rather than ever
    reaching `withdraw_delisting`, which (like the Scorer/Admin proxy
    path) has no ownership check of its own since it is also the audited
    proxy's own call."""
    require_entry_context(request, principal, season_entry_id)
    team = request.app.state.identities.get_public_team(season_entry_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Private resource not found")
    midseason = request.app.state.midseason_draft
    delisting = midseason.get_delisting(delisting_id)
    if delisting is None or delisting.season_entry_id != season_entry_id:
        raise HTTPException(status_code=404, detail="Private resource not found")
    midseason.withdraw_delisting(team.season_id, delisting_id, actor=_actor(principal), reason=payload.reason)
    return _status_view(request, season_entry_id)


@page_router.get("/account/delisting", response_class=HTMLResponse)
def delisting_page(request: Request, principal: Principal = Depends(participate)):
    """The Coach Account entry point (issue #226): no season id or entry
    id in the URL -- resolved server-side from the authenticated coach
    identity via `coach_delisting_context`, exactly like `/account` itself
    resolves the signed-in coach from the session cookie. A coach with no
    currently-open delisting window (or no team at all) sees an explanatory
    empty state rather than a 404, since simply not having anything open
    right now is not an error. The `Role.COACH` guard mirrors `require_coach`:
    a delegated active role (Scorer/Admin/etc.) has its own proxy surface
    on the admin operations page and is redirected back to `/account`
    rather than resolving an unrelated coach identity here."""
    if principal.role is not Role.COACH or principal.coach_id is None:
        return RedirectResponse("/account", status_code=303)
    context = request.app.state.midseason_draft.coach_delisting_context(
        request.app.state.identities, principal.coach_id
    )
    return templates.TemplateResponse(
        request,
        "coach_delisting.html",
        {
            "season_entry_id": context["season_entry_id"] if context else None,
            "team_name": context["team_name"] if context else None,
        },
    )
