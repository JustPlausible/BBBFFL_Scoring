"""Draft Board application read service shared by HTTP and operator tools.

Repository mechanics remain in :mod:`app.draft`; callers inject the
repositories used here so this service owns orchestration without reaching
through the HTTP layer or creating a second draft model.

Issue #181 ("shared draft UX"): `entry_view`/`player_name`/`pick_view`/
`build_readiness`/`build_board` below are the *presentation* layer both
`app.routes.draft` (preseason) and `app.routes.midseason_draft` (once a
mid-season pick table exists) render from -- every one of them is
parameterised by `draft_kind` and reads straight from
`request.app.state.draft` (`app.draft.DraftRepository`), which already
tracks preseason and mid-season picks under the same schema
(`draft_kind` column). Nothing here decides turn order, availability or
validity: it only shapes what `DraftRepository`/`IdentityRepository`/
`PlayerPoolRepository` already return into human-readable view models.
"""

import dataclasses

from app.audit import AuditEventRepository
from app.authorization import Role


def resolve_my_entry_id(request, principal, season_id: str) -> str | None:
    """The season entry a signed-in principal should see private/self-
    service context for on a shared draft board (issue #181): a delegated
    (Scorer/Admin/etc.) active role's currently represented entry, or --
    for a Coach acting as themselves -- the entry their own coach identity
    owns in this season. `None` for a spectator/legacy-token principal, or
    a Coach with no entry in this particular season."""
    if principal.represented_season_entry_id:
        return principal.represented_season_entry_id
    if principal.role is Role.COACH and principal.coach_id:
        for entry in request.app.state.identities.list_entries(season_id):
            if entry.coach_id == principal.coach_id:
                return entry.season_entry_id
    return None


def draft_board_readiness(database, identities, draft, player_pool, season_id, *, draft_kind: str = "preseason"):
    """Return whether the existing board can execute its next human pick.

    `draft_kind` (issue #181, Codex review on PR #225, P1): without it this
    always read the *preseason* draft's status regardless of which board
    actually called in -- in the normal mid-season flow the preseason draft
    already exists and is finalized, so a mid-season caller would silently
    see `draft_not_paused`/`draft_not_finalized` computed from the wrong
    draft entirely (both trivially true), rather than its own mid-season
    draft's real state."""
    entries = identities.list_entries(season_id)
    status = draft.status(season_id, draft_kind=draft_kind)
    config = database.execute(
        "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?", (season_id,)
    ).fetchone()
    available_count = len(player_pool.list_available(season_id))
    total_required = status.total_picks if status else (len(entries) * config["squad_limit"] if config else 0)
    completed = status.completed_picks if status else 0
    remaining = max(total_required - completed, 0)
    checks = {
        "entries": len(entries) == 10,
        "order": status is not None and len(draft.order(season_id, draft_kind=draft_kind)) == 10,
        "players": total_required > 0 and available_count >= remaining,
        "squad": config is not None and config["squad_limit"] > 0,
        "draft_not_paused": status is not None and not status.is_paused,
        "draft_not_finalized": status is not None and not status.is_finalized,
    }
    next_pick = (
        draft.next_pick(season_id, draft_kind=draft_kind)
        if status and not status.is_paused and not status.is_finalized
        else None
    )
    return {
        "ready": all(checks.values()),
        "checks": checks,
        "available_player_count": available_count,
        "remaining_selection_count": remaining,
        "next_pick_overall": next_pick.overall_number if next_pick else None,
    }


def entry_view(request, entry_id: str, cache: dict) -> dict:
    """Resolve stable entry identity into public, human-facing labels --
    shared by every draft-kind's board (issue #151/#181)."""
    if entry_id not in cache:
        team = request.app.state.identities.get_public_team(entry_id)
        coach = request.app.state.identities.get_current_coach(entry_id)
        cache[entry_id] = {
            "season_entry_id": entry_id,
            "team_name": team.team_name if team else "Unknown team",
            "coach_display_name": coach.display_name if coach else "Coach not assigned",
        }
    return cache[entry_id]


def player_name(request, season_player_id: str | None, cache: dict) -> str | None:
    if season_player_id is None:
        return None
    if season_player_id not in cache:
        player = request.app.state.player_pool.get_by_id(season_player_id)
        cache[season_player_id] = player.display_name if player else season_player_id
    return cache[season_player_id]


