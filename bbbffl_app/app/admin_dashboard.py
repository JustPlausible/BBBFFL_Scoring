"""Server-side read model for the Administrator Dashboard (issue #148).

This is the Administrator's role home: *is the league/season correctly
configured and governed, what administrative issues need attention, and
where should the Administrator go next?* It is deliberately the durable
**governance/readiness/navigation** counterpart to the Scorer Operations
Dashboard's (issue #147, `app.scorer_dashboard`) weekly **operational
attention/next-safe-action** surface -- see this module's "Relationship to
the Scorer Dashboard" note below.

Like `app.scorer_dashboard`/`app.season_centre`, this is an *aggregation and
navigation* read model only: it performs no domain mutations, persists no
dashboard-specific state, and never reimplements any workflow's own rules.
Every fact is read fresh, on every call, from the same authoritative
repositories the owning workflow pages already use (issue #153: nothing
here can drift from or contradict those pages), and every actionable item
links to the existing page that actually performs the mutation.

## Reuse, not duplication

- **`app.season_centre.build_season_centre`** supplies the selected
  season's identity, entries, competitions and readiness (draft/preseason/
  player-pool/Opening-Round) verbatim -- this module adds governance
  framing (attention queue, workflow stage, role/audit overview) on top,
  never a second readiness computation.
- **`app.scorer_dashboard.ordinary_rounds_with_lifecycle`/
  `select_current_round`** are the *shared* source of the
  definitions-vs-lifecycle distinction (issue #148's "never present '0
  opened lifecycle rows' as '0 rounds created'") -- the exact same function
  the Scorer Dashboard uses, so the two surfaces can never disagree about
  which round is "current" or what state it is in.
- **`app.scorer_dashboard.build_scorer_dashboard`** is called, whole, for
  the *concise operational summary* once weekly operations have begun
  (`_scorer_summary` below extracts only round/state/next-action/attention-
  counts from its result) -- this module never re-derives lineup, lockout,
  correction, adjudication or publication reasoning of its own.
- **`app.round_preflight.build_round_preflight`** (issue #152) supplies
  round-preparation readiness for the "governance attention queue" while a
  round is still `not_created`/`upcoming`, exactly like the Scorer
  Dashboard's own preflight branch.

## Relationship to the Scorer Dashboard

The Administrator Dashboard never reproduces the Scorer Dashboard's
complete attention queue, lineup table, lockout/trigger detail or
publication reasoning -- it shows a short summary (current round, its
state, the deterministic next action, and attention-item counts by
category) and links to `/scorer` for the operational detail. Both surfaces
are built from the same authoritative repositories, so they can never
contradict one another for the same season/round (see
`tests/test_admin_dashboard.py`'s and
`tests/test_dashboards_agree.py`'s cross-dashboard consistency coverage).
"""

import dataclasses

from app.audit import ENTITY_TYPE_LINEUP, ROLE_GRANT_CREATED, ROLE_GRANT_REVOKED
from app.lineups import WeeklyLineupRepository
from app.round_preflight import build_round_preflight
from app.scorer_dashboard import (
    CATEGORY_BLOCKING,
    build_scorer_dashboard,
    ordinary_rounds_with_lifecycle,
    select_current_round,
)
from app.season_centre import build_season_centre

# BBBFFL is a fixed ten-team league (see `app.replay_bootstrap.TEAM_COUNT`
# and `app.draft_board`'s own `len(entries) == 10` convention) -- the
# historical fixture rotation (`app.fixtures.BASE_ROTATION`) is itself only
# defined for ten teams, so this is a structural constant of the domain,
# not a configurable option this dashboard invents.
BBBFFL_TEAM_COUNT = 10

# -- Governance attention-queue categories (issue #148), in priority order.
# Deliberately distinct from `app.scorer_dashboard`'s own five categories --
# this dashboard groups by *governance concern*, not by operational
# urgency.
CATEGORY_BLOCKING_CONFIGURATION = "blocking_configuration"
CATEGORY_AUTHORITY_SECURITY = "authority_security"
CATEGORY_DATA_EVIDENCE = "data_evidence_readiness"
CATEGORY_OPERATIONAL_HANDOFF = "operational_handoff"
CATEGORY_COMPLETED_RECENT = "completed_recent"
CATEGORY_ORDER = (
    CATEGORY_BLOCKING_CONFIGURATION,
    CATEGORY_AUTHORITY_SECURITY,
    CATEGORY_DATA_EVIDENCE,
    CATEGORY_OPERATIONAL_HANDOFF,
    CATEGORY_COMPLETED_RECENT,
)

