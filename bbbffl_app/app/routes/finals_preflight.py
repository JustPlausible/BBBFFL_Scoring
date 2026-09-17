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
from app.finals import DownstreamPlayStateError, FinalsBracketError, FinalsBracketRepository, StaleFinalsResultError
from app.finals_preflight import build_finals_week_preflight, open_finals_week
from app.finals_review import correct_finals_result, publish_finals_round
from app.finals_superscore_open import (
    FrozenMappingDivergedError,
    LockoutPlanDivergedError,
    PairedOpenWeekError,
    open_finals_and_superscore_week,
)
from app.routes.round_review import require_round_reviewer
from app.superscore_round import SuperScoreRoundError

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
    different from the one they reviewed (Codex review, PR #201). Rejects
    (400) anything that isn't a JSON object of {string: integer} -- a JSON
    array or scalar would otherwise reach `_lock_matchup_version` and raise
    an uncaught `AttributeError` on `.get()`, and JSON `null` would
    silently decode to `None` and disable the staleness guard the caller
    explicitly asked for, rather than reporting the malformed input
    (Codex review, PR #201)."""
    if not expected_versions:
        return None
    try:
        parsed = json.loads(expected_versions)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "expected_versions must be a JSON object of {matchup_id: integer_version}") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool) for key, value in parsed.items()
    ):
        raise HTTPException(400, "expected_versions must be a JSON object of {matchup_id: integer_version}")
    return parsed


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


@router.post("/{bracket_id}/weeks/{week_number}/open-paired")
def open_week_paired(
    bracket_id: str, week_number: int, request: Request, principal: Principal = Depends(require_finals_operator)
):
    """Issue #211 workflow improvement B: the single paired web "Open week"
    action -- validates the finals/SuperScore pairing, synchronises SS's
    lockout plan from the finals week, then opens each stream's own
    lifecycle separately (`app.finals_superscore_open.
    open_finals_and_superscore_week`). Never a third, combined lifecycle:
    the response simply reports both streams' own independent state."""
    _authorise_bracket(request, principal, bracket_id)
    _csrf(request, principal)
    try:
        return open_finals_and_superscore_week(
            request.app.state.database,
            request.app.state.afl_client,
            bracket_id,
            week_number,
            actor=_actor(principal),
        )
    except KeyError as exc:
        raise HTTPException(404, "Unknown finals week") from exc
    except PairedOpenWeekError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (FinalsBracketError, SuperScoreRoundError) as exc:
        raise HTTPException(409, str(exc)) from exc
    except (LockoutPlanDivergedError, FrozenMappingDivergedError) as exc:
        # Issue #211 P2 (Codex review, round 2): both are expected,
        # operator-resolvable "the SS lockout plan/mapping cannot be
        # safely auto-synchronised" outcomes (a stale SS-only trigger key,
        # an unreconcilable sequence cycle, or a frozen mapping divergence)
        # -- without this, they fell through to an uncaught 500 instead of
        # the actionable 409 every other paired-open conflict returns.
        raise HTTPException(409, str(exc)) from exc


@router.post("/{bracket_id}/weeks/{week_number}/publish")
def publish_week(
    bracket_id: str,
    week_number: int,
    request: Request,
    reason: str | None = None,
    principal: Principal = Depends(require_round_reviewer),
):
    bracket = _authorise_bracket(request, principal, bracket_id)
    _csrf(request, principal)
    week = request.app.state.database.execute(
        "SELECT bbbffl_round_id FROM finals_bracket_week WHERE bracket_id=? AND week_number=?",
        (bracket.bracket_id, week_number),
    ).fetchone()
    if week is None:
        raise HTTPException(404, "Unknown finals week")
    try:
        published = publish_finals_round(
            request.app.state.database,
            request.app.state.afl_client,
            request.app.state.lifecycle,
            request.app.state.round_review,
            request.app.state.identities,
            week["bbbffl_round_id"],
            actor=_actor(principal),
            reason=reason,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"round_id": published.bbbffl_round_id, "state": published.state, "week_number": week_number}


@router.post("/{bracket_id}/weeks/{week_number}/advance-to-review")
def advance_week_to_review(
    bracket_id: str,
    week_number: int,
    request: Request,
    reason: str | None = None,
    principal: Principal = Depends(require_round_reviewer),
):
    """The finals-week `open -> live -> review` progression action (issue
    #208): before this route existed, nothing on the HTTP surface could
    reach `FinalsBracketRepository.advance_week_to_review` at all, so a
    finals week's publish action (`.../publish`, below) -- which requires
    the round to already be `review` -- was unreachable through any
    supported operator workflow once a week had opened. Idempotent against
    a week already at `review`/`final` (`already_advanced` reports which)."""
    _authorise_bracket(request, principal, bracket_id)
    _csrf(request, principal)
    repo = FinalsBracketRepository(request.app.state.database)
    try:
        result = repo.advance_week_to_review(bracket_id, week_number, actor=_actor(principal), reason=reason)
    except KeyError as exc:
        raise HTTPException(404, "Unknown finals week") from exc
    except FinalsBracketError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "round_id": result["round"].bbbffl_round_id,
        "state": result["round"].state,
        "already_advanced": result["already_advanced"],
    }


@router.post("/{bracket_id}/matchups/{matchup_id}/correct")
def correct_result(
    bracket_id: str,
    matchup_id: str,
    request: Request,
    reason: str,
    principal: Principal = Depends(require_round_reviewer),
):
    bracket = _authorise_bracket(request, principal, bracket_id)
    _csrf(request, principal)
    belongs = request.app.state.database.execute(
        "SELECT 1 FROM finals_bracket_pairing WHERE bracket_id=? AND matchup_id=? AND status='active'",
        (bracket.bracket_id, matchup_id),
    ).fetchone()
    if belongs is None:
        raise HTTPException(404, "Unknown active finals matchup")
    try:
        result = correct_finals_result(
            request.app.state.database,
            request.app.state.afl_client,
            request.app.state.lifecycle,
            request.app.state.round_review,
            request.app.state.identities,
            matchup_id,
            actor=_actor(principal),
            reason=reason,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "matchup_id": result.matchup_id,
        "version": result.version,
        "home_score": result.home_score,
        "away_score": result.away_score,
    }


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
    except (FinalsBracketError, StaleFinalsResultError) as exc:
        # Codex review, PR #201: `StaleFinalsResultError` inherits from
        # `RuntimeError`, not `FinalsBracketError` -- without catching it
        # explicitly here, a correction landing between an operator's
        # preview and apply request produced an uncaught 500 instead of the
        # advertised 409, despite the transaction having safely rolled back.
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
    except (FinalsBracketError, StaleFinalsResultError) as exc:
        # See advance_week's identical except clause: StaleFinalsResultError
        # inherits from RuntimeError, not FinalsBracketError.
        raise HTTPException(409, str(exc)) from exc
