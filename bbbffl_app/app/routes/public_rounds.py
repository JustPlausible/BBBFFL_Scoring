"""Anonymous ordinary-season public landing page, round browser and Round
Centre/ladder routes (issue #161 extends issue #78's original Round Centre
with a season-wide round browser; issue #213 further extends the same
round browser from Round 20 through Finals Week 1, Finals Week 2, the
Preliminary Final and the Grand Final, with each finals week's concurrent
SuperScore round rendered beneath it -- all four stay allow-listed DTOs
built entirely from :mod:`app.public_rounds`/:mod:`app.public_finals`,
never a second/parallel public navigation surface)."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR
from app.public_finals import (
    build_public_finals_round,
    build_public_finals_week_by_number,
    build_public_season_sequence,
    finals_round_context,
)
from app.public_rounds import (
    build_public_ladder,
    build_public_round,
    build_public_round_by_number,
    build_public_season_rounds,
    latest_ordinary_season_id,
    round_stream_type,
)

router = APIRouter()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


@router.get("/", response_class=HTMLResponse)
def regular_season_home(request: Request):
    """Enter the latest ordinary season without requiring a UUID.

    Linking to the season URL deliberately delegates round choice to
    :func:`season_overview`, keeping one owner for that policy.
    """
    state = request.app.state
    season_id = latest_ordinary_season_id(state.database, state.seasons)
    if season_id:
        return RedirectResponse(f"/seasons/{season_id}", status_code=302)
    return templates.TemplateResponse(
        request,
        "regular_season_empty.html",
        {"superscore_enabled": request.app.state.superscore_config is not None},
    )


def _season_rounds(request, season_id):
    try:
        return build_public_season_rounds(request.app.state.database, request.app.state.seasons, season_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Public season not found") from exc


def _season_sequence(request, season_id):
    """The full ordinary-then-finals navigation sequence (issue #213) --
    what powers the round selector/previous-next controls and the
    season landing page's default round. `_season_rounds` above stays
    ordinary-only and unchanged: it is still what the ladder-by-round-
    number/ladder-by-round-id endpoints use, since the ladder itself is an
    ordinary-competition-only concept that does not extend into finals."""
    try:
        return build_public_season_sequence(request.app.state.database, request.app.state.seasons, season_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Public season not found") from exc


def _round(request, season_id, round_id):
    state = request.app.state
    stream = round_stream_type(state.database, round_id)
    try:
        if stream == "finals":
            result = build_public_finals_round(
                state.database, state.lifecycle, state.round_review, state.identities, state.afl_client, round_id
            )
        else:
            result = build_public_round(state.database, state.lifecycle, state.round_review, state.identities, round_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Public round not found") from exc
    if result["season_id"] != season_id:
        raise HTTPException(status_code=404, detail="Public round not found")
    return result


@router.get("/api/public/seasons/{season_id}/rounds")
def season_round_index(season_id: str, request: Request):
    index = _season_sequence(request, season_id)
    # `ordinary_competition_id`/`finals_competition_id` are internal join
    # keys (never previously part of any public payload); the round-by-
    # number/ladder routes below use them server-side only, so they are
    # deliberately not echoed to the client here. Each entry in `rounds`
    # now additionally carries `stream` ('ordinary'/'finals') and
    # `week_number` (finals only) -- purely additive beyond issue #161's
    # original shape, so an existing client ignoring the new fields still
    # sees exactly the ordinary-round list it always did.
    return {
        "season_id": index["season_id"],
        "rounds": index["rounds"],
        "default_round_number": index["default_round_number"],
    }


@router.get("/api/public/seasons/{season_id}/rounds/{round_number:int}")
def round_by_number(season_id: str, round_number: int, request: Request):
    index = _season_sequence(request, season_id)
    total = len(index["rounds"])
    if not 1 <= round_number <= total:
        raise HTTPException(status_code=404, detail="Public round not found")
    state = request.app.state
    slot = index["rounds"][round_number - 1]
    if slot["stream"] == "finals":
        result = build_public_finals_week_by_number(
            state.database,
            state.lifecycle,
            state.round_review,
            state.identities,
            state.afl_client,
            season_id,
            index["finals_competition_id"],
            slot["week_number"],
        )
    else:
        result = build_public_round_by_number(
            state.database,
            state.lifecycle,
            state.round_review,
            state.identities,
            state.fixtures,
            season_id,
            index["ordinary_competition_id"],
            round_number,
            index["total_ordinary_rounds"],
        )
    # Navigation is always resolved against the *full* ordinary-then-
    # finals sequence here, overriding whatever narrower bound each
    # stream-specific builder computed on its own -- this is the one
    # change that makes Round 20's "next" reach Finals Week 1 and Finals
    # Week 1's "previous" return to Round 20, without either builder
    # needing to know the other stream exists.
    result["round_number"] = round_number
    result["prev_round_number"] = round_number - 1 if round_number > 1 else None
    result["next_round_number"] = round_number + 1 if round_number < total else None
    return result


@router.get("/api/public/seasons/{season_id}/rounds/{round_number:int}/ladder")
def ladder_by_number(season_id: str, round_number: int, request: Request):
    index = _season_rounds(request, season_id)
    if not 1 <= round_number <= len(index["rounds"]):
        raise HTTPException(status_code=404, detail="Public round not found")
    return build_public_ladder(
        request.app.state.ladder, request.app.state.identities, index["competition_id"], round_number
    )


@router.get("/api/public/seasons/{season_id}/rounds/{round_id}")
def round_state(season_id: str, round_id: str, request: Request):
    return _round(request, season_id, round_id)


@router.get("/api/public/seasons/{season_id}/rounds/{round_id}/ladder")
def ladder_state(season_id: str, round_id: str, request: Request):
    # The ladder is an ordinary-competition-only concept -- a finals
    # round_id has no ladder of its own to show (its page shows the
    # bracket/SuperScore section instead, never this endpoint), so this
    # never silently substitutes the frozen Round 20 ladder or any other
    # round's state for one.
    if round_stream_type(request.app.state.database, round_id) != "ordinary":
        raise HTTPException(status_code=404, detail="Public round not found")
    public_round = _round(request, season_id, round_id)
    round_ = request.app.state.lifecycle.get_round(round_id)
    return build_public_ladder(
        request.app.state.ladder,
        request.app.state.identities,
        round_.competition_id,
        public_round["round_number"],
    )


@router.get("/seasons/{season_id}")
def season_overview(season_id: str, request: Request):
    """The season landing page: redirect to the current (else most recently
    published) round's canonical, round-number-keyed URL -- see
    ``build_public_season_sequence``'s default-round policy, which now
    tracks the season into the finals phase once Round 20 is finalised
    and Finals Week 1 has opened (issue #213)."""
    index = _season_sequence(request, season_id)
    if index["default_round_number"] is None:
        raise HTTPException(status_code=404, detail="Public season not found")
    return RedirectResponse(f"/seasons/{season_id}/rounds/{index['default_round_number']}", status_code=302)


@router.get("/seasons/{season_id}/rounds/{round_number:int}", response_class=HTMLResponse)
def season_round_browser_page(season_id: str, round_number: int, request: Request):
    """Canonical season/round browser page (issue #161, extended by #213
    through the finals phase): round selector, previous/next navigation,
    and either the round's five matchups plus ladder (an ordinary round)
    or the finals bracket/bye plus the concurrent SuperScore section (a
    finals week) -- the same page/template either way, never a separate
    finals navigation page."""
    index = _season_sequence(request, season_id)
    if not 1 <= round_number <= len(index["rounds"]):
        raise HTTPException(status_code=404, detail="Public round not found")
    return templates.TemplateResponse(
        request,
        "public_season_rounds.html",
        {
            "season_id": season_id,
            "round_number": round_number,
            # Known here without any client-side fetch (a round_number's
            # stream never changes) -- passed straight through so the
            # template can decide whether to poll *before* its first
            # fetch even runs, rather than only after that fetch has
            # already succeeded (issue #213 Codex follow-up: scheduling
            # the poll timer only on the success path meant a transient
            # first-load failure left the page stuck with no retry).
            "stream": index["rounds"][round_number - 1]["stream"],
            # A finals week has no per-matchup polling detail page of its
            # own to fall back on (unlike an ordinary round's matches,
            # which link to public_round_centre.html) -- issue #213 Codex
            # follow-up: without this, a spectator watching an in-progress
            # finals week never sees a later calculation, review
            # transition, published result or SuperScore publication
            # without a manual reload. Passed through unconditionally
            # (identical to public_round_centre.html's own context); the
            # template only actually polls on a finals round_number.
            "poll_interval_seconds": request.app.state.settings.poll_interval_seconds,
        },
    )


@router.get("/seasons/{season_id}/rounds/{round_id}", response_class=HTMLResponse)
def round_page(season_id: str, round_id: str, request: Request):
    """The existing detailed public result/player-evidence view (issue
    #78), unchanged for an ordinary round_id: the season/round browser
    links each matchup through to this page for its full lineup
    breakdown. A finals round_id has no per-matchup lineup-breakdown page
    of its own (its bracket/bye/SuperScore content lives entirely on the
    canonical round-number page) -- issue #213 redirects it there rather
    than either 404ing or trying to force finals data through a template
    built around exactly five ordinary matchups."""
    state = request.app.state
    if round_stream_type(state.database, round_id) == "finals":
        context = finals_round_context(state.database, round_id)
        if context is None or context["season_id"] != season_id:
            raise HTTPException(status_code=404, detail="Public round not found")
        season = state.seasons.get_season(season_id)
        if season is None:
            raise HTTPException(status_code=404, detail="Public round not found")
        round_number = season.regular_season_round_count + context["week_number"]
        return RedirectResponse(f"/seasons/{season_id}/rounds/{round_number}", status_code=302)
    _round(request, season_id, round_id)
    return templates.TemplateResponse(
        request,
        "public_round_centre.html",
        {
            "season_id": season_id,
            "round_id": round_id,
            "poll_interval_seconds": request.app.state.settings.poll_interval_seconds,
        },
    )