# -- Workflow map stages (issue #148's "compact, state-aware sequence").
STAGE_SETUP = "setup"
STAGE_DRAFT = "draft"
STAGE_PRESEASON = "preseason"
STAGE_ROUND_PREPARATION = "round_preparation"
STAGE_WEEKLY_OPERATIONS = "weekly_operations"
STAGE_SEASON_COMPLETE = "season_complete"

_WORKFLOW_STAGES = (
    (STAGE_SETUP, "Identities, competition & rules setup"),
    (STAGE_DRAFT, "Draft"),
    (STAGE_PRESEASON, "Preseason review & freeze"),
    (STAGE_ROUND_PREPARATION, "Fixture, Opening Round & round preparation"),
    (STAGE_WEEKLY_OPERATIONS, "Weekly Scorer operations"),
    (STAGE_SEASON_COMPLETE, "Season completion / archive"),
)

SEASON_CENTRE_URL = "/admin/season-centre/{season_id}"
PUBLIC_SEASON_URL = "/seasons/{season_id}"
SCORER_DASHBOARD_URL = "/scorer"
DRAFT_URL = "/admin/draft/{season_id}"
PRESEASON_URL = "/admin/preseason/{season_id}"
OPENING_ROUND_URL = "/operations/seasons/{season_id}/opening-round"
PREFLIGHT_URL = "/admin/round-preflight/{round_id}"


def scorer_dashboard_link(season_id: str, round_id: str | None = None) -> str:
    """A Scorer Dashboard handoff link carrying the *season this
    Administrator is actually looking at* (and its current round, where
    known) as query parameters -- issue #148/Codex review on PR #160: the
    Scorer Dashboard page has no other way to know which season an
    Administrator meant, and without this it silently falls back to
    `SeasonRepository.list_seasons()[0]` (the newest season), which can
    silently show -- and permit actions against -- a different season
    than the one this dashboard just summarised. `app.routes.
    scorer_dashboard.scorer_home_page`/`scorer_dashboard.html` read these
    same parameters back on load (see that template's `initialSeasonId`/
    `initialRoundId`)."""
    url = f"{SCORER_DASHBOARD_URL}?season_id={season_id}"
    if round_id:
        url += f"&round_id={round_id}"
    return url


# -- Season portfolio ---------------------------------------------------


def _round_summary(database, season_id: str, round_id: str | None = None) -> dict:
    """The definitions-vs-lifecycle round summary shared with the Scorer
    Dashboard: how many ordinary rounds are *defined*, how many have an
    opened lifecycle row, and which one is "current" -- see
    `ordinary_rounds_with_lifecycle`'s docstring. Never conflates the two
    counts (issue #148's core acceptance criterion). `round_id`, when it
    names a real ordinary round in this season, is reused verbatim from
    `select_current_round` -- the exact same explicit-override-else-
    deterministic-current rule the Scorer Dashboard applies, so an
    explicit selection (or an unrecognised one, which falls back
    gracefully) can never make the two dashboards disagree about which
    round is being described."""
    rounds = ordinary_rounds_with_lifecycle(database, season_id)
    opened = [r for r in rounds if r["round_state"] is not None]
    current = select_current_round(rounds, round_id)
    return {
        "rounds_defined": len(rounds),
        "rounds_opened": len(opened),
        "current_round": None
        if current is None
        else {
            "bbbffl_round_id": current["bbbffl_round_id"],
            "round_label": current["round_label"],
            "state": current["round_state"] or "not_created",
        },
    }


def _season_blockers(entries, draft_status, window, fixture_draw) -> list[str]:
    """A short, human-readable list of the most significant readiness
    blockers for one season -- for the portfolio row only; the full
    governance attention queue (`_attention_queue`) is the authoritative,
    linkable version of this for the *selected* season."""
    blockers = []
    if len(entries) != BBBFFL_TEAM_COUNT:
        blockers.append(f"{len(entries)}/{BBBFFL_TEAM_COUNT} teams established")
    if draft_status is None:
        blockers.append("Draft not yet started")
    elif not draft_status.is_finalized:
        blockers.append("Draft not finalized")
    elif window is None:
        blockers.append("Preseason window not opened")
    elif window.is_open:
        blockers.append("Preseason window still open")
    elif fixture_draw is None or fixture_draw.state != "frozen":
        blockers.append("Fixture draw not frozen")
    return blockers


