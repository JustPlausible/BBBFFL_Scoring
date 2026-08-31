from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import Principal, require_capability, require_role_covers_season
from app.config import BASE_DIR
from app.csrf import issue_token, verify_token
from app.lockouts import LockoutTriggerRepository
from app.round_mapping import AflApiReferenceValidator, RoundMappingRepository
from app.round_preflight import build_round_preflight

router = APIRouter(prefix="/api/admin/round-preflight")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
require_round_operator = require_capability("roundsetup.manage")


class MappingRequest(BaseModel):
    afl_season_id: int
    afl_round_id: int
    reason: str | None = None


class TriggerRequest(BaseModel):
    trigger_key: str
    trigger_type: str
    sequence: int
    afl_match_ids: list[int]
    reason: str | None = None


def _actor(principal):
    return ActorContext(actor_type="authenticated_coach", actor_id=principal.coach_id, actor_role=principal.role.value)


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


@router.get("/{round_id}")
def view(round_id: str, request: Request, principal: Principal = Depends(require_round_operator)):
    _authorise(request, principal, round_id)
    return _view(request, round_id)


@router.post("/{round_id}/mapping")
def accept_mapping(
    round_id: str, payload: MappingRequest, request: Request, principal: Principal = Depends(require_round_operator)
):
    _authorise(request, principal, round_id)
    _csrf(request, principal)
    repo = RoundMappingRepository(request.app.state.database)
    existing = repo.resolve(round_id)
    if existing:
        repo.correct(
            round_id,
            payload.afl_season_id,
            payload.afl_round_id,
            AflApiReferenceValidator(request.app.state.afl_client),
            reason=payload.reason or "Mapping corrected in round preflight",
            actor=_actor(principal),
        )
    else:
        repo.accept(
            round_id,
            payload.afl_season_id,
            payload.afl_round_id,
            AflApiReferenceValidator(request.app.state.afl_client),
            reason=payload.reason or "Mapping accepted in round preflight",
            actor=_actor(principal),
        )
    return _view(request, round_id)


@router.post("/{round_id}/lockout-trigger")
def configure_trigger(
    round_id: str, payload: TriggerRequest, request: Request, principal: Principal = Depends(require_round_operator)
):
    _authorise(request, principal, round_id)
    _csrf(request, principal)
    repo = LockoutTriggerRepository(request.app.state.database)
    existing = repo.get(round_id, payload.trigger_key)
    if existing:
        repo.replace(
            round_id,
            payload.trigger_key,
            trigger_type=payload.trigger_type,
            sequence=payload.sequence,
            afl_match_ids=payload.afl_match_ids,
            reason=payload.reason or "Lockout plan revised in preflight",
            actor=_actor(principal),
        )
    else:
        repo.create(
            round_id,
            payload.trigger_key,
            payload.trigger_type,
            payload.sequence,
            payload.afl_match_ids,
            reason=payload.reason or "Lockout plan configured in preflight",
            actor=_actor(principal),
        )
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
    persisted = state.lifecycle.get_round(round_id)
    if persisted is None:
        state.lifecycle.create_ordinary_round(
            round_id, actor=actor, reason="Round context frozen after successful operator preflight"
        )
    state.lifecycle.transition(
        round_id, "open", actor=actor, reason="Explicit Open Round action after successful preflight"
    )
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


def _csrf(request, principal):
    if principal.session_id is not None and not verify_token(
        request.app.state.settings.session_secret,
        request.cookies.get("bbbffl_csrf"),
        request.headers.get("X-CSRF-Token"),
    ):
        raise HTTPException(403, "Invalid CSRF token")
