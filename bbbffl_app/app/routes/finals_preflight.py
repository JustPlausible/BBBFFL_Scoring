"""Issue #190: HTTP surface for the finals-specific preflight/open-round
adapter (`app.finals_preflight`) -- the route-layer sibling
`docs/2026-finals-superscore-design.md` requires alongside
`app/routes/round_preflight.py`'s own `stream_type='ordinary'`-scoped
surface, never a modification of it."""

import json

from fastapi import APIRouter, Depends, HTTPException, Request

from app.audit import ActorContext
from app.authorization import Principal, require_capability, require_role_covers_season
from app.csrf import verify_token
from app.finals import DownstreamPlayStateError, FinalsBracketError, FinalsBracketRepository
from app.finals_preflight import build_finals_week_preflight, open_finals_week

router = APIRouter(prefix="/api/admin/finals")
require_finals_operator = require_capability("roundsetup.manage")


def _actor(principal: Principal) -> ActorContext:
    return ActorContext(actor_type="anonymous_operator", actor_id=principal.coach_id, actor_role=principal.role.value)


def _authorise_bracket(request: Request, principal: Principal, bracket_id: str):
    bracket = FinalsBracketRepository(request.app.state.database).get_bracket_by_id(bracket_id)
    if bracket is None:
        raise HTTPException(404, "Unknown finals bracket")
    require_role_covers_season(request, principal, bracket.season_id)
    return bracket


def _csrf(request: Request, principal: Principal) -> None:
    if principal.session_id is not None and not verify_token(
        request.app.state.settings.session_secret,
        request.cookies.get("bbbffl_csrf"),
        request.headers.get("X-CSRF-Token"),
    ):
        raise HTTPException(403, "Invalid CSRF token")


def _parse_expected_versions(expected_versions: str | None) -> dict[str, int] | None:
    """`expected_versions` travels as a JSON-object query string, exactly
    like the CLI's own `--expected-versions` -- the map a prior preview
    call returned, to be re-checked under lock so a correction landing
    between an operator's preview and apply request is detected
    (`StaleFinalsResultError`) instead of silently authorising a derivation
    different from the one they reviewed (Codex review, PR #201)."""
    if not expected_versions:
        return None
    try:
        return json.loads(expected_versions)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "expected_versions must be a JSON object") from exc


@router.get("/{bracket_id}")
def view_bracket(bracket_id: str, request: Request, principal: Principal = Depends(require_finals_operator)):
    _authorise_bracket(request, principal, bracket_id)
    return FinalsBracketRepository(request.app.state.database).describe(bracket_id)


@router.get("/{bracket_id}/weeks/{week_number}")
def view_week_preflight(
    bracket_id: str, week_number: int, request: Request, principal: Principal = Depends(require_finals_operator)
):
    _authorise_bracket(request, principal, bracket_id)
    try:
        return build_finals_week_preflight(request.app.state.database, bracket_id, week_number)
    except KeyError as exc:
        raise HTTPException(404, "Unknown finals week") from exc


@router.post("/{bracket_id}/weeks/{week_number}/open")
def open_week(
    bracket_id: str, week_number: int, request: Request, principal: Principal = Depends(require_finals_operator)
):
    _authorise_bracket(request, principal, bracket_id)
    _csrf(request, principal)
    try:
        open_finals_week(request.app.state.database, bracket_id, week_number, actor=_actor(principal))
    except FinalsBracketError as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "Unknown finals week") from exc
    return build_finals_week_preflight(request.app.state.database, bracket_id, week_number)


@router.post("/{bracket_id}/advance/{from_week}")
def advance_week(
    bracket_id: str,
    from_week: int,
    request: Request,
    reason: str,
    expected_versions: str | None = None,
    principal: Principal = Depends(require_finals_operator),
):
    _authorise_bracket(request, principal, bracket_id)
    _csrf(request, principal)
    repo = FinalsBracketRepository(request.app.state.database)
    try:
        result = repo.advance_bracket(
            bracket_id,
            from_week,
            actor=_actor(principal),
            reason=reason,
            expected_versions=_parse_expected_versions(expected_versions),
        )
    except FinalsBracketError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"bracket_id": bracket_id, **{k: v for k, v in result.items() if k != "bracket_id"}}


@router.post("/{bracket_id}/rewind/{from_week}")
def rewind_week(
    bracket_id: str,
    from_week: int,
    request: Request,
    reason: str,
    apply: bool = False,
    expected_versions: str | None = None,
    principal: Principal = Depends(require_finals_operator),
):
    _authorise_bracket(request, principal, bracket_id)
    _csrf(request, principal)
    repo = FinalsBracketRepository(request.app.state.database)
    try:
        return repo.rewind_bracket(
            bracket_id,
            from_week,
            actor=_actor(principal),
            reason=reason,
            apply=apply,
            expected_versions=_parse_expected_versions(expected_versions),
        )
    except DownstreamPlayStateError as exc:
        raise HTTPException(409, {"message": str(exc), "report": exc.report}) from exc
    except FinalsBracketError as exc:
        raise HTTPException(409, str(exc)) from exc