def build_season_portfolio(
    seasons_repo,
    identities,
    draft_repo,
    preseason_repo,
    fixtures_repo,
    database,
    *,
    season_ids: set | None = None,
) -> list[dict]:
    """Every season this Administrator (or season-scoped role) may see,
    human-labelled with high-level lifecycle/status -- issue #148's "do not
    assume the newest season is always the active operational season".
    `season_ids=None` returns every season (unscoped Administrator); a set
    restricts to those ids (mirroring `app.routes.scorer_dashboard`'s own
    `_authorized_seasons` season-scoping, applied by the caller)."""
    rows = []
    for season in seasons_repo.list_seasons():
        if season_ids is not None and season.season_id not in season_ids:
            continue
        entries = identities.list_entries(season.season_id)
        competitions = seasons_repo.list_competitions(season.season_id)
        draft_status = draft_repo.status(season.season_id)
        window = preseason_repo.get_window(season.season_id)
        fixture_draw = fixtures_repo.get_draw(season.season_id)
        round_summary = _round_summary(database, season.season_id)
        ordinary = next((c for c in competitions if c.stream_type == "ordinary"), None)
        rules_versions = {rv.rules_version_id: rv for rv in seasons_repo.list_rules_versions(season.season_id)}
        rules_label = None
        if ordinary is not None:
            rules_version = rules_versions.get(ordinary.rules_version_id)
            rules_label = rules_version.display_label if rules_version is not None else None
        rows.append(
            {
                "season_id": season.season_id,
                "year": season.year,
                "label": season.label,
                "lifecycle_state": season.lifecycle_state,
                "competition_label": ordinary.label if ordinary is not None else None,
                "rules_label": rules_label,
                "team_count": len(entries),
                "expected_team_count": BBBFFL_TEAM_COUNT,
                "rounds_defined": round_summary["rounds_defined"],
                "rounds_opened": round_summary["rounds_opened"],
                "current_round": round_summary["current_round"],
                "blockers": _season_blockers(entries, draft_status, window, fixture_draw),
                "season_centre_url": SEASON_CENTRE_URL.format(season_id=season.season_id),
                "public_season_url": PUBLIC_SEASON_URL.format(season_id=season.season_id),
                "scorer_dashboard_url": (
                    scorer_dashboard_link(
                        season.season_id,
                        round_summary["current_round"]["bbbffl_round_id"] if round_summary["current_round"] else None,
                    )
                    if round_summary["rounds_opened"] > 0
                    else None
                ),
            }
        )
    return rows


# -- Identity/duplicate integrity -----------------------------------------


def _identity_integrity_issues(entries) -> list[dict]:
    """Duplicate/incomplete coach-licence-team identity (issue #148's
    "incomplete or duplicate coach/team/licence identity"). `entries` is
    `IdentityRepository.list_entries`'s existing `SeasonEntryOverview` list
    -- no new identity model, just a uniqueness scan over what it already
    returns."""
    issues: list[dict] = []
    by_name: dict[str, list] = {}
    by_licence: dict[str, list] = {}
    for entry in entries:
        by_name.setdefault((entry.team_name or "").strip().lower(), []).append(entry)
        by_licence.setdefault(entry.licence_key, []).append(entry)
    for name, group in by_name.items():
        if name and len(group) > 1:
            issues.append(
                {
                    "kind": "duplicate_team_name",
                    "detail": f"{len(group)} teams share the public name {group[0].team_name!r}",
                    "season_entry_ids": [e.season_entry_id for e in group],
                }
            )
    for licence, group in by_licence.items():
        if len(group) > 1:
            issues.append(
                {
                    "kind": "duplicate_licence_key",
                    "detail": f"{len(group)} season entries share licence key {licence!r}",
                    "season_entry_ids": [e.season_entry_id for e in group],
                }
            )
    return issues


# -- Role and access overview ---------------------------------------------


def _role_overview(identities, role_grants, season_id: str) -> dict:
    """Discoverable summary of who holds which granted authority relevant
    to this season (issue #148's "Role and access administration").
    Read-only: every mutation stays in the existing Season Centre "Role
    grants" panel (`POST /api/admin/role-grants*`, `app.routes.context`) --
    this never grants/revokes anything itself."""
    coaches_by_role: dict[str, list[dict]] = {}
    for coach in identities.list_coaches():
        for grant in role_grants.list_active_for_coach(coach.coach_id):
            if grant.season_id is not None and grant.season_id != season_id:
                continue
            coaches_by_role.setdefault(grant.role, []).append(
                {
                    "coach_id": coach.coach_id,
                    "display_name": coach.display_name,
                    "season_scoped": grant.season_id is not None,
                    "granted_at": grant.granted_at,
                }
            )
    return {
        "grants_by_role": coaches_by_role,
        "season_centre_url": SEASON_CENTRE_URL.format(season_id=season_id),
    }