def pick_view(request, pick, cache: dict, player_cache: dict, event_cache: dict) -> dict:
    current_identity = entry_view(request, pick.current_season_entry_id, cache)
    original_identity = entry_view(request, pick.original_season_entry_id, cache)
    player = (
        request.app.state.player_pool.get_by_id(pick.selected_season_player_id)
        if pick.selected_season_player_id
        else None
    )
    # Completion provenance is bulk-loaded once by `build_board`. Current and
    # upcoming picks cannot have a completion event, so never query for it.
    event = event_cache.get(pick.draft_pick_id) if pick.completed_at else None
    # A completed pick's audit event exists for *every* selection, coach
    # self-service included (issue #181, Codex review on PR #225, P2 --
    # revised in a later round to check the authoritative `actor_type`
    # rather than `actor_role`, which a genuine coach action now leaves
    # `None`, see `_pick_actor`/`ActorContext.coach`). A delegated proxy
    # always records `actor_type="anonymous_operator"` with its own active
    # role in `actor_role` (scorer/secretary/admin/replay_operator); only
    # that is a genuine proxy entry worth surfacing here.
    is_proxy = event is not None and event.actor_type != "coach"
    actor_name = None
    if is_proxy and event.actor_id:
        actor = request.app.state.identities.get_coach(event.actor_id)
        actor_name = actor.display_name if actor else event.actor_id
    return {
        "draft_pick_id": pick.draft_pick_id,
        "overall_number": pick.overall_number,
        "round": pick.draft_round,
        "round_position": pick.round_position,
        "original_season_entry_id": pick.original_season_entry_id,
        "original_team_name": original_identity["team_name"],
        "current_season_entry_id": pick.current_season_entry_id,
        "current_team_name": current_identity["team_name"],
        "current_coach_display_name": current_identity["coach_display_name"],
        "traded": pick.original_season_entry_id != pick.current_season_entry_id,
        "selected_season_player_id": pick.selected_season_player_id,
        "selected_player_name": player_name(request, pick.selected_season_player_id, player_cache),
        "selected_player_afl_team_name": player.afl_team_name if player else None,
        "completed_at": pick.completed_at,
        "proxy": (
            {
                "operator_name": actor_name or "Scorer",
                "operator_role": event.actor_role,
                "reason": event.reason,
                "on_behalf_of_team": current_identity["team_name"],
            }
            if is_proxy
            else None
        ),
    }


def build_readiness(request, season_id: str, *, draft_kind: str = "preseason") -> dict:
    database = request.app.state.database
    entries = request.app.state.identities.list_entries(season_id)
    status = request.app.state.draft.status(season_id, draft_kind=draft_kind)
    config = database.execute(
        "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?", (season_id,)
    ).fetchone()
    shared = draft_board_readiness(
        database,
        request.app.state.identities,
        request.app.state.draft,
        request.app.state.player_pool,
        season_id,
        draft_kind=draft_kind,
    )
    available_count = shared["available_player_count"]
    if status is not None:
        total_required = status.total_picks
        completed = status.completed_picks
    else:
        total_required = len(entries) * config["squad_limit"] if config is not None else 0
        completed = 0
    remaining_required = max(total_required - completed, 0)
    pool_sufficient = total_required > 0 and available_count >= remaining_required
    player_word = "player" if available_count == 1 else "players"
    player_verb = "is" if available_count == 1 else "are"
    selection_word = "selection" if remaining_required == 1 else "selections"
    checks = [
        {
            "key": "entries",
            "ready": len(entries) == 10,
            "label": "Ten BBBFFL teams",
            "detail": f"{len(entries)} of 10 season entries are present.",
        },
        {
            "key": "order",
            "ready": status is not None,
            "label": "Draft order accepted",
            "detail": "The frozen draft order is ready."
            if status
            else "Accept and freeze the draft order in season setup.",
        },
        {
            "key": "players",
            "ready": pool_sufficient,
            "label": "Season player pool",
            "detail": (
                f"{available_count} selectable {player_word} {player_verb} available for "
                f"{remaining_required} remaining {selection_word}."
                if pool_sufficient
                else f"Only {available_count} selectable {player_word} {player_verb} available; "
                f"{remaining_required} remaining {selection_word} are required."
            ),
        },
        {
            "key": "squad",
            "ready": config is not None and config["squad_limit"] > 0,
            "label": "Target squad size",
            "detail": f"Target: {config['squad_limit']} players per team."
            if config
            else "Configure the target squad size.",
        },
        {
            "key": "draft_not_paused",
            "ready": shared["checks"]["draft_not_paused"],
            "label": "Draft is operational",
            "detail": "The draft is not paused."
            if shared["checks"]["draft_not_paused"]
            else "The draft is paused; an operator must explicitly resume it before the next pick.",
        },
        {
            "key": "draft_not_finalized",
            "ready": shared["checks"]["draft_not_finalized"],
            "label": "Draft is not finalized",
            "detail": "The draft remains open for selections."
            if shared["checks"]["draft_not_finalized"]
            else "The draft is finalized and cannot accept a selection.",
        },
    ]
    return {"ready": shared["ready"], "checks": checks}


