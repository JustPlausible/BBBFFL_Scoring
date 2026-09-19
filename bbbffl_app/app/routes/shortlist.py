"""Private coach draft shortlist/planning surface (issue #181).

A thin JSON operator surface over `app.shortlist.ShortlistRepository` --
this module owns exactly one thing beyond a direct translation to a
repository call: authorization. Every route requires the caller to be
acting for the named `season_entry_id`
(`app.authorization.require_entry_context`) -- the same "coach owns it, or
a delegated Scorer/Admin is currently representing it" rule every other
proxy-capable BBBFFL surface uses (draft picks, lineup proxy submission).
There is no separate "read-only" relaxation: a shortlist is private
planning data, so *reading* another team's shortlist requires exactly the
same authorization as mutating it, and an unauthorised request 404s
(`require_entry_context`'s enumeration-safe convention) rather than 403,
never revealing whether a shortlist exists for a foreign entry.

Never a source of draft authority: nothing here reserves a player, and no
draft repository consults this module's data.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import Principal, Role, require_capability, require_entry_context
from app.config import BASE_DIR
from app.draft_board import player_browse_view

router = APIRouter(prefix="/api/shortlist")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

manage = require_capability("shortlist.manage")


class AddPlayerRequest(BaseModel):
    season_player_id: str
    reason: str | None = None


class RemovePlayerRequest(BaseModel):
    season_player_id: str
    reason: str | None = None


class ReorderRequest(BaseModel):
    ordered_season_player_ids: list[str]
    reason: str | None = None


def _actor(principal: Principal) -> ActorContext:
    # Codex review, PR #225 (P2): a coach managing their own shortlist is a
    # genuine self-action -- `actor_type="coach"` (`ActorContext.coach`),
    # never `anonymous_operator`, which is reserved for the shared-token/
    # delegated-role proxy surface (see app/audit.py's module docstring).
    # A Scorer/Admin supporting a coach's shortlist is still the delegated
    # proxy case (its own active role is never "coach").
    if principal.role is Role.COACH and principal.coach_id is not None:
        return ActorContext.coach(principal.coach_id)
    return ActorContext(actor_type="anonymous_operator", actor_id=principal.coach_id, actor_role=principal.role.value)


def _item_view(request: Request, item, suggestion_id: str | None) -> dict:
    player = request.app.state.player_pool.get_by_id(item.season_player_id)
    owner_row = request.app.state.database.execute(
        "SELECT season_entry_id FROM player_ownership_period WHERE season_player_id=? AND released_at IS NULL",
        (item.season_player_id,),
    ).fetchone()
    owner_team_name = None
    if owner_row is not None:
        team = request.app.state.identities.get_public_team(owner_row["season_entry_id"])
        owner_team_name = team.team_name if team else "Unknown team"
    return {
        "shortlist_item_id": item.shortlist_item_id,
        "season_player_id": item.season_player_id,
        "rank": item.rank,
        "display_name": player.display_name if player else "Unknown player",
        "afl_team_name": player.afl_team_name if player else None,
        "available": owner_row is None,
        "owner_team_name": owner_team_name,
        "is_suggestion": suggestion_id is not None and item.shortlist_item_id == suggestion_id,
    }


def _view(request: Request, season_entry_id: str) -> dict:
    shortlist = request.app.state.shortlist
    items = shortlist.list_items(season_entry_id)
    suggestion = shortlist.suggestion(season_entry_id)
    return {
        "season_entry_id": season_entry_id,
        "items": [_item_view(request, item, suggestion.shortlist_item_id if suggestion else None) for item in items],
        "suggestion": _item_view(request, suggestion, suggestion.shortlist_item_id) if suggestion else None,
    }


@router.get("/{season_entry_id}")
def get_shortlist(season_entry_id: str, request: Request, principal: Principal = Depends(manage)):
    require_entry_context(request, principal, season_entry_id)
    return _view(request, season_entry_id)


@router.post("/{season_entry_id}/add")
def add_player(
    season_entry_id: str, payload: AddPlayerRequest, request: Request, principal: Principal = Depends(manage)
):
    require_entry_context(request, principal, season_entry_id)
    request.app.state.shortlist.add_player(
        season_entry_id, payload.season_player_id, actor=_actor(principal), reason=payload.reason
    )
    return _view(request, season_entry_id)


@router.post("/{season_entry_id}/remove")
def remove_player(
    season_entry_id: str, payload: RemovePlayerRequest, request: Request, principal: Principal = Depends(manage)
):
    require_entry_context(request, principal, season_entry_id)
    request.app.state.shortlist.remove_player(
        season_entry_id, payload.season_player_id, actor=_actor(principal), reason=payload.reason
    )
    return _view(request, season_entry_id)


@router.post("/{season_entry_id}/reorder")
def reorder(season_entry_id: str, payload: ReorderRequest, request: Request, principal: Principal = Depends(manage)):
    require_entry_context(request, principal, season_entry_id)
    request.app.state.shortlist.reorder(
        season_entry_id, payload.ordered_season_player_ids, actor=_actor(principal), reason=payload.reason
    )
    return _view(request, season_entry_id)


@router.get("/{season_entry_id}/players")
def shortlist_players(
    season_entry_id: str,
    request: Request,
    q: str | None = None,
    availability: str | None = None,
    limit: int = 100,
    principal: Principal = Depends(manage),
):
    """The same private-entry-context authorization as the rest of this
    module, over the shared player browser (`app.draft_board.
    player_browse_view`) -- lets the shortlist page search for a player to
    add without duplicating `app.player_pool.PlayerPoolRepository.browse`'s
    presentation. `draft_kind` follows whichever draft is actually running
    for this team's season (mid-season once one exists, preseason
    otherwise), so the scoring context shown always matches the phase the
    shortlist is genuinely being used for."""
    require_entry_context(request, principal, season_entry_id)
    team = request.app.state.identities.get_public_team(season_entry_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Private resource not found")
    availability = availability or None
    if availability not in (None, "available", "owned", "unresolved"):
        raise HTTPException(status_code=400, detail="availability must be available, owned, or unresolved")
    draft_kind = "midseason" if request.app.state.midseason_draft.get_draft(team.season_id) else "preseason"
    return player_browse_view(
        request, team.season_id, draft_kind=draft_kind, query=q, availability=availability, limit=min(limit, 300)
    )


@router.get("/{season_entry_id}/suggestion")
def suggestion(season_entry_id: str, request: Request, principal: Principal = Depends(manage)):
    require_entry_context(request, principal, season_entry_id)
    item = request.app.state.shortlist.suggestion(season_entry_id)
    return {"suggestion": _item_view(request, item, item.shortlist_item_id) if item else None}


@page_router.get("/shortlist/{season_entry_id}", response_class=HTMLResponse)
def shortlist_page(season_entry_id: str, request: Request, principal: Principal = Depends(manage)):
    require_entry_context(request, principal, season_entry_id)
    return templates.TemplateResponse(request, "shortlist.html", {"season_entry_id": season_entry_id})
