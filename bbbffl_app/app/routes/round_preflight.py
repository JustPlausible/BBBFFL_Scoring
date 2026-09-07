from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import Principal, require_capability, require_role_covers_season
from app.config import BASE_DIR
from app.csrf import issue_token, verify_token
from app.round_preflight import (
    StaleMappingRevisionError,
    StaleTriggerRevisionError,
    accept_preflight_mapping,
    build_round_preflight,
    configure_preflight_trigger,
    open_preflight_round,
)

router = APIRouter(prefix="/api/admin/round-preflight")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
require_round_operator = require_capability("roundsetup.manage")


class MappingRequest(BaseModel):
    afl_season_id: int
    afl_round_id: int
    reason: str | None = None
    # Issue #152: acceptance requires an explicit operator confirmation, not
    # merely a chosen season/round -- see accept_preflight_mapping's
    # docstring. Defaults False so an unmodified/legacy client can never
    # accidentally satisfy it.
    confirmed: bool = False
    # Optimistic concurrency: the mapping revision the caller last observed
    # (0 meaning "no accepted mapping yet"), or omit to skip the check.
    expected_revision: int | None = None


class TriggerRequest(BaseModel):
    trigger_key: str
    trigger_type: str
    sequence: int
    afl_match_ids: list[int]
    reason: str | None = None
    # Optimistic concurrency: the trigger's revision the caller last
    # observed for this trigger_key (0 meaning "does not exist yet"), or
    # omit to skip the check.
    expected_revision: int | None = None


def _actor(principal):
    # Administrative domain writes retain the authenticated person's identity,
    # even when their active context represents another coach's season entry.
    # The shared audit model classifies these as operator/proxy actions rather
    # than actions performed by a coach on their own behalf.
    return ActorContext(actor_type="anonymous_operator", actor_id=principal.coach_id, actor_role=principal.role.value)


