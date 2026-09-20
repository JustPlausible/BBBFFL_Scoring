"""Scorer-operated preseason draft workflow (roadmap package 14, issue #53).

An operator surface over `app.draft`'s authoritative `DraftRepository` --
turn sequencing, pick-owner resolution, player eligibility/availability,
squad-capacity validation and concurrency control all happen in that
repository/service layer inside one database transaction per pick. Nothing
in this module or in `templates/draft.html`'s JavaScript decides whose turn
it is, which players are available, or whether a pick is valid: every
mutating endpoint here is a thin translation from an HTTP request to one
`DraftRepository`/`PlayerPoolRepository` call, and every read rebuilds its
response from the database on every request -- a browser reload always
reflects authoritative persisted state, never reconstructed client state.

Authenticated operators use issue #107's active role and represented-entry
context; the represented season entry is the domain target (`execute_pick`'s
owner-of-the-pick identity), never the operator. The audit actor for such a
pick stays `anonymous_operator` -- the same delegated-write convention every
other proxy/domain write in this module uses -- with the authenticated
operator's stable `coach_id` carried in `actor_id` purely as provenance, and
the active delegated role in `actor_role`. The older shared-token API may
still supply `PickRequest.scorer_name` as its transitional audit label; it is
never an entry or ownership identifier.

Reopening a finalized draft is deliberately not an ordinary one-click
control (see `DraftRepository.reopen`'s docstring): `/reopen` requires the
caller to echo a literal confirmation phrase back, which `draft.html`
exposes only behind a separate "danger zone" section, not the normal
finalise button.
"""

import dataclasses

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import (
    Principal,
    Role,
    require_capability,
    require_coach,
    require_entry_context,
    require_role_covers_season,
)
from app.config import BASE_DIR
from app.draft_board import build_board, build_readiness, player_browse_view, resolve_my_entry_id
from app.routes.admin import require_admin

router = APIRouter(prefix="/api/admin/draft")
# Issue #229: the Coach-facing self-service surface at
# `/account/preseason-draft/{season_id}`, mirroring `app.routes.
# midseason_draft`'s `coach_router` -- its own endpoints require an active
# Coach role and an entry in the requested season; the `/api/admin/...` and
# `/admin/...` surfaces above require `draft.participate` throughout and
# remain reachable by a delegated operator (Scorer/Secretary/Admin/Replay
# Operator) representing an entry, not just a genuine Coach.
coach_router = APIRouter(prefix="/api/account/preseason-draft")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

REOPEN_CONFIRMATION_PHRASE = "REOPEN FINALIZED DRAFT"

participate = require_capability("draft.participate")


def coach_participant(principal: Principal = Depends(participate)) -> Principal:
    return require_coach(principal)


class PickRequest(BaseModel):
    season_entry_id: str
    season_player_id: str
    draft_pick_id: str | None = None
    scorer_name: str | None = None
    reason: str | None = None


class PauseRequest(BaseModel):
    reason: str | None = None


class CorrectionRequest(BaseModel):
    draft_pick_id: str
    reason: str | None = None


class FinalizeRequest(BaseModel):
    note: str | None = None


class ReopenRequest(BaseModel):
    reason: str
    confirm: str


def _scorer_actor(scorer_name: str | None) -> ActorContext:
    return ActorContext(actor_type="anonymous_operator", actor_id=scorer_name, actor_role="scorer")


def _pick_actor(principal: Principal, legacy_scorer_name: str | None) -> ActorContext:
    # Codex review, PR #225 (P2): `app.audit`'s actor contract requires a
    # genuine coach self-action to use `actor_type="coach"`
    # (`ActorContext.coach`), never `anonymous_operator` -- that type is
    # reserved for the shared-token/delegated-role proxy surface and "must
    # never be used for an authenticated coach's own action" (see
    # app/audit.py's module docstring). Before issue #181 granted Coach
    # `draft.participate`, this branch was only ever reached by a
    # delegated (Scorer/Secretary/Admin/Replay Operator) active role.
    if principal.role is Role.COACH and principal.coach_id is not None:
        return ActorContext.coach(principal.coach_id)
    if principal.coach_id is not None:
        return ActorContext(
            actor_type="anonymous_operator",
            actor_id=principal.coach_id,
            actor_role=principal.role.value,
        )
    return _scorer_actor(legacy_scorer_name)


def _readiness(request: Request, season_id: str) -> dict:
    return build_readiness(request, season_id, draft_kind="preseason")


