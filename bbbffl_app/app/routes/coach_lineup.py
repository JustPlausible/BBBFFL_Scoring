"""Server-rendered, authenticated coach weekly-selection surface."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.coach_lineup import (
    COACH_LINEUP_POSITION_GROUPS,
    COACH_LINEUP_POSITIONS,
    EXPECTED_COACH_LINEUP_ERRORS,
    CoachLineupService,
)
from app.config import BASE_DIR
from app.csrf import verify_token
from app.lineup_validation import LineupValidationError
from app.routes.auth import _attach_csrf_cookie, _issue_csrf_token, _parse_form, get_current_coach

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def _service(request):
    return CoachLineupService(request.app.state.database, request.app.state.afl_client)


def _render(request, season_id, round_id, coach, *, positions=None, errors=(), validation=None, status=200):
    context = _service(request).view(coach.coach_id, season_id, round_id, positions=positions, validation=validation)
    if context is None:
        return HTMLResponse("Private lineup not found", status_code=404)
    token = _issue_csrf_token(request)
    notice = request.query_params.get("notice")
    response = templates.TemplateResponse(
        request,
        "coach_lineup.html",
        {
            "coach": coach,
            "lineup": context,
            "positions": COACH_LINEUP_POSITIONS,
            "position_groups": COACH_LINEUP_POSITION_GROUPS,
            "errors": errors,
            "notice": notice,
            "csrf_token": token,
        },
        status_code=status,
    )
    _attach_csrf_cookie(request, response, token)
    return response


@router.get("/coach/seasons/{season_id}/rounds/{round_id}/lineup", response_class=HTMLResponse)
def lineup_page(request: Request, season_id: str, round_id: str):
    coach = get_current_coach(request)
    if coach is None:
        return RedirectResponse(f"/login?next=/coach/seasons/{season_id}/rounds/{round_id}/lineup", status_code=303)
    return _render(request, season_id, round_id, coach)


@router.post("/coach/seasons/{season_id}/rounds/{round_id}/lineup", response_class=HTMLResponse)
async def lineup_action(request: Request, season_id: str, round_id: str):
    coach = get_current_coach(request)
    if coach is None:
        return RedirectResponse("/login", status_code=303)
    form = await _parse_form(request)
    positions = {position: form.get(f"position_{position}") or None for position in COACH_LINEUP_POSITIONS}
    if not verify_token(
        request.app.state.settings.session_secret,
        request.cookies.get("bbbffl_csrf"),
        form.get("csrf_token"),
    ):
        return _render(
            request,
            season_id,
            round_id,
            coach,
            positions=positions,
            errors=("The form expired. Try again.",),
            status=403,
        )
    service = _service(request)
    entry = service.resolve(coach.coach_id, season_id, round_id)
    if entry is None:
        return HTMLResponse("Private lineup not found", status_code=404)
    try:
        revision = int(form.get("draft_revision", "-1"))
        draft = service.save(season_id, round_id, entry, positions, revision)
        if form.get("action") == "save":
            return RedirectResponse(
                f"/coach/seasons/{season_id}/rounds/{round_id}/lineup?notice=draft-saved", status_code=303
            )
        if form.get("action") != "submit":
            raise ValueError("Choose Save Draft or Submit")
        service.submit(draft, int(form.get("submission_version", "0")), coach.coach_id)
        return RedirectResponse(
            f"/coach/seasons/{season_id}/rounds/{round_id}/lineup?notice=submitted", status_code=303
        )
    except LineupValidationError as exc:
        return _render(
            request,
            season_id,
            round_id,
            coach,
            positions=positions,
            errors=tuple(_message_text(m) for m in exc.result.errors),
            validation=exc.result,
            status=422,
        )
    except EXPECTED_COACH_LINEUP_ERRORS as exc:
        # Preserve the submitted choices, but translate expected domain failures
        # into a coach-safe page rather than leaking a database traceback.
        return _render(request, season_id, round_id, coach, positions=positions, errors=(str(exc),), status=409)


def _message_text(message):
    labels = {
        "player_selected_multiple_times": "This player is selected in more than one position.",
        "player_not_owned": "This player is not in your authoritative owned squad.",
        "season_player_invalid": "This player does not belong to this season.",
    }
    prefix = f"{message.position}: " if message.position else ""
    return prefix + labels.get(message.code, message.code.replace("_", " ").capitalize())