# -- Governance attention queue --------------------------------------------


def _attention_item(category, code, title, detail, *, capability, url, diagnostics=None) -> dict:
    return {
        "category": category,
        "code": code,
        "title": title,
        "detail": detail,
        "capability": capability,
        "url": url,
        "diagnostics": diagnostics,
    }


def _attention_queue(
    *,
    season,
    entries,
    readiness: dict,
    has_ordinary_competition: bool,
    fixture_draw,
    round_summary: dict,
    current_round_id: str | None,
    identity_issues: list[dict],
    role_overview: dict,
    preflight: dict | None,
    scorer_summary: dict | None,
) -> list[dict]:
    items: list[dict] = []
    season_centre_url = SEASON_CENTRE_URL.format(season_id=season.season_id)

    # -- Blocking configuration ------------------------------------------
    if len(entries) != BBBFFL_TEAM_COUNT:
        items.append(
            _attention_item(
                CATEGORY_BLOCKING_CONFIGURATION,
                "identity:incomplete_entries",
                "Season entries incomplete" if len(entries) < BBBFFL_TEAM_COUNT else "Season has too many entries",
                f"{len(entries)} of {BBBFFL_TEAM_COUNT} BBBFFL teams are established for {season.label}.",
                capability="season.manage",
                url=season_centre_url,
            )
        )
    for issue in identity_issues:
        items.append(
            _attention_item(
                CATEGORY_BLOCKING_CONFIGURATION,
                f"identity:{issue['kind']}",
                "Duplicate team identity",
                issue["detail"],
                capability="season.manage",
                url=season_centre_url,
                diagnostics={"season_entry_ids": issue["season_entry_ids"]},
            )
        )
    if not has_ordinary_competition:
        items.append(
            _attention_item(
                CATEGORY_BLOCKING_CONFIGURATION,
                "season:no_competition_configured",
                "No ordinary competition/rules stream configured",
                "This season has no ordinary competition stream, so it cannot run weekly rounds yet -- a "
                "finals/replay/SuperScore-only stream does not satisfy this."
                if readiness["competition_streams_configured"]
                else "This season has no competition stream, so no rules version is accepted yet.",
                capability="season.manage",
                url=season_centre_url,
            )
        )
    draft = readiness["draft"]
    if draft is not None and not draft["is_finalized"] and draft["is_complete"]:
        items.append(
            _attention_item(
                CATEGORY_BLOCKING_CONFIGURATION,
                "draft:not_finalized",
                "Draft complete but not finalized",
                "Every pick is complete; finalize the draft to open the preseason window.",
                capability="draft.manage",
                url=DRAFT_URL.format(season_id=season.season_id),
            )
        )
    elif draft is not None and not draft["is_finalized"]:
        items.append(
            _attention_item(
                CATEGORY_BLOCKING_CONFIGURATION,
                "draft:incomplete",
                "Draft in progress",
                f"{draft['completed_picks']}/{draft['total_picks']} picks complete.",
                capability="draft.manage",
                url=DRAFT_URL.format(season_id=season.season_id),
            )
        )
    window = readiness["preseason_window"]
    if draft is not None and draft["is_finalized"] and window is None:
        items.append(
            _attention_item(
                CATEGORY_BLOCKING_CONFIGURATION,
                "preseason:window_not_opened",
                "Preseason window not opened",
                "The draft is finalized; open the preseason trade/transaction window.",
                capability="preseason.manage",
                url=PRESEASON_URL.format(season_id=season.season_id),
            )
        )
    elif window is not None and window["is_open"]:
        items.append(
            _attention_item(
                CATEGORY_DATA_EVIDENCE,
                "preseason:window_open",
                "Preseason window still open",
                "Opening squads are not yet frozen; close the window before round preparation.",
                capability="preseason.manage",
                url=PRESEASON_URL.format(season_id=season.season_id),
            )
        )
    if window is not None and not window["is_open"] and (fixture_draw is None or fixture_draw.state != "frozen"):
        items.append(
            _attention_item(
                CATEGORY_BLOCKING_CONFIGURATION,
                "fixture:not_frozen",
                "Fixture draw not frozen",
                "Opening squads are frozen; the season fixture draw is not yet frozen.",
                capability="fixture.manage",
                url=season_centre_url,
            )
        )
    opening_round = readiness["opening_round"]
    if opening_round is not None and not opening_round["is_ready"]:
        items.append(
            _attention_item(
                CATEGORY_BLOCKING_CONFIGURATION,
                "opening_round:not_ready",
                "Opening Round nominations incomplete",
                f"{opening_round['total_confirmed']}/{opening_round['total_entries']} entries confirmed.",
                capability="opening_round.nominate",
                url=OPENING_ROUND_URL.format(season_id=season.season_id),
            )
        )
    if opening_round is not None and opening_round["has_integrity_issues"]:
        items.append(
            _attention_item(
                CATEGORY_DATA_EVIDENCE,
                "opening_round:integrity",
                "Opening Round nomination integrity warning",
                "Duplicate, mismatched or conflicting Opening Round nominations were detected.",
                capability="opening_round.nominate",
                url=OPENING_ROUND_URL.format(season_id=season.season_id),
            )
        )
    if preflight is not None:
        for blocker in preflight["readiness"]["blockers"]:
            round_id = current_round_id
            items.append(
                _attention_item(
                    CATEGORY_BLOCKING_CONFIGURATION,
                    f"preflight:{blocker['code']}",
                    "Round preflight blocker",
                    blocker["message"],
                    capability="roundsetup.manage",
                    url=PREFLIGHT_URL.format(round_id=round_id),
                    diagnostics={"code": blocker["code"]},
                )
            )

    # -- Authority / security ---------------------------------------------
    if not role_overview["grants_by_role"].get("admin"):
        items.append(
            _attention_item(
                CATEGORY_AUTHORITY_SECURITY,
                "authority:no_standing_administrator",
                "No authenticated Administrator holds a role grant",
                "Only the legacy shared admin-token credential currently confers Administrator authority.",
                capability=None,
                url=role_overview["season_centre_url"],
            )
        )
    if not role_overview["grants_by_role"].get("scorer") and round_summary["rounds_opened"] > 0:
        items.append(
            _attention_item(
                CATEGORY_AUTHORITY_SECURITY,
                "authority:no_scorer_granted",
                "No standing Scorer authority granted",
                "Weekly operations have begun but no coach identity holds a Scorer role grant for this season.",
                capability=None,
                url=role_overview["season_centre_url"],
            )
        )

    # -- Operational handoff ------------------------------------------------
    if scorer_summary is not None:
        blocking = scorer_summary["attention_counts"].get(CATEGORY_BLOCKING, 0)
        if blocking:
            items.append(
                _attention_item(
                    CATEGORY_OPERATIONAL_HANDOFF,
                    "scorer:blocking_attention",
                    "Scorer Dashboard has blocking attention items",
                    f"{blocking} blocking item(s) on the current round -- see the Scorer Dashboard.",
                    capability="round.review",
                    url=scorer_summary["scorer_dashboard_url"],
                )
            )
        items.append(
            _attention_item(
                CATEGORY_OPERATIONAL_HANDOFF,
                "scorer:next_action",
                scorer_summary["next_action"]["title"],
                scorer_summary["next_action"]["detail"],
                capability=scorer_summary["next_action"]["capability"],
                url=scorer_summary["scorer_dashboard_url"],
            )
        )

    items.sort(key=lambda item: (CATEGORY_ORDER.index(item["category"]), item["title"]))
    return items