def _board(request: Request, season_id: str) -> dict:
    board = build_board(request, season_id, draft_kind="preseason")
    board["preseason_url"] = f"/admin/preseason/{season_id}"
    return board


def _authorise_season(request: Request, principal: Principal, season_id: str):
    require_role_covers_season(request, principal, season_id)


def _coach_entry_id(request: Request, principal: Principal, season_id: str) -> str:
    # Issue #229: derive the entry from the authenticated Coach's own
    # identity (`resolve_my_entry_id`, shared with the mid-season Coach
    # surface) rather than trusting a client-supplied ownership id -- 404
    # (never a raw season id in the response) when this coach has no entry
    # in the season, matching `require_entry_context`'s enumeration-safe
    # convention.
    entry_id = resolve_my_entry_id(request, principal, season_id)
    if entry_id is None:
        raise HTTPException(status_code=404, detail="Private resource not found")
    return entry_id


@router.get("/{season_id}/readiness")
def readiness(
    season_id: str,
    request: Request,
    principal: Principal = Depends(require_capability("draft.participate")),
):
    _authorise_season(request, principal, season_id)
    return _readiness(request, season_id)


@router.get("/{season_id}/board")
def board(
    season_id: str,
    request: Request,
    principal: Principal = Depends(require_capability("draft.participate")),
):
    _authorise_season(request, principal, season_id)
    return _board(request, season_id)


@router.get("/{season_id}/available-players")
def available_players(
    season_id: str,
    request: Request,
    q: str | None = None,
    limit: int = 50,
    principal: Principal = Depends(require_capability("player_pool.read")),
):
    _authorise_season(request, principal, season_id)
    players = request.app.state.player_pool.search_available(season_id, q, limit)
    return [dataclasses.asdict(player) for player in players]


@router.get("/{season_id}/players")
def player_pool(
    season_id: str,
    request: Request,
    q: str | None = None,
    availability: str | None = None,
    limit: int = 200,
    principal: Principal = Depends(require_capability("player_pool.read")),
):
    _authorise_season(request, principal, season_id)
    availability = availability or None
    if availability not in (None, "available", "owned", "unresolved"):
        raise HTTPException(status_code=400, detail="availability must be available, owned, or unresolved")
    return player_browse_view(
        request, season_id, draft_kind="preseason", query=q, availability=availability, limit=min(limit, 500)
    )


@router.post("/{season_id}/pick")
def submit_pick(
    season_id: str,
    payload: PickRequest,
    request: Request,
    principal: Principal = Depends(require_capability("draft.participate")),
):
    _authorise_season(request, principal, season_id)
    # Authenticated #107 sessions must use their represented entry. Keep the
    # established shared-token compatibility path, which has no coach/session.
    if principal.coach_id is not None:
        require_entry_context(request, principal, payload.season_entry_id)
    request.app.state.draft.execute_pick(
        season_id,
        payload.season_entry_id,
        payload.season_player_id,
        pick_id=payload.draft_pick_id,
        actor=_pick_actor(principal, payload.scorer_name),
        reason=payload.reason or "draft selection",
    )
    return _board(request, season_id)


@router.post("/{season_id}/pause", dependencies=[Depends(require_admin)])
def pause(season_id: str, payload: PauseRequest, request: Request):
    request.app.state.draft.pause(season_id, actor=ActorContext.anonymous_operator("scorer"), reason=payload.reason)
    return _board(request, season_id)


@router.post("/{season_id}/resume", dependencies=[Depends(require_admin)])
def resume(season_id: str, payload: PauseRequest, request: Request):
    request.app.state.draft.resume(season_id, actor=ActorContext.anonymous_operator("scorer"), reason=payload.reason)
    return _board(request, season_id)


@router.post("/{season_id}/correct", dependencies=[Depends(require_admin)])
def correct(season_id: str, payload: CorrectionRequest, request: Request):
    request.app.state.draft.correct_pick(
        season_id,
        payload.draft_pick_id,
        actor=ActorContext.anonymous_operator("admin"),
        reason=payload.reason,
    )
    return _board(request, season_id)


@router.post("/{season_id}/finalize", dependencies=[Depends(require_admin)])
def finalize(season_id: str, payload: FinalizeRequest, request: Request):
    request.app.state.draft.finalize(season_id, actor=ActorContext.anonymous_operator("admin"), note=payload.note)
    return _board(request, season_id)


