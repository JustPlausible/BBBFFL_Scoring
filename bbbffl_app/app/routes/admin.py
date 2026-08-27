"""Scorer/admin controls.

Deliberately small: mark/unmark DNP, assign/clear interchange, set/clear a
direct score override with a reason, and finalise. All actions are
reversible until the Grand Final is finalised, at which point the decisions
store is locked (HTTP 423) so results become stable for historical
inspection.

If BBBFFL_ADMIN_TOKEN is set, every endpoint here requires a matching
`X-Admin-Token` header. This is a lightweight gate suitable for a single
trusted scorer on a home-server prototype, not general-purpose auth.

Every mutation below also records an immutable audit event in the same
transaction as its domain write (see app/audit.py and
docs/audit-events.md). GET /audit-events is a tiny read-only diagnostic
surface over that trail -- not an audit UI.
"""

import dataclasses

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import BASE_DIR
from app.scoring import SCORABLE_POSITIONS
from app.scorer_decisions import finalize as finalize_result
from app.scorer_decisions import set_dnp as apply_dnp_decision
from app.scorer_decisions import set_interchange as apply_interchange_decision
from app.scorer_decisions import set_override as apply_override_decision
from app.service import build_matchup_state, get_matchup_view
from app.superscore import superscore_round_label

router = APIRouter(prefix="/api/admin")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


class DnpRequest(BaseModel):
    team_key: str
    slot: str
    dnp: bool


class InterchangeRequest(BaseModel):
    team_key: str
    target_position: str | None = None


class OverrideRequest(BaseModel):
    team_key: str
    position: str
    override_score: float | None = None
    reason: str | None = None


class FinalizeRequest(BaseModel):
    note: str | None = None


def require_admin(request: Request, x_admin_token: str | None = Header(default=None)) -> None:
    settings = request.app.state.settings
    if settings.admin_token and x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Token")


def _team_keys(request: Request) -> set[str]:
    return {t.team_key for t in request.app.state.teams}


def _current_state(request: Request) -> dict:
    state = request.app.state
    return get_matchup_view(state.afl_client, state.teams, state.decisions, state.identity_cache)


@router.get("/state", dependencies=[Depends(require_admin)])
def admin_state(request: Request):
    return _current_state(request)


@router.post("/dnp", dependencies=[Depends(require_admin)])
def set_dnp(payload: DnpRequest, request: Request):
    apply_dnp_decision(
        request.app.state.decisions, _team_keys(request), payload.team_key, payload.slot, payload.dnp
    )
    return _current_state(request)


@router.post("/interchange", dependencies=[Depends(require_admin)])
def set_interchange(payload: InterchangeRequest, request: Request):
    apply_interchange_decision(
        request.app.state.decisions, _team_keys(request), payload.team_key, payload.target_position
    )
    return _current_state(request)


@router.post("/override", dependencies=[Depends(require_admin)])
def set_override(payload: OverrideRequest, request: Request):
    apply_override_decision(
        request.app.state.decisions,
        _team_keys(request),
        payload.team_key,
        payload.position,
        payload.override_score,
        payload.reason,
    )
    return _current_state(request)


@router.post("/finalize", dependencies=[Depends(require_admin)])
def finalize(payload: FinalizeRequest, request: Request):
    state = request.app.state
    # Computed exactly once here and passed to finalize_result() as the
    # frozen snapshot -- see app.scorer_decisions.finalize's docstring for
    # why a second afl-api round trip after the write commits would be
    # unsafe.
    result = build_matchup_state(state.afl_client, state.teams, state.decisions, state.identity_cache)
    finalize_result(result, state.decisions, payload.note)
    return _current_state(request)


@router.get("/audit-events", dependencies=[Depends(require_admin)])
def list_audit_events(
    request: Request,
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    correlation_id: str | None = None,
    limit: int = 200,
):
    """Tiny read-only diagnostic surface over the audit trail -- proves the
    append-only boundary end-to-end, not an admin audit UI. Deterministic
    chronological order (see AuditEventRepository.list_events)."""
    events = request.app.state.audit_events.list_events(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        correlation_id=correlation_id,
        limit=limit,
    )
    return [dataclasses.asdict(event) for event in events]


@page_router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    superscore_config = request.app.state.superscore_config
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "positions": list(SCORABLE_POSITIONS),
            "teams": request.app.state.teams,
            "superscore_enabled": superscore_config is not None,
            "superscore_round_label": (
                superscore_round_label(superscore_config.afl_round) if superscore_config else None
            ),
        },
    )