# -- Workflow map ------------------------------------------------------


def _current_stage(
    season, readiness: dict, has_ordinary_competition: bool, window, fixture_draw, round_summary: dict
) -> str:
    if season.lifecycle_state == "completed":
        return STAGE_SEASON_COMPLETE
    if readiness["entries_established"] != BBBFFL_TEAM_COUNT or not has_ordinary_competition:
        return STAGE_SETUP
    draft = readiness["draft"]
    if draft is None or not draft["is_finalized"]:
        return STAGE_DRAFT
    if window is None or window.is_open:
        return STAGE_PRESEASON
    current_round = round_summary["current_round"]
    if current_round is None or current_round["state"] in ("not_created", "upcoming"):
        return STAGE_ROUND_PREPARATION
    if current_round["state"] == "final" and round_summary["rounds_opened"] == round_summary["rounds_defined"]:
        return STAGE_SEASON_COMPLETE
    return STAGE_WEEKLY_OPERATIONS


def _workflow_map(
    season, readiness, has_ordinary_competition, window, fixture_draw, round_summary, links, current_round_id
) -> list[dict]:
    current = _current_stage(season, readiness, has_ordinary_competition, window, fixture_draw, round_summary)
    # `current_round_id` (from `select_current_round`) only names a round
    # actually awaiting preparation while `current` is itself
    # STAGE_ROUND_PREPARATION -- once a round has opened, the *next*
    # round's preparation has not begun yet (rounds are prepared one at a
    # time), so falling back to the general Opening Round entry point
    # avoids linking this stage at a stale, already-opened round.
    preparation_url = (
        PREFLIGHT_URL.format(round_id=current_round_id)
        if current == STAGE_ROUND_PREPARATION and current_round_id
        else links["opening_round"]
    )
    stage_urls = {
        STAGE_SETUP: SEASON_CENTRE_URL.format(season_id=season.season_id),
        # `app.season_centre`'s own `links["draft"]`/`links["preseason"]`
        # are `None` until a draft has actually started -- appropriate for
        # Season Centre's own conditional display, but wrong here: the
        # workflow map must link the *current* stage to the page that
        # begins it, precisely while nothing has started there yet. Use
        # this module's own unconditional URLs instead.
        STAGE_DRAFT: DRAFT_URL.format(season_id=season.season_id),
        STAGE_PRESEASON: PRESEASON_URL.format(season_id=season.season_id),
        STAGE_ROUND_PREPARATION: preparation_url,
        STAGE_WEEKLY_OPERATIONS: (
            scorer_dashboard_link(season.season_id, current_round_id) if round_summary["rounds_opened"] > 0 else None
        ),
        STAGE_SEASON_COMPLETE: SEASON_CENTRE_URL.format(season_id=season.season_id),
    }
    return [
        {"stage": stage, "label": label, "is_current": stage == current, "url": stage_urls.get(stage)}
        for stage, label in _WORKFLOW_STAGES
    ]