@router.post("/{season_id}/reopen", dependencies=[Depends(require_admin)])
def reopen(season_id: str, payload: ReopenRequest, request: Request):
    if payload.confirm != REOPEN_CONFIRMATION_PHRASE:
        raise HTTPException(status_code=400, detail=f"confirm must exactly equal '{REOPEN_CONFIRMATION_PHRASE}'")
    request.app.state.draft.reopen(season_id, actor=ActorContext.anonymous_operator("admin"), reason=payload.reason)
    return _board(request, season_id)


@page_router.get("/admin/draft/{season_id}", response_class=HTMLResponse)
def draft_page(
    season_id: str,
    request: Request,
    principal: Principal = Depends(require_capability("draft.participate")),
):
    _authorise_season(request, principal, season_id)
    return templates.TemplateResponse(
        request,
        "draft.html",
        {
            "season_id": season_id,
            "draft_kind": "preseason",
            "api_base": "/api/admin/draft",
            "my_season_entry_id": resolve_my_entry_id(request, principal, season_id),
            "coach_view": principal.role is Role.COACH,
        },
    )


@coach_router.get("/{season_id}/players")
def coach_player_pool(
    season_id: str,
    request: Request,
    q: str | None = None,
    availability: str | None = None,
    limit: int = 200,
    principal: Principal = Depends(coach_participant),
):
    _coach_entry_id(request, principal, season_id)
    availability = availability or None
    if availability not in (None, "available", "owned", "unresolved"):
        raise HTTPException(status_code=400, detail="availability must be available, owned, or unresolved")
    return player_browse_view(
        request, season_id, draft_kind="preseason", query=q, availability=availability, limit=min(limit, 500)
    )


@coach_router.get("/{season_id}/board")
def coach_board(season_id: str, request: Request, principal: Principal = Depends(coach_participant)):
    _coach_entry_id(request, principal, season_id)
    return _board(request, season_id)


@coach_router.post("/{season_id}/pick")
def coach_submit_pick(
    season_id: str,
    payload: PickRequest,
    request: Request,
    principal: Principal = Depends(coach_participant),
):
    # Issue #229: never trust `payload.season_entry_id` -- a Coach may only
    # ever act for the entry `_coach_entry_id` resolves from their own
    # authenticated identity (404, enumeration-safe, for anything else),
    # matching `app.routes.midseason_draft.coach_submit_pick`'s existing
    # cross-team rejection. `require_entry_context` then performs the same
    # authoritative ownership check `submit_pick` above relies on, and
    # `execute_pick` still re-validates turn/ownership/availability/squad
    # limits/staleness inside one transaction regardless of what this route
    # already checked.
    own_entry_id = _coach_entry_id(request, principal, season_id)
    if payload.season_entry_id != own_entry_id:
        raise HTTPException(status_code=404, detail="Private resource not found")
    require_entry_context(request, principal, payload.season_entry_id)
    request.app.state.draft.execute_pick(
        season_id,
        own_entry_id,
        payload.season_player_id,
        pick_id=payload.draft_pick_id,
        actor=_pick_actor(principal, None),
        reason=payload.reason or "draft selection",
    )
    return _board(request, season_id)


@page_router.get("/account/preseason-draft/{season_id}", response_class=HTMLResponse)
def coach_draft_page(season_id: str, request: Request, principal: Principal = Depends(coach_participant)):
    """The Coach-facing pre-season draft page (issue #229): the same shared
    `draft.html` board the operator surface above renders, restricted to
    Coach-appropriate context/actions. Reuses `resolve_my_entry_id`/
    `_board` exactly as `/admin/draft/{season_id}` does -- no second draft
    engine or duplicated authority rule, only the view-layer distinction
    `draft.html`'s existing `coach_view` flag already implements (issue
    #181's shared draft board, previously only reachable from the operator
    URL or, for the mid-season draft, from issue #226's `/account/
    midseason-draft/{season_id}`)."""
    entry_id = _coach_entry_id(request, principal, season_id)
    team = request.app.state.identities.get_public_team(entry_id)
    return templates.TemplateResponse(
        request,
        "draft.html",
        {
            "season_id": season_id,
            "draft_kind": "preseason",
            "api_base": "/api/account/preseason-draft",
            "my_season_entry_id": entry_id,
            "coach_view": True,
            "coach_team_name": team.team_name if team else "Coach",
        },
    )
