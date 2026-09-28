"""Season completion (issue #240): the Scorer/Administrator browser gate for
production season completion, plus a production-safe, read-only archival
verifier.

`docs/2027-live-season-readiness.md`'s remaining item 4: `app.season_
completion.preview_complete_season`/`complete_season` and `app.season_
archival.verify_season_completed_for_archival` are fully domain- and
test-proven, but the only wired entry points before this issue were the
2026-replay-only operator CLIs (`scripts/season_completion_2026.py`,
`scripts/season_archival_checkpoint_2026.py`), both of which refuse
outright under `BBBFFL_ENVIRONMENT=production` -- correctly, since they are
2026-replay tooling, not live-season tooling (see each script's own module
docstring). This module adds the missing production-safe browser surface,
mirroring `app.routes.season_activation`'s exact shape for the sibling
`setup -> active` gate: a thin HTTP translation layer only. Every readiness
rule, refusal and the atomic transition itself live in `app.season_
completion`; every archival read lives in `app.season_archival`. This
module reimplements neither -- it does not reopen either module's
docstring-declared scope, and it does not touch the 2026 scripts' own
production guards, which remain exactly as strict as before.

Authorization matches `app.routes.season_activation`: Scorer or
Administrator only (`app.authorization.require_scorer_or_admin`), plus the
existing season-scoped `require_role_covers_season` check every other
season-model route uses. Every write requires an explicit reason and --
for a cookie-authenticated session -- the double-submit CSRF token. The
archival-verification read is a plain, unauthenticated-of-side-effects GET:
it never mutates, so it carries no CSRF requirement, exactly like
`preview_complete_season`'s own read-only preview.
"""

from __future__ import annotations

import dataclasses

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.audit import ActorContext
from app.authorization import Principal, require_role_covers_season, require_scorer_or_admin, resolve_principal
from app.config import BASE_DIR
from app.csrf import issue_token, verify_token
from app.season_archival import ArchivalVerification, verify_season_completed_for_archival
from app.season_completion import CompletionResult, complete_season, preview_complete_season

router = APIRouter(prefix="/api/scorer/season-completion")
page_router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


class CompleteRequest(BaseModel):
    reason: str | None = None


def require_completion_operator(principal: Principal = Depends(resolve_principal)) -> Principal:
    """Route-level `Depends`-ready wrapper, matching `app.routes.
    season_activation`'s identical local-wrapper convention."""
    return require_scorer_or_admin(principal)


def _actor(principal: Principal) -> ActorContext:
    return ActorContext(actor_type="anonymous_operator", actor_id=principal.coach_id, actor_role=principal.role.value)


def _require_session_csrf(request: Request, principal: Principal) -> None:
    """Cookie-authenticated writes need CSRF; header-token writes do not --
    see `app.routes.season_activation`'s identical helper."""
    if principal.session_id is not None and not verify_token(
        request.app.state.settings.session_secret,
        request.cookies.get("bbbffl_csrf"),
        request.headers.get("X-CSRF-Token"),
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _require_known_season(request: Request, season_id: str) -> None:
    """404 for an unknown season before any role-scope check -- matches
    `app.routes.season_activation`'s `_require_known_season` convention."""
    if request.app.state.seasons.get_season(season_id) is None:
        raise HTTPException(status_code=404, detail="Unknown season")


def _team_name(request: Request, season_entry_id: str | None) -> str | None:
    """Presentation-only enrichment reusing the existing public-team read
    model (`app.identity.IdentityRepository.get_public_team`) -- the same
    reuse `app.finals_superscore_dashboard.build_finals_progression_preview`
    already relies on to resolve team names for a browser preview, never a
    second identity/team-name derivation."""
    if season_entry_id is None:
        return None
    team = request.app.state.identities.get_public_team(season_entry_id)
    return team.team_name if team is not None else None


def _completion_payload(request: Request, result: CompletionResult) -> dict:
    return {
        "season": dataclasses.asdict(result.season),
        "completed_season_version": result.completed_season_version,
        "completion_event_id": result.completion_event_id,
        "premiership": {
            "award_id": result.premiership_award.award_id,
            "season_entry_id": result.premiership_award.season_entry_id,
            "team_name": _team_name(request, result.premiership_award.season_entry_id),
            "created": result.premiership_created,
        },
        "wooden_spoon": {
            "award_id": result.wooden_spoon_award.award_id,
            "season_entry_id": result.wooden_spoon_award.season_entry_id,
            "team_name": _team_name(request, result.wooden_spoon_award.season_entry_id),
            "created": result.wooden_spoon_created,
        },
    }


def _archival_payload(result: ArchivalVerification) -> dict:
    return dataclasses.asdict(result)


@router.get("/{season_id}")
def season_completion_readiness(
    season_id: str, request: Request, principal: Principal = Depends(require_completion_operator)
):
    _require_known_season(request, season_id)
    require_role_covers_season(request, principal, season_id)
    return preview_complete_season(request.app.state.database, season_id)


@router.post("/{season_id}/complete")
def season_completion_complete(
    season_id: str,
    payload: CompleteRequest,
    request: Request,
    principal: Principal = Depends(require_completion_operator),
):
    _require_known_season(request, season_id)
    require_role_covers_season(request, principal, season_id)
    _require_session_csrf(request, principal)
    database = request.app.state.database
    result = complete_season(database, season_id, actor=_actor(principal), reason=payload.reason)
    return _completion_payload(request, result)


@router.get("/{season_id}/archival-verification")
def season_completion_archival_verification(
    season_id: str,
    request: Request,
    expected_completion_event_id: str | None = None,
    principal: Principal = Depends(require_completion_operator),
):
    """Read-only: never locks a row, never mutates, never migrates the
    schema -- production-safe by construction, independent of the
    2026-replay-only `scripts/season_archival_checkpoint_2026.py`, which
    stays refused under `BBBFFL_ENVIRONMENT=production`."""
    _require_known_season(request, season_id)
    require_role_covers_season(request, principal, season_id)
    result = verify_season_completed_for_archival(
        request.app.state.database, season_id, expected_completion_event_id=expected_completion_event_id
    )
    return _archival_payload(result)


@page_router.get("/scorer/season-completion/{season_id}", response_class=HTMLResponse)
def season_completion_page(season_id: str, request: Request):
    token = issue_token(request.app.state.settings.session_secret)
    response = templates.TemplateResponse(
        request, "season_completion.html", {"season_id": season_id, "csrf_token": token}
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