# -- Scorer operational summary (reuses #147 wholesale) --------------------


def _scorer_summary(
    database, lifecycle, identities, seasons_repo, round_review_repo, audit_events, afl_client, season_id, round_id
) -> dict | None:
    """A *concise* Scorer Operations Dashboard summary -- issue #148's
    "show a concise human-readable summary of Scorer operational state ...
    do not reproduce its complete attention queue ... do not create a
    second version of its deterministic next-action logic". Calls
    `build_scorer_dashboard` (#147) in full and extracts only round/state/
    next-action/attention-counts from its result; never recomputes any of
    it. Returns `None` while no round has ever been opened for this
    season -- there is nothing operational to summarise yet."""
    dashboard = build_scorer_dashboard(
        database,
        lifecycle,
        identities,
        seasons_repo,
        round_review_repo,
        audit_events,
        afl_client,
        season_id,
        round_id=round_id,
    )
    if dashboard["round"] is None:
        return None
    counts: dict[str, int] = {}
    for item in dashboard["attention"]:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    return {
        "round_label": dashboard["round"]["round_label"],
        "round_state": dashboard["round"]["state"],
        "next_action": dashboard["next_action"],
        "attention_counts": counts,
        "review_ready_for_signoff": dashboard["review"]["ready_for_signoff"] if dashboard["review"] else None,
        "scorer_dashboard_url": scorer_dashboard_link(season_id, dashboard["round"]["bbbffl_round_id"]),
    }


# -- Audit / integrity overview --------------------------------------------

_ADMIN_AUDIT_ACTION_LABELS = {
    "season.lifecycle.changed": "Season lifecycle changed",
    "season.rules_version.created": "Rules version created",
    "identity.coach.updated": "Coach record updated",
    "identity.season_entry.created": "Season entry created",
    "identity.team_name.changed": "Team renamed",
    "identity.season_entry.coach_changed": "Team reassigned to a different coach",
    ROLE_GRANT_CREATED: "Role grant created",
    ROLE_GRANT_REVOKED: "Role grant revoked",
    "draft.order.accepted": "Draft order accepted",
    "draft.finalized": "Draft finalized",
    "draft.pick.corrected": "Draft pick corrected",
    "preseason.window.opened": "Preseason window opened",
    "preseason.squad.frozen": "Opening squads frozen",
    "preseason.window.closed": "Preseason window closed",
    "preseason.trade.applied": "Preseason trade applied",
    "preseason.correction.applied": "Preseason correction applied",
    "fixture.draw.frozen": "Fixture draw frozen",
    "round.afl_mapping.accepted": "AFL round mapping accepted",
    "round.afl_mapping.corrected": "AFL round mapping corrected",
    "competition.round.finalized": "Round published",
    "competition.result.corrected": "Official result corrected",
    "lineup.correction.recorded": "Locked lineup corrected",
    "lineup.adjudication.recorded": "Missed submission adjudicated",
}