def build_board(request, season_id: str, *, draft_kind: str = "preseason") -> dict:
    """The shared conduct-draft board view model (issue #181): every field
    a board renders for either draft kind, built from the same
    `app.draft.DraftRepository` methods, scoped by `draft_kind`."""
    draft = request.app.state.draft
    status = draft.status(season_id, draft_kind=draft_kind)
    if status is None:
        raise KeyError(season_id)
    cache: dict = {}
    player_cache: dict = {}
    order = [
        {"position": position, **entry_view(request, entry_id, cache)}
        for position, entry_id in draft.order(season_id, draft_kind=draft_kind)
    ]
    all_picks = draft.picks(season_id, draft_kind=draft_kind)
    completed = [pick for pick in all_picks if pick.completed_at is not None]
    remaining = [pick for pick in all_picks if pick.completed_at is None]
    current = remaining[0] if remaining else None
    latest_completed = completed[-1] if completed else None
    completed_ids = {pick.draft_pick_id for pick in completed}
    event_cache = {
        event.entity_id: event
        for event in AuditEventRepository(request.app.state.database).list_events(action="draft.pick.completed")
        if event.entity_id in completed_ids
    }
    # A mid-season draft's `target_squad_size` (issue #181, Codex review on
    # PR #225, P2) is the season's uniform squad *limit*, not a uniform
    # picks-per-team count -- unlike the preseason snake draft, mid-season
    # picks are vacancy-based and vary per entry (`app.midseason_draft.
    # vacancy_allocations`), including entries with zero picks at all.
    # Each entry's own target is however many picks it was actually
    # allocated in `all_picks`, not the season-wide squad limit.
    target_counts = (
        {
            row["season_entry_id"]: sum(
                1 for pick in all_picks if pick.current_season_entry_id == row["season_entry_id"]
            )
            for row in order
        }
        if draft_kind == "midseason"
        else None
    )
    return {
        "season_id": season_id,
        "draft_kind": draft_kind,
        "status": dataclasses.asdict(status),
        "readiness": build_readiness(request, season_id, draft_kind=draft_kind),
        "order": order,
        "current_pick": pick_view(request, current, cache, player_cache, event_cache) if current else None,
        "upcoming_picks": [pick_view(request, pick, cache, player_cache, event_cache) for pick in remaining[1:]],
        "completed_picks": [pick_view(request, pick, cache, player_cache, event_cache) for pick in reversed(completed)],
        "team_progress": [
            {
                **identity,
                "drafted_count": sum(
                    1 for pick in completed if pick.current_season_entry_id == identity["season_entry_id"]
                ),
                "target_count": (
                    target_counts[identity["season_entry_id"]]
                    if target_counts is not None
                    else status.target_squad_size
                ),
            }
            for identity in (
                entry_view(request, entry_id, cache) for _, entry_id in draft.order(season_id, draft_kind=draft_kind)
            )
        ],
        "correctable_draft_pick_id": latest_completed.draft_pick_id if latest_completed else None,
        "corrections": [dataclasses.asdict(item) for item in draft.corrections(season_id, draft_kind=draft_kind)],
    }


def player_browse_view(
    request, season_id: str, *, draft_kind: str, query=None, availability=None, limit=200
) -> list[dict]:
    """The shared player-browser response (issue #181): `app.player_pool.
    PlayerPoolRepository.browse`'s existing availability/search/sort model,
    annotated with phase-appropriate scoring context from
    `app.player_stats_context.PlayerStatsContext` -- previous completed
    season for a preseason draft, current season to date for a mid-season
    draft. Stats are informational only: `browse`'s own `availability`
    field remains the sole authoritative state, read fresh from
    `player_ownership_period`, exactly as before this annotation existed."""
    items = request.app.state.player_pool.browse(season_id, query, availability, limit)
    stats_context = request.app.state.player_stats_context
    if draft_kind == "midseason":
        stats_by_canonical_id = stats_context.current_season_points(season_id)
        stats_label = "Current season to date"
    else:
        _year, stats_by_canonical_id = stats_context.previous_completed_season_points(season_id)
        stats_label = "Previous season"
    views = []
    for item in items:
        stats = stats_by_canonical_id.get(item.canonical_player_id)
        view = dataclasses.asdict(item)
        view["stats_label"] = stats_label
        view["games_played"] = stats.games if stats else None
        view["total_points"] = stats.total_points if stats else None
        view["average_points"] = stats.average_points if stats else None
        views.append(view)
    return views
