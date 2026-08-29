"""Private coach weekly-lineup draft API, guarded by shared policy."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.authorization import Principal, require_owned_season_entry, resolve_principal
from app.csrf import verify_token

router = APIRouter(prefix="/api/coach/lineups")


class DraftRequest(BaseModel):
    season_id: str
    competition_id: str
    round_id: str
    season_entry_id: str
    expected_revision: int
    positions: dict[str, str]


def _private_view(draft) -> dict:
    """Allowlisted private response; never serialize audit/session/contact state."""
    return {
        "lineup_id": draft.lineup_id,
        "season_id": draft.season_id,
        "competition_id": draft.competition_id,
        "round_id": draft.bbbffl_round_id,
        "season_entry_id": draft.season_entry_id,
        "revision": draft.revision,
        "positions": draft.positions,
    }


@router.get("/draft")
def get_draft(
    season_id: str,
    competition_id: str,
    round_id: str,
    season_entry_id: str,
    request: Request,
    principal: Principal = Depends(resolve_principal),
):
    require_owned_season_entry(request, principal, season_entry_id)
    draft = request.app.state.lineups.get_draft(season_id, competition_id, round_id, season_entry_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Private resource not found")
    return _private_view(draft)


@router.put("/draft")
def save_draft(
    payload: DraftRequest,
    request: Request,
    principal: Principal = Depends(resolve_principal),
):
    require_owned_season_entry(request, principal, payload.season_entry_id)
    csrf = request.headers.get("X-CSRF-Token")
    if not verify_token(
        request.app.state.settings.session_secret,
        request.cookies.get("bbbffl_csrf"),
        csrf,
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    draft = request.app.state.lineups.save_draft(
        payload.season_id,
        payload.competition_id,
        payload.round_id,
        payload.season_entry_id,
        payload.positions,
        expected_revision=payload.expected_revision,
    )
    return _private_view(draft)