def _audit_summary(
    database,
    lifecycle,
    audit_events,
    role_grants,
    season,
    entries,
    draft_status,
    window,
    fixture_draw,
    current_round_id: str | None,
    limit: int = 20,
) -> list[dict]:
    """Recent, high-value administrative events for this season, read
    straight from the existing immutable `audit_event` trail -- issue
    #148's "use existing immutable audit/provenance records ... do not
    create a parallel log". Gathers events by the same known entity ids
    the domain modules themselves recorded them against (see this
    module's docstring), mirroring `app.scorer_dashboard._recent_activity`'s
    established per-entity-id gather-then-merge shape.

    Round-level events (mapping acceptance/correction, round lifecycle
    transitions, published/corrected results) are gathered for every
    ordinary round in the season (Codex review, PR #160) -- cheap,
    bounded audit-table reads, no live AFL evidence involved. Lineup-level
    correction/adjudication events are gathered only for the *current*
    round, matching `app.scorer_dashboard`'s own single-round scope for
    that same event class -- scanning every entry's private draft across
    every historical round would be disproportionate for a 20-item recent-
    activity summary."""
    events = list(audit_events.list_events(entity_type="season", entity_id=season.season_id, limit=limit))
    coach_ids = {entry.coach_id for entry in entries}
    for coach_id in coach_ids:
        events += audit_events.list_events(entity_type="coach", entity_id=coach_id, limit=5)
    for entry in entries:
        events += audit_events.list_events(entity_type="season_entry", entity_id=entry.season_entry_id, limit=5)
    if draft_status is not None:
        events += audit_events.list_events(entity_type="draft", entity_id=draft_status.draft_id, limit=10)
    if window is not None:
        events += audit_events.list_events(entity_type="preseason.window", entity_id=window.window_id, limit=10)
    if fixture_draw is not None:
        events += audit_events.list_events(entity_type="fixture_draw", entity_id=fixture_draw.fixture_draw_id, limit=5)
    rounds = ordinary_rounds_with_lifecycle(database, season.season_id)
    for row in rounds:
        events += audit_events.list_events(entity_type="competition.round", entity_id=row["bbbffl_round_id"], limit=5)
        if row["mapping_id"]:
            events += audit_events.list_events(entity_type="round.afl_mapping", entity_id=row["mapping_id"], limit=5)
        if row["round_state"] is not None:
            for matchup in lifecycle.list_matchups(row["bbbffl_round_id"]):
                events += audit_events.list_events(
                    entity_type="competition.matchup", entity_id=matchup.matchup_id, limit=5
                )
    current_round = next((r for r in rounds if r["bbbffl_round_id"] == current_round_id), None)
    if current_round is not None:
        lineups_repo = WeeklyLineupRepository(database)
        for entry in entries:
            draft = lineups_repo.get_draft(
                season.season_id,
                current_round["competition_id"],
                current_round["bbbffl_round_id"],
                entry.season_entry_id,
            )
            if draft is not None:
                events += audit_events.list_events(entity_type=ENTITY_TYPE_LINEUP, entity_id=draft.lineup_id, limit=5)
    # Only this season's own role-grant changes -- a coach participating
    # here may separately hold a season-scoped grant for a *different*
    # season (or several); `list_all_for_coach` returns every grant that
    # coach has ever held, so a global grant (`season_id is None`) is kept
    # but a grant scoped to another season is excluded (Codex review, PR
    # #160), matching this function's own "recent events for *this*
    # season" contract.
    for coach_id in coach_ids:
        for grant in role_grants.list_all_for_coach(coach_id):
            if grant.season_id is not None and grant.season_id != season.season_id:
                continue
            events += audit_events.list_events(entity_type="identity.role_grant", entity_id=grant.grant_id, limit=5)
    events.sort(key=lambda event: event.sequence, reverse=True)
    seen: set[str] = set()
    deduped = []
    for event in events:
        if event.event_id in seen:
            continue
        seen.add(event.event_id)
        deduped.append(event)
        if len(deduped) >= limit:
            break
    return [
        {
            "event_id": event.event_id,
            "action": event.action,
            "label": _ADMIN_AUDIT_ACTION_LABELS.get(event.action, event.action),
            "entity_type": event.entity_type,
            "occurred_at": event.occurred_at,
            "actor_role": event.actor_role,
            "reason": event.reason,
            "diagnostics": {"entity_type": event.entity_type, "entity_id": event.entity_id},
        }
        for event in deduped
    ]


