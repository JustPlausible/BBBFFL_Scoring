"""JSON read and operator endpoints for season-model SuperScore results."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import (
    Principal,
    require_authenticated,
    require_role_covers_season,
    require_round_reviewer,
    resolve_principal,
)
from app.csrf import verify_token
from app.superscore_results import (
    CompletedSeasonError,
    StaleSuperScoreEvidenceError,
    StaleSuperScorePublicationError,
    SuperScoreResultError,
)
from app.superscore_round import SuperScoreRoundError, advance_round_to_review

router = APIRouter(prefix="/api/season-superscore")


class PublishRequest(BaseModel):
    reason: str | None = None


class AdvanceToReviewRequest(BaseModel):
    reason: str | None = None


def _round(request, round_id):
    row = request.app.state.database.execute(
        "SELECT r.*,c.season_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=? AND c.stream_type='superscore'",
        (round_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Unknown SuperScore round")
    return row


def _csrf(request: Request, principal: Principal) -> None:
    """Apply the shared double-submit check only to cookie sessions."""
    if principal.session_id is not None and not verify_token(
        request.app.state.settings.session_secret,
        request.cookies.get("bbbffl_csrf"),
        request.headers.get("X-CSRF-Token"),
    ):
        raise HTTPException(403, "Invalid CSRF token")


def _operator_actor(principal: Principal) -> ActorContext:
    return ActorContext("anonymous_operator", principal.coach_id, principal.role.value)


def serialize_public_leaderboard(leaderboard: dict) -> dict:
    """Explicit public contract; publication/audit provenance is private."""
    return {
        "kind": leaderboard["kind"],
        "bbbffl_round_id": leaderboard["bbbffl_round_id"],
        "version": leaderboard["version"],
        "published_at": leaderboard["published_at"],
        "entries": [
            {
                "season_entry_id": entry["season_entry_id"],
                "team_name": entry["team_name"],
                "total_score": entry["total_score"],
                "rank": entry["rank"],
                "is_joint_winner": entry["is_joint_winner"],
            }
            for entry in leaderboard["entries"]
        ],
    }


@router.get("/rounds/{round_id}/leaderboard")
def public_leaderboard(round_id: str, request: Request):
    _round(request, round_id)
    result = request.app.state.superscore_results.leaderboard(round_id)
    if result is None:
        raise HTTPException(404, "No published SuperScore leaderboard")
    return serialize_public_leaderboard(result)


@router.get("/coach/rounds/{round_id}/leaderboard")
def coach_leaderboard(round_id: str, request: Request, principal: Principal = Depends(resolve_principal)):
    require_authenticated(principal)
    row = _round(request, round_id)
    require_role_covers_season(request, principal, row["season_id"])
    result = request.app.state.superscore_results.leaderboard(round_id)
    if result is None:
        raise HTTPException(404, "No published SuperScore leaderboard")
    return result


@router.get("/scorer/rounds/{round_id}/leaderboard")
def scorer_leaderboard(round_id: str, request: Request, principal: Principal = Depends(require_round_reviewer)):
    row = _round(request, round_id)
    require_role_covers_season(request, principal, row["season_id"])
    return {
        "current": request.app.state.superscore_results.leaderboard(round_id, include_inputs=True),
        "history": request.app.state.superscore_results.history(round_id, include_inputs=True),
    }


@router.post("/scorer/rounds/{round_id}/calculate")
def calculate(round_id: str, request: Request, principal: Principal = Depends(require_round_reviewer)):
    row = _round(request, round_id)
    require_role_covers_season(request, principal, row["season_id"])
    _csrf(request, principal)
    try:
        return request.app.state.superscore_results.calculations.calculate_round(round_id)
    except CompletedSeasonError as exc:
        raise HTTPException(423, str(exc)) from exc


@router.post("/scorer/rounds/{round_id}/advance-to-review")
def advance_to_review(
    round_id: str,
    payload: AdvanceToReviewRequest,
    request: Request,
    principal: Principal = Depends(require_round_reviewer),
):
    """The SuperScore `open -> live -> review` progression action (issue
    #208 review finding): `SuperScoreLeaderboardService.publish`/`_persist`
    require the round already be `review` or `final`, but nothing on the
    HTTP surface could reach `app.superscore_round.advance_round_to_review`
    before this route existed -- only `scripts/superscore_round_2026.py`
    could, so the browser Scorer workflow always 409'd on publish once a
    round opened. Idempotent against a round already at `review`/`final`
    (mirrors `advance_round_to_review`'s own idempotency)."""
    row = _round(request, round_id)
    require_role_covers_season(request, principal, row["season_id"])
    _csrf(request, principal)
    try:
        result = advance_round_to_review(
            request.app.state.database, round_id, actor=_operator_actor(principal), reason=payload.reason
        )
    except CompletedSeasonError as exc:
        raise HTTPException(423, str(exc)) from exc
    except SuperScoreRoundError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"round_id": round_id, "state": result.state}


@router.post("/scorer/rounds/{round_id}/publish")
def publish(
    round_id: str,
    payload: PublishRequest,
    request: Request,
    principal: Principal = Depends(require_round_reviewer),
):
    row = _round(request, round_id)
    require_role_covers_season(request, principal, row["season_id"])
    _csrf(request, principal)
    try:
        return request.app.state.superscore_results.publish(
            round_id, actor=_operator_actor(principal), reason=payload.reason
        )
    except StaleSuperScoreEvidenceError as exc:
        raise HTTPException(503, str(exc)) from exc
    except CompletedSeasonError as exc:
        raise HTTPException(423, str(exc)) from exc
    except (StaleSuperScorePublicationError, SuperScoreResultError) as exc:
        raise HTTPException(409, str(exc)) from exc
