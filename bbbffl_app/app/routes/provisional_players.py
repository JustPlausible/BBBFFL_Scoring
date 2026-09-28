"""Provisional player creation, Coach nomination and canonical afl-api
reconciliation (issue #242).

Thin HTTP translation over `app.provisional_players` -- every authorization
decision happens here, and every domain invariant (required fields, season-
completed write fence, reconciliation safety) lives in that module.

## Role boundary

- **Coach** (`provisional_player.nominate`, granted to Role.COACH only):
  may submit/list a missing-player nomination for a season entry they own
  or currently represent (`require_entry_context`, the same rule
  `app.routes.shortlist` uses for private per-entry data). There is no
  route here that lets a Coach create a provisional player, decide a
  candidate, or reconcile one -- see `app.authorization.CAPABILITIES`.
- **Scorer/Administrator** (`provisional_player.manage`): create a
  provisional player (optionally resolving a pending nomination),
  dismiss a nomination, and review/decide a detected candidate match
  (approve via reconciliation, reject, defer). Season-scoped via
  `require_role_covers_season`, exactly like every other season-model
  route. Reconciliation is treated as the high-risk identity operation the
  issue calls for: it requires the double-submit CSRF token for a
  cookie-authenticated session, the same convention
  `app.routes.ladder_tie_ruling`/`app.routes.season_activation` use for
  their own exceptional governance actions.
"""

from __future__ import annotations

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
    require_entry_context,
    require_role_covers_season,
)
from app.config import BASE_DIR
from app.csrf import issue_token, verify_token
from app.draft_board import resolve_my_entry_id
from app.provisional_players import PlayerNominationRepository, ProvisionalPlayerRepository

router = APIRouter()
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

nominate = require_capability("provisional_player.nominate")
manage = require_capability("provisional_player.manage")


class NominateRequest(BaseModel):
    season_id: str
    player_name: str
    afl_club_note: str | None = None
    note: str | None = None


class DismissNominationRequest(BaseModel):
    reason: str


class CreateProvisionalRequest(BaseModel):
    display_name: str
    given_name: str
    family_name: str
    note: str
    afl_team_name: str | None = None
    reason: str | None = None
    nomination_id: str | None = None


class ReconcileRequest(BaseModel):
    target_season_player_id: str
    reason: str


class CandidateDecisionRequest(BaseModel):
    canonical_player_id: int
    reason: str | None = None


def _actor(principal: Principal) -> ActorContext:
    # Mirrors `app.routes.shortlist._actor`: a Coach acting as themselves is
    # a genuine self-action; a delegated Scorer/Admin/Replay-Operator role
    # (including the legacy shared-token operator) is always the proxy case.
    if principal.role is Role.COACH and principal.coach_id is not None:
        return ActorContext.coach(principal.coach_id)
    return ActorContext(actor_type="anonymous_operator", actor_id=principal.coach_id, actor_role=principal.role.value)


