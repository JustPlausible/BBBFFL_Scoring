"""JSON read and operator endpoints for season-model SuperScore results."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import Principal, require_authenticated, require_role_covers_season, resolve_principal
from app.routes.round_review import require_round_reviewer

router = APIRouter(prefix="/api/season-superscore")


class PublishRequest(BaseModel):
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


@router.get("/rounds/{round_id}/leaderboard")
def public_leaderboard(round_id: str, request: Request):
    _round(request, round_id)
    result = request.app.state.superscore_results.leaderboard(round_id)
    if result is None:
        raise HTTPException(404, "No published SuperScore leaderboard")
    return result


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
    return request.app.state.superscore_results.calculations.calculate_round(round_id)


@router.post("/scorer/rounds/{round_id}/publish")
def publish(
    round_id: str,
    payload: PublishRequest,
    request: Request,
    principal: Principal = Depends(require_round_reviewer),
):
    row = _round(request, round_id)
    require_role_covers_season(request, principal, row["season_id"])
    return request.app.state.superscore_results.publish(
        round_id,
        actor=ActorContext("coach", principal.coach_id, principal.role.value),
        reason=payload.reason,
    )