# -- Top-level orchestrator -------------------------------------------------


def build_admin_dashboard(
    database,
    seasons_repo,
    identities,
    draft_repo,
    preseason_repo,
    player_pool_repo,
    lifecycle,
    fixtures_repo,
    round_review_repo,
    audit_events,
    role_grants,
    afl_client,
    season_id: str,
    *,
    round_id: str | None = None,
) -> dict:
    """The full Administrator Dashboard read model for one selected season.

    Every value is recomputed fresh on this call (issue #153) -- nothing is
    cached, and nothing here mutates anything. Raises `KeyError` for an
    unknown `season_id`, matching `build_season_centre`/`build_scorer_
    dashboard`'s existing convention (the route layer maps this to 404)."""
    centre = build_season_centre(
        seasons_repo, identities, draft_repo, preseason_repo, player_pool_repo, lifecycle, season_id, database
    )
    season = seasons_repo.get_season(season_id)
    if season is None:
        raise KeyError(season_id)
    entries = identities.list_entries(season_id)
    readiness = centre["readiness"]
    draft_status = draft_repo.status(season_id)
    window = preseason_repo.get_window(season_id)
    fixture_draw = fixtures_repo.get_draw(season_id)
    round_summary = _round_summary(database, season_id, round_id)
    identity_issues = _identity_integrity_issues(entries)
    role_overview = _role_overview(identities, role_grants, season_id)
    rules_versions = {rv.rules_version_id: rv for rv in seasons_repo.list_rules_versions(season_id)}
    ordinary = next((c for c in centre["competitions"] if c["stream_type"] == "ordinary"), None)
    rules_view = None
    if ordinary is not None:
        rules_version = rules_versions.get(ordinary["rules_version_id"])
        rules_view = None if rules_version is None else rules_version.display_label

    current_round = round_summary["current_round"]
    current_round_id = current_round["bbbffl_round_id"] if current_round is not None else None
    current_state = current_round["state"] if current_round is not None else None

    preflight = None
    if current_round_id is not None and current_state in ("not_created", "upcoming"):
        preflight = build_round_preflight(database, lifecycle, identities, afl_client, current_round_id)

    scorer_summary = None
    if round_summary["rounds_opened"] > 0:
        scorer_summary = _scorer_summary(
            database,
            lifecycle,
            identities,
            seasons_repo,
            round_review_repo,
            audit_events,
            afl_client,
            season_id,
            current_round_id,
        )

    attention = _attention_queue(
        season=season,
        entries=entries,
        readiness=readiness,
        has_ordinary_competition=ordinary is not None,
        fixture_draw=fixture_draw,
        round_summary=round_summary,
        current_round_id=current_round_id,
        identity_issues=identity_issues,
        role_overview=role_overview,
        preflight=preflight,
        scorer_summary=scorer_summary,
    )
    workflow_map = _workflow_map(
        season, readiness, ordinary is not None, window, fixture_draw, round_summary, centre["links"], current_round_id
    )
    audit_summary = _audit_summary(
        database,
        lifecycle,
        audit_events,
        role_grants,
        season,
        entries,
        draft_status,
        window,
        fixture_draw,
        current_round_id,
    )

    return {
        "season": {
            "season_id": season.season_id,
            "year": season.year,
            "label": season.label,
            "lifecycle_state": season.lifecycle_state,
            "rules_label": rules_view,
        },
        "entries": [dataclasses.asdict(entry) for entry in entries],
        "identity_issues": identity_issues,
        "readiness": {
            **readiness,
            "fixture_draw": None
            if fixture_draw is None
            else {"state": fixture_draw.state, "frozen_at": fixture_draw.frozen_at},
            "rounds_defined": round_summary["rounds_defined"],
            "rounds_opened": round_summary["rounds_opened"],
        },
        "current_round": round_summary["current_round"],
        "preflight": preflight,
        "scorer_summary": scorer_summary,
        "attention": attention,
        "workflow_map": workflow_map,
        "role_overview": role_overview,
        "audit": audit_summary,
        "links": {
            **centre["links"],
            "season_centre": SEASON_CENTRE_URL.format(season_id=season_id),
            "public_season": PUBLIC_SEASON_URL.format(season_id=season_id),
        },
    }
