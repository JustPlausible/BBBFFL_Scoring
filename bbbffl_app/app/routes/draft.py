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
context, with their stable coach identity recorded as the audit actor.  The
older shared-token API may still supply `PickRequest.scorer_name` as its
transitional audit label; it is never an entry or ownership identifier.

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

from app.audit import ActorContext, AuditEventRepository
from app.authorization import (
    Principal,
    require_capability,
    require_entry_context,
    require_role_covers_season,
)
from app.config import BASE_DIR
from app.draft import draft_board_readiness
from app.routes.admin import require_admin

router = APIRouter(prefix="/api/admin/draft")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

REOPEN_CONFIRMATION_PHRASE = "REOPEN FINALIZED DRAFT"


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
    if principal.coach_id is not None:
        return ActorContext(
            actor_type="coach_identity",
            actor_id=principal.coach_id,
            actor_role=principal.role.value,
        )
    return _scorer_actor(legacy_scorer_name)


def _entry_view(request: Request, entry_id: str, cache: dict) -> dict:
    """Resolve stable entry identity into public, human-facing labels."""
    if entry_id not in cache:
        team = request.app.state.identities.get_public_team(entry_id)
        coach = request.app.state.identities.get_current_coach(entry_id)
        cache[entry_id] = {
            "season_entry_id": entry_id,
            "team_name": team.team_name if team else "Unknown team",
            "coach_display_name": coach.display_name if coach else "Coach not assigned",
        }
    return cache[entry_id]


def _player_name(request: Request, season_player_id: str | None, cache: dict) -> str | None:
    if season_player_id is None:
        return None
    if season_player_id not in cache:
        player = request.app.state.player_pool.get_by_id(season_player_id)
        cache[season_player_id] = player.display_name if player else season_player_id
    return cache[season_player_id]


def _pick_view(request: Request, pick, cache: dict, player_cache: dict, event_cache: dict) -> dict:
    current_identity = _entry_view(request, pick.current_season_entry_id, cache)
    original_identity = _entry_view(request, pick.original_season_entry_id, cache)
    player = (
        request.app.state.player_pool.get_by_id(pick.selected_season_player_id)
        if pick.selected_season_player_id
        else None
    )
    # Completion provenance is bulk-loaded once by `_board`. Current and
    # upcoming picks cannot have a completion event, so never query for it.
    event = event_cache.get(pick.draft_pick_id) if pick.completed_at else None
    actor_name = None
    if event and event.actor_id:
        actor = request.app.state.identities.get_coach(event.actor_id)
        actor_name = actor.display_name if actor else event.actor_id
    return {
        "draft_pick_id": pick.draft_pick_id,
        "overall_number": pick.overall_number,
        "round": pick.draft_round,
        "round_position": pick.round_position,
        "original_season_entry_id": pick.original_season_entry_id,
        "original_team_name": original_identity["team_name"],
        "current_season_entry_id": pick.current_season_entry_id,
        "current_team_name": current_identity["team_name"],
        "current_coach_display_name": current_identity["coach_display_name"],
        "traded": pick.original_season_entry_id != pick.current_season_entry_id,
        "selected_season_player_id": pick.selected_season_player_id,
        "selected_player_name": _player_name(request, pick.selected_season_player_id, player_cache),
        "selected_player_afl_team_name": player.afl_team_name if player else None,
        "completed_at": pick.completed_at,
        "proxy": (
            {
                "operator_name": actor_name or "Scorer",
                "operator_role": event.actor_role,
                "reason": event.reason,
                "on_behalf_of_team": current_identity["team_name"],
            }
            if event
            else None
        ),
    }