def _authorise(request, principal, round_id):
    row = request.app.state.database.execute(
        "SELECT c.season_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
        (round_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Unknown BBBFFL round")
    require_role_covers_season(request, principal, row["season_id"])


def _view(request, round_id):
    state = request.app.state
    return build_round_preflight(state.database, state.lifecycle, state.identities, state.afl_client, round_id)


def _available_rounds(request, principal):
    """Recognisable, active-context-filtered navigation into preflight."""
    rows = request.app.state.database.execute(
        "SELECT r.bbbffl_round_id, r.label round_label, r.sequence, c.label competition_label, "
        "c.stream_key, c.season_id, s.year, s.label season_label "
        "FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
        "JOIN bbbffl_season s ON s.season_id=c.season_id WHERE c.stream_type='ordinary' "
        "ORDER BY s.year DESC, c.stream_key, r.sequence"
    ).fetchall()
    available = []
    for row in rows:
        try:
            require_role_covers_season(request, principal, row["season_id"])
        except HTTPException as exc:
            if exc.status_code == 403:
                continue
            raise
        available.append({**dict(row), "preflight_url": f"/admin/round-preflight/{row['bbbffl_round_id']}"})
    return available


@router.get("")
def round_index(request: Request, principal: Principal = Depends(require_round_operator)):
    return {"rounds": _available_rounds(request, principal)}


@router.get("/{round_id}")
def view(round_id: str, request: Request, principal: Principal = Depends(require_round_operator)):
    _authorise(request, principal, round_id)
    return _view(request, round_id)


@router.get("/{round_id}/afl-seasons")
def afl_seasons(round_id: str, request: Request, principal: Principal = Depends(require_round_operator)):
    """Human-readable AFL season listing for mapping selection (issue #152)
    -- always from the request's own configured afl_client, so live and
    replay deployments answer through the identical contract/endpoint."""
    _authorise(request, principal, round_id)
    client = request.app.state.afl_client
    list_seasons = getattr(client, "get_seasons", None)
    if not callable(list_seasons):
        raise HTTPException(501, "The configured AFL client does not support listing seasons")
    try:
        seasons = list_seasons()
    except Exception as exc:
        raise HTTPException(502, f"AFL season list is unavailable: {exc}") from exc
    return {
        "afl_seasons": [
            {"season_id": s.season_id, "year": s.year, "name": s.name, "is_current": s.is_current} for s in seasons
        ]
    }


@router.get("/{round_id}/afl-seasons/{afl_season_id}/afl-rounds")
def afl_rounds(
    round_id: str, afl_season_id: int, request: Request, principal: Principal = Depends(require_round_operator)
):
    """Human-readable AFL round listing for one season (issue #152), loaded
    only once an operator has selected a season -- opaque `round_id` is
    retained only as secondary/reference information alongside `round_number`."""
    _authorise(request, principal, round_id)
    try:
        rounds = request.app.state.afl_client.get_rounds(afl_season_id)
    except Exception as exc:
        raise HTTPException(502, f"AFL round list is unavailable: {exc}") from exc
    return {"afl_rounds": [{"round_id": r.round_id, "round_number": r.round_number} for r in rounds]}


@router.post("/{round_id}/mapping")
def accept_mapping(
    round_id: str, payload: MappingRequest, request: Request, principal: Principal = Depends(require_round_operator)
):
    _authorise(request, principal, round_id)
    _csrf(request, principal)
    try:
        accept_preflight_mapping(
            request.app.state.database,
            request.app.state.lifecycle,
            request.app.state.afl_client,
            round_id,
            payload.afl_season_id,
            payload.afl_round_id,
            reason=payload.reason,
            confirmed=payload.confirmed,
            expected_revision=payload.expected_revision,
            actor=_actor(principal),
        )
    except StaleMappingRevisionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _view(request, round_id)


@router.post("/{round_id}/lockout-trigger")
def configure_trigger(
    round_id: str, payload: TriggerRequest, request: Request, principal: Principal = Depends(require_round_operator)
):
    _authorise(request, principal, round_id)
    _csrf(request, principal)
    try:
        configure_preflight_trigger(
            request.app.state.database,
            round_id,
            payload,
            request.app.state.afl_client,
            reason=payload.reason or "Lockout plan configured in preflight",
            actor=_actor(principal),
        )
    except StaleTriggerRevisionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:  # covers TriggerValidationError and app.lockouts.LockoutIntegrityError
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _view(request, round_id)


@router.post("/{round_id}/open")
def open_round(round_id: str, request: Request, principal: Principal = Depends(require_round_operator)):
    _authorise(request, principal, round_id)
    _csrf(request, principal)
    state = request.app.state
    preflight = _view(request, round_id)
    if not preflight["readiness"]["safe_to_open"]:
        raise HTTPException(
            409,
            {"message": "Round failed preflight and was not opened", "blockers": preflight["readiness"]["blockers"]},
        )
    actor = _actor(principal)
    open_preflight_round(state.lifecycle, round_id, actor=actor)
    return _view(request, round_id)


@page_router.get("/admin/round-preflight/{round_id}", response_class=HTMLResponse)
def page(round_id: str, request: Request):
    token = issue_token(request.app.state.settings.session_secret)
    response = templates.TemplateResponse(request, "round_preflight.html", {"round_id": round_id, "csrf_token": token})
    response.set_cookie(
        "bbbffl_csrf",
        token,
        max_age=3600,
        httponly=True,
        secure=request.app.state.settings.is_production,
        samesite="lax",
    )
    return response


@page_router.get("/admin/round-preflight", response_class=HTMLResponse)
def index_page(request: Request):
    token = issue_token(request.app.state.settings.session_secret)
    response = templates.TemplateResponse(request, "round_preflight_index.html", {"csrf_token": token})
    response.set_cookie(
        "bbbffl_csrf",
        token,
        max_age=3600,
        httponly=True,
        secure=request.app.state.settings.is_production,
        samesite="lax",
    )
    return response


def _csrf(request, principal):
    if principal.session_id is not None and not verify_token(
        request.app.state.settings.session_secret,
        request.cookies.get("bbbffl_csrf"),
        request.headers.get("X-CSRF-Token"),
    ):
        raise HTTPException(403, "Invalid CSRF token")
