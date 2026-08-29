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

Scorer proxy entry: `PickRequest.scorer_name` (optional, free text) is
recorded as the audited actor's `actor_id` with `actor_role="scorer"` --
*never* as the receiving `season_entry_id`, which is a separate, always-
required field. This is what lets the audit trail distinguish "which
BBBFFL entry received the player" from "which human scorer entered the
selection" (see docs/scorer-draft-workflow.md's "Proxy picks" section).

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
from app.config import BASE_DIR
from app.routes.admin import require_admin

router = APIRouter(prefix="/api/admin/draft")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

REOPEN_CONFIRMATION_PHRASE = "REOPEN FINALIZED DRAFT"


class PickRequest(BaseModel):
    season_entry_id: str
    season_player_id: str
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


def _team_name(request: Request, entry_id: str, cache: dict) -> str:
    if entry_id not in cache:
        team = request.app.state.identities.get_public_team(entry_id)
        cache[entry_id] = team.team_name if team else entry_id
    return cache[entry_id]


def _player_name(request: Request, season_player_id: str | None, cache: dict) -> str | None:
    if season_player_id is None:
        return None
    if season_player_id not in cache:
        player = request.app.state.player_pool.get_by_id(season_player_id)
        cache[season_player_id] = player.display_name if player else season_player_id
    return cache[season_player_id]


def _pick_view(request: Request, pick, cache: dict, player_cache: dict) -> dict:
    return {
        "draft_pick_id": pick.draft_pick_id,
        "overall_number": pick.overall_number,
        "round": pick.draft_round,
        "round_position": pick.round_position,
        "original_season_entry_id": pick.original_season_entry_id,
        "original_team_name": _team_name(request, pick.original_season_entry_id, cache),
        "current_season_entry_id": pick.current_season_entry_id,
        "current_team_name": _team_name(request, pick.current_season_entry_id, cache),
        "traded": pick.original_season_entry_id != pick.current_season_entry_id,
        "selected_season_player_id": pick.selected_season_player_id,
        "selected_player_name": _player_name(request, pick.selected_season_player_id, player_cache),
        "completed_at": pick.completed_at,
    }


def _board(request: Request, season_id: str) -> dict:
    draft = request.app.state.draft
    status = draft.status(season_id)
    if status is None:
        raise KeyError(season_id)
    cache: dict = {}
    player_cache: dict = {}
    order = [
        {"position": position, "season_entry_id": entry_id, "team_name": _team_name(request, entry_id, cache)}
        for position, entry_id in draft.order(season_id)
    ]
    all_picks = draft.picks(season_id)
    completed = [pick for pick in all_picks if pick.completed_at is not None]
    remaining = [pick for pick in all_picks if pick.completed_at is None]
    current = remaining[0] if remaining else None
    latest_completed = completed[-1] if completed else None
    return {
        "season_id": season_id,
        "status": dataclasses.asdict(status),
        "order": order,
        "current_pick": _pick_view(request, current, cache, player_cache) if current else None,
        "upcoming_picks": [_pick_view(request, pick, cache, player_cache) for pick in remaining[1:]],
        "completed_picks": [_pick_view(request, pick, cache, player_cache) for pick in reversed(completed)],
        "correctable_draft_pick_id": latest_completed.draft_pick_id if latest_completed else None,
        "corrections": [dataclasses.asdict(item) for item in draft.corrections(season_id)],
    }


@router.get("/{season_id}/board", dependencies=[Depends(require_admin)])
def board(season_id: str, request: Request):
    return _board(request, season_id)


@router.get("/{season_id}/available-players", dependencies=[Depends(require_admin)])
def available_players(season_id: str, request: Request, q: str | None = None, limit: int = 50):
    players = request.app.state.player_pool.search_available(season_id, q, limit)
    return [dataclasses.asdict(player) for player in players]


@router.post("/{season_id}/pick", dependencies=[Depends(require_admin)])
def submit_pick(season_id: str, payload: PickRequest, request: Request):
    request.app.state.draft.execute_pick(
        season_id,
        payload.season_entry_id,
        payload.season_player_id,
        actor=_scorer_actor(payload.scorer_name),
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


@page_router.get("/admin/draft/{season_id}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def draft_page(season_id: str, request: Request):
    return templates.TemplateResponse(request, "draft.html", {"season_id": season_id})