def _require_session_csrf(request: Request, principal: Principal) -> None:
    if principal.session_id is not None and not verify_token(
        request.app.state.settings.session_secret,
        request.cookies.get("bbbffl_csrf"),
        request.headers.get("X-CSRF-Token"),
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _require_known_season(request: Request, season_id: str) -> None:
    if request.app.state.seasons.get_season(season_id) is None:
        raise HTTPException(status_code=404, detail="Unknown season")


def _nomination_view(nomination) -> dict:
    return dataclasses.asdict(nomination)


def _candidate_view(request: Request, season_id: str, candidate) -> dict:
    target = request.app.state.player_pool.get(season_id, candidate.canonical_player_id)
    return {
        **dataclasses.asdict(candidate),
        "target_display_name": target.display_name if target else None,
        "target_afl_team_name": target.afl_team_name if target else None,
        "target_season_player_id": target.season_player_id if target else None,
    }


def _outstanding_view(request: Request, season_id: str, row) -> dict:
    player = row.player
    return {
        "season_player_id": player.season_player_id,
        "display_name": player.display_name,
        "given_name": player.given_name,
        "family_name": player.family_name,
        "afl_team_name": player.afl_team_name,
        "provisional_note": player.provisional_note,
        "created_at": player.created_at,
        "owner_season_entry_id": row.owner_season_entry_id,
        "owner_team_name": row.owner_team_name,
        "has_candidate": row.has_candidate,
        "is_ambiguous": row.is_ambiguous,
        "candidates": [_candidate_view(request, season_id, c) for c in row.candidates if c.status == "pending"],
    }


def _management_payload(request: Request, season_id: str) -> dict:
    provisional = ProvisionalPlayerRepository(request.app.state.database)
    nominations = PlayerNominationRepository(request.app.state.database)
    entries = {entry.season_entry_id: entry.team_name for entry in request.app.state.identities.list_entries(season_id)}
    return {
        "season_id": season_id,
        "outstanding": [_outstanding_view(request, season_id, row) for row in provisional.list_outstanding(season_id)],
        "pending_nominations": [_nomination_view(n) for n in nominations.list_for_season(season_id, status="pending")],
        "team_names": entries,
    }


# -- Coach: nominate a missing player -----------------------------------------


@router.post("/api/account/player-nominations/{season_entry_id}")
def submit_nomination(
    season_entry_id: str, payload: NominateRequest, request: Request, principal: Principal = Depends(nominate)
):
    require_entry_context(request, principal, season_entry_id)
    nomination = PlayerNominationRepository(request.app.state.database).submit(
        payload.season_id,
        season_entry_id,
        payload.player_name,
        afl_club_note=payload.afl_club_note,
        note=payload.note,
        actor=_actor(principal),
    )
    return _nomination_view(nomination)


@router.get("/api/account/player-nominations/{season_entry_id}")
def list_own_nominations(season_entry_id: str, request: Request, principal: Principal = Depends(nominate)):
    require_entry_context(request, principal, season_entry_id)
    nominations = PlayerNominationRepository(request.app.state.database).list_for_entry(season_entry_id)
    return {"nominations": [_nomination_view(n) for n in nominations]}


@router.get("/api/account/provisional-notice/{season_id}")
def coach_provisional_notice(season_id: str, request: Request, principal: Principal = Depends(nominate)):
    """Issue #242's persistent Coach dashboard notice: every currently
    outstanding provisional player in this season, with this signed-in
    Coach's own entry (if any) identified so `account.html` can highlight
    a player on their own squad specifically -- informational only, no
    reconciliation control is exposed here."""
    my_entry_id = resolve_my_entry_id(request, principal, season_id) if principal.role is Role.COACH else None
    outstanding = ProvisionalPlayerRepository(request.app.state.database).list_outstanding(season_id)
    return {
        "season_id": season_id,
        "my_season_entry_id": my_entry_id,
        "outstanding": [
            {
                "season_player_id": row.player.season_player_id,
                "display_name": row.player.display_name,
                "on_my_squad": my_entry_id is not None and row.owner_season_entry_id == my_entry_id,
                "has_candidate": row.has_candidate,
            }
            for row in outstanding
        ],
    }


# -- Scorer/Administrator: verify, create, detect, reconcile -----------------


@router.get("/api/scorer/provisional-players/{season_id}")
def get_management_view(season_id: str, request: Request, principal: Principal = Depends(manage)):
    _require_known_season(request, season_id)
    require_role_covers_season(request, principal, season_id)
    return _management_payload(request, season_id)


@router.post("/api/scorer/provisional-players/{season_id}/create")
def create_provisional(
    season_id: str, payload: CreateProvisionalRequest, request: Request, principal: Principal = Depends(manage)
):
    _require_known_season(request, season_id)
    require_role_covers_season(request, principal, season_id)
    _require_session_csrf(request, principal)
    ProvisionalPlayerRepository(request.app.state.database).create(
        season_id,
        display_name=payload.display_name,
        given_name=payload.given_name,
        family_name=payload.family_name,
        note=payload.note,
        afl_team_name=payload.afl_team_name,
        actor=_actor(principal),
        reason=payload.reason,
        nomination_id=payload.nomination_id,
    )
    return _management_payload(request, season_id)


@router.post("/api/scorer/provisional-players/{season_id}/nominations/{nomination_id}/dismiss")
def dismiss_nomination(
    season_id: str,
    nomination_id: str,
    payload: DismissNominationRequest,
    request: Request,
    principal: Principal = Depends(manage),
):
    _require_known_season(request, season_id)
    require_role_covers_season(request, principal, season_id)
    _require_session_csrf(request, principal)
    PlayerNominationRepository(request.app.state.database).dismiss(
        season_id, nomination_id, actor=_actor(principal), reason=payload.reason
    )
    return _management_payload(request, season_id)


@router.post("/api/scorer/provisional-players/{season_id}/{season_player_id}/reconcile")
def reconcile(
    season_id: str,
    season_player_id: str,
    payload: ReconcileRequest,
    request: Request,
    principal: Principal = Depends(manage),
):
    _require_known_season(request, season_id)
    require_role_covers_season(request, principal, season_id)
    _require_session_csrf(request, principal)
    ProvisionalPlayerRepository(request.app.state.database).reconcile(
        season_id, season_player_id, payload.target_season_player_id, actor=_actor(principal), reason=payload.reason
    )
    return _management_payload(request, season_id)


@router.post("/api/scorer/provisional-players/{season_id}/{season_player_id}/candidates/reject")
def reject_candidate(
    season_id: str,
    season_player_id: str,
    payload: CandidateDecisionRequest,
    request: Request,
    principal: Principal = Depends(manage),
):
    _require_known_season(request, season_id)
    require_role_covers_season(request, principal, season_id)
    _require_session_csrf(request, principal)
    if not payload.reason or not payload.reason.strip():
        raise HTTPException(status_code=400, detail="rejecting a candidate match requires a reason")
    ProvisionalPlayerRepository(request.app.state.database).reject_candidate(
        season_id, season_player_id, payload.canonical_player_id, actor=_actor(principal), reason=payload.reason
    )
    return _management_payload(request, season_id)


@router.post("/api/scorer/provisional-players/{season_id}/{season_player_id}/candidates/defer")
def defer_candidate(
    season_id: str,
    season_player_id: str,
    payload: CandidateDecisionRequest,
    request: Request,
    principal: Principal = Depends(manage),
):
    _require_known_season(request, season_id)
    require_role_covers_season(request, principal, season_id)
    _require_session_csrf(request, principal)
    ProvisionalPlayerRepository(request.app.state.database).defer_candidate(
        season_id, season_player_id, payload.canonical_player_id, actor=_actor(principal), reason=payload.reason
    )
    return _management_payload(request, season_id)


@page_router.get("/scorer/provisional-players/{season_id}", response_class=HTMLResponse)
def provisional_players_page(season_id: str, request: Request):
    token = issue_token(request.app.state.settings.session_secret)
    response = templates.TemplateResponse(
        request, "provisional_players.html", {"season_id": season_id, "csrf_token": token}
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