def _readiness(request: Request, season_id: str) -> dict:
    database = request.app.state.database
    entries = request.app.state.identities.list_entries(season_id)
    status = request.app.state.draft.status(season_id)
    config = database.execute(
        "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?", (season_id,)
    ).fetchone()
    shared = draft_board_readiness(
        database,
        request.app.state.identities,
        request.app.state.draft,
        request.app.state.player_pool,
        season_id,
    )
    available_count = shared["available_player_count"]
    if status is not None:
        total_required = status.total_picks
        completed = status.completed_picks
    else:
        total_required = len(entries) * config["squad_limit"] if config is not None else 0
        completed = 0
    remaining_required = max(total_required - completed, 0)
    pool_sufficient = total_required > 0 and available_count >= remaining_required
    player_word = "player" if available_count == 1 else "players"
    player_verb = "is" if available_count == 1 else "are"
    selection_word = "selection" if remaining_required == 1 else "selections"
    checks = [
        {
            "key": "entries",
            "ready": len(entries) == 10,
            "label": "Ten BBBFFL teams",
            "detail": f"{len(entries)} of 10 season entries are present.",
        },
        {
            "key": "order",
            "ready": status is not None,
            "label": "Draft order accepted",
            "detail": "The frozen draft order is ready."
            if status
            else "Accept and freeze the draft order in season setup.",
        },
        {
            "key": "players",
            "ready": pool_sufficient,
            "label": "Season player pool",
            "detail": (
                f"{available_count} selectable {player_word} {player_verb} available for "
                f"{remaining_required} remaining {selection_word}."
                if pool_sufficient
                else f"Only {available_count} selectable {player_word} {player_verb} available; "
                f"{remaining_required} remaining {selection_word} are required."
            ),
        },
        {
            "key": "squad",
            "ready": config is not None and config["squad_limit"] > 0,
            "label": "Target squad size",
            "detail": f"Target: {config['squad_limit']} players per team."
            if config
            else "Configure the target squad size.",
        },
    ]
    return {"ready": shared["ready"], "checks": checks}


def _board(request: Request, season_id: str) -> dict:
    draft = request.app.state.draft
    status = draft.status(season_id)
    if status is None:
        raise KeyError(season_id)
    cache: dict = {}
    player_cache: dict = {}
    event_cache: dict = {}
    order = [
        {"position": position, **_entry_view(request, entry_id, cache)} for position, entry_id in draft.order(season_id)
    ]
    all_picks = draft.picks(season_id)
    completed = [pick for pick in all_picks if pick.completed_at is not None]
    remaining = [pick for pick in all_picks if pick.completed_at is None]
    current = remaining[0] if remaining else None
    latest_completed = completed[-1] if completed else None
    completed_ids = {pick.draft_pick_id for pick in completed}
    event_cache = {
        event.entity_id: event
        for event in AuditEventRepository(request.app.state.database).list_events(action="draft.pick.completed")
        if event.entity_id in completed_ids
    }
    return {
        "season_id": season_id,
        "status": dataclasses.asdict(status),
        "readiness": _readiness(request, season_id),
        "order": order,
        "current_pick": _pick_view(request, current, cache, player_cache, event_cache) if current else None,
        "upcoming_picks": [_pick_view(request, pick, cache, player_cache, event_cache) for pick in remaining[1:]],
        "completed_picks": [
            _pick_view(request, pick, cache, player_cache, event_cache) for pick in reversed(completed)
        ],
        "team_progress": [
            {
                **identity,
                "drafted_count": sum(
                    1 for pick in completed if pick.current_season_entry_id == identity["season_entry_id"]
                ),
                "target_count": status.target_squad_size,
            }
            for identity in (_entry_view(request, entry_id, cache) for _, entry_id in draft.order(season_id))
        ],
        "correctable_draft_pick_id": latest_completed.draft_pick_id if latest_completed else None,
        "corrections": [dataclasses.asdict(item) for item in draft.corrections(season_id)],
        "preseason_url": f"/admin/preseason/{season_id}",
    }


def _authorise_season(request: Request, principal: Principal, season_id: str):
    require_role_covers_season(request, principal, season_id)


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
    return [
        dataclasses.asdict(item)
        for item in request.app.state.player_pool.browse(season_id, q, availability, min(limit, 500))
    ]


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
    return templates.TemplateResponse(request, "draft.html", {"season_id": season_id})
