"""Draft Board application read service shared by HTTP and operator tools.

Repository mechanics remain in :mod:`app.draft`; callers inject the
repositories used here so this service owns orchestration without reaching
through the HTTP layer or creating a second draft model.
"""


def draft_board_readiness(database, identities, draft, player_pool, season_id):
    """Return whether the existing board can execute its next human pick."""
    entries = identities.list_entries(season_id)
    status = draft.status(season_id)
    config = database.execute(
        "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?", (season_id,)
    ).fetchone()
    available_count = len(player_pool.list_available(season_id))
    total_required = status.total_picks if status else (len(entries) * config["squad_limit"] if config else 0)
    completed = status.completed_picks if status else 0
    remaining = max(total_required - completed, 0)
    checks = {
        "entries": len(entries) == 10,
        "order": status is not None and len(draft.order(season_id)) == 10,
        "players": total_required > 0 and available_count >= remaining,
        "squad": config is not None and config["squad_limit"] > 0,
        "draft_not_paused": status is not None and not status.is_paused,
        "draft_not_finalized": status is not None and not status.is_finalized,
    }
    next_pick = draft.next_pick(season_id) if status and not status.is_paused and not status.is_finalized else None
    return {
        "ready": all(checks.values()),
        "checks": checks,
        "available_player_count": available_count,
        "remaining_selection_count": remaining,
        "next_pick_overall": next_pick.overall_number if next_pick else None,
    }
