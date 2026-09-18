"""Composed Scorer read model for one concurrent AFL finals week (issue
#208): the finals-stream and SuperScore-stream counterpart to
`app.scorer_dashboard`'s `ordinary`-only Scorer Operations Dashboard.

Before this module existed, requesting the Scorer dashboard with a
`finals`/`superscore` `round_id` silently fell back to whatever ordinary
round `app.scorer_dashboard.select_current_round` picked instead (the
reported "resolves back to Round 20" defect) -- `app.scorer_dashboard`
only ever read `ordinary`-typed rounds at all. `app.routes.scorer_dashboard`
now resolves a requested round's stream first (`app.scorer_dashboard.
round_stream_type`) and calls `build_finals_week_dashboard` here instead
whenever it names a `finals` or `superscore` round.

This module composes -- it does not reimplement -- the two streams' own
domain read models:

- Finals: `app.finals.FinalsBracketRepository` (pairings/bye),
  `app.finals_preflight.build_finals_week_preflight` (pre-open readiness)
  and `app.finals_review.build_finals_round_review` (once opened) --
  the exact review model `app/routes/finals_preflight.py`'s publish action
  already relies on, never a second one.
- SuperScore: the always-present `superscore_entry_review_state` row set
  (`app.superscore_round`) and `app.superscore_results.
  SuperScoreLeaderboardService.leaderboard` for published state -- again,
  the existing entry-scoped review/calculation/publication boundary
  (`app.superscore_review`/`app.superscore_results`), never a duplicate.

Per-entry lineup readiness for both sections reuses `app.scorer_dashboard.
compute_round_readiness` -- the exact lockout/lineup-state evaluation the
ordinary dashboard already performs, applied to each stream's own
participant list (`app.finals_participation.list_round_participant_entry_ids`
for finals; every `superscore_entry_review_state` row for SuperScore) --
never a fabricated matchup/opponent for SuperScore, and never a narrower
eligibility check than each stream's own authoritative participation rule.

The two sections are returned side by side, never merged into one shape:
finals keeps its variable-match-count, bye-aware bracket model; SuperScore
keeps its matchup-free, all-ten-entry leaderboard model.
"""

import json

from app.competition_lifecycle import CompetitionLifecycleRepository
from app.finals import (
    SLOT_LABELS,
    WEEK_LABELS,
    FinalsBracketError,
    FinalsBracketRepository,
    IncompleteFinalsWeekError,
)
from app.finals_participation import list_round_participant_entry_ids
from app.finals_preflight import build_finals_week_preflight
from app.finals_review import build_finals_round_review
from app.scorer_dashboard import (
    CATEGORY_ADVISORY,
    CATEGORY_BLOCKING,
    CATEGORY_DECISION_REQUIRED,
    CATEGORY_WAITING,
    NO_AUTHORITATIVE_SUBMISSION_STATES,
    SCORER_DASHBOARD_URL,
    NextAction,
    compute_round_readiness,
    round_stream_type,
    season_round_options,
)
from app.stream_presentation import humanize_round_label
from app.superscore_results import SuperScoreLeaderboardService

SUPERSCORE_CALCULATE_URL = "/api/season-superscore/scorer/rounds/{round_id}/calculate"
SUPERSCORE_ADVANCE_TO_REVIEW_URL = "/api/season-superscore/scorer/rounds/{round_id}/advance-to-review"
SUPERSCORE_PUBLISH_URL = "/api/season-superscore/scorer/rounds/{round_id}/publish"
SUPERSCORE_RULING_URL = "/api/scorer/superscore/rounds/{round_id}/entries/{season_entry_id}"
FINALS_OPEN_URL = "/api/admin/finals/{bracket_id}/weeks/{week_number}/open"
# Issue #211 P1 (Codex review): the dashboard's own "Open finals week"
# button must actually reach workflow B's paired action
# (`app.finals_superscore_open.open_finals_and_superscore_week`) whenever a
# concurrent SuperScore round exists -- otherwise it silently continues
# opening Finals alone, leaving SS's lockout plan unsynchronised and SS
# itself unopened, and the paired endpoint stays reachable only to a caller
# who already knows its URL.
FINALS_OPEN_PAIRED_URL = "/api/admin/finals/{bracket_id}/weeks/{week_number}/open-paired"
FINALS_ADVANCE_TO_REVIEW_URL = "/api/admin/finals/{bracket_id}/weeks/{week_number}/advance-to-review"
FINALS_PUBLISH_URL = "/api/admin/finals/{bracket_id}/weeks/{week_number}/publish"
FINALS_ROUND_REVIEW_API_URL = "/api/admin/round-review/{round_id}"
# Issue #216: the bracket-progression preview/apply pair -- `preview` is a
# new, read-only route wrapping `FinalsBracketRepository.preview_advance_
# bracket` (previously reachable only from the CLI); `advance` is the
# existing, unmodified apply route (issue #201/#190) the Scorer UI now also
# reaches, never a second progression implementation.
FINALS_ADVANCE_PREVIEW_URL = "/api/admin/finals/{bracket_id}/advance/{from_week}/preview"
FINALS_ADVANCE_URL = "/api/admin/finals/{bracket_id}/advance/{from_week}"


def _resolve_superscore_round_id(database, season_id: str, week_number: int) -> str | None:
    row = database.execute(
        "SELECT sr.bbbffl_round_id FROM bbbffl_round sr "
        "JOIN competition_stream sc ON sc.competition_id=sr.competition_id "
        "WHERE sc.season_id=? AND sc.stream_type='superscore' AND sr.round_key=?",
        (season_id, f"ss{week_number}"),
    ).fetchone()
    return row["bbbffl_round_id"] if row else None


def _week_lifecycle_state(database, bracket_id: str, week_number: int) -> str:
    row = database.execute(
        "SELECT bbbffl_round_id FROM finals_bracket_week WHERE bracket_id=? AND week_number=?",
        (bracket_id, week_number),
    ).fetchone()
    if row is None:
        return "not_created"
    persisted = CompetitionLifecycleRepository(database).get_round(row["bbbffl_round_id"])
    return persisted.state if persisted else "not_created"


def build_finals_progression_preview(database, identities, season, bracket_id: str, from_week: int) -> dict:
    """Human-facing wrapper around `FinalsBracketRepository.preview_advance_
    bracket` (issue #216): the domain preview already reports exactly what
    `advance_bracket(from_week=...)` would derive right now -- team names
    and human-readable slot/week labels are the only thing missing for a
    Scorer UI to show it before mutation, so this resolves those from
    `app.identity`/`app.finals.SLOT_LABELS`/`WEEK_LABELS` rather than
    reconstructing the derivation itself. Mirrors `app.finals.
    FinalsBracketRepository.preview_create_bracket`'s/`app.season_
    completion.preview_complete_season`'s own `ready`/`diagnostic` report
    shape for an unready state, instead of letting a domain exception
    surface raw to the UI."""
    from_week_label = WEEK_LABELS.get(from_week, f"Finals Week {from_week}")
    target_week = from_week + 1
    target_week_label = WEEK_LABELS.get(target_week, f"Finals Week {target_week}")
    report: dict = {
        "bracket_id": bracket_id,
        "from_week": from_week,
        "from_week_label": from_week_label,
        "target_week": target_week,
        "target_week_label": target_week_label,
        "ready": False,
        "diagnostic": None,
        "expected_versions": None,
        "new_pairings": [],
        "elimination": None,
    }
    entry_names = {entry.season_entry_id: entry.team_name for entry in identities.list_entries(season.season_id)}
    try:
        preview = FinalsBracketRepository(database).preview_advance_bracket(bracket_id, from_week)
    except KeyError as exc:
        report["diagnostic"] = f"unknown finals bracket or source matchup: {exc}"
        return report
    except IncompleteFinalsWeekError as exc:
        report["diagnostic"] = str(exc)
        return report
    except FinalsBracketError as exc:
        report["diagnostic"] = str(exc)
        return report
    report["ready"] = True
    report["expected_versions"] = preview["expected_versions"]
    report["new_pairings"] = [
        {
            "slot": pairing["slot"],
            "slot_label": SLOT_LABELS.get(pairing["slot"], pairing["slot"]),
            "home_season_entry_id": pairing["home_season_entry_id"],
            "home_team_name": entry_names.get(pairing["home_season_entry_id"]),
            "away_season_entry_id": pairing["away_season_entry_id"],
            "away_team_name": entry_names.get(pairing["away_season_entry_id"]),
        }
        for pairing in preview["new_pairings"]
    ]
    if preview["elimination"] is not None:
        eliminated_entry_id = preview["elimination"]["season_entry_id"]
        report["elimination"] = {
            "stage": preview["elimination"]["stage"],
            "season_entry_id": eliminated_entry_id,
            "team_name": entry_names.get(eliminated_entry_id),
        }
    return report


def build_finals_week_dashboard(database, lifecycle, identities, round_review_repo, afl_client, season, round_id):
    """The composed dashboard for the concurrent finals/SuperScore week
    `round_id` belongs to, or `None` if `round_id` names neither a finals
    nor a SuperScore round -- the caller (`app.routes.scorer_dashboard`)
    falls back to the ordinary dashboard in that case."""
    stream = round_stream_type(database, round_id)
    round_season_row = database.execute(
        "SELECT c.season_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (round_id,),
    ).fetchone()
    if round_season_row is None or round_season_row["season_id"] != season.season_id:
        # `round_id` belongs to a different season than the caller resolved
        # -- never trust it as authority over which season's data to
        # compose (the same "URL identifiers locate a round; they never
        # confer authority" rule `app.routes.delegated_operations` states).
        return None
    if stream == "finals":
        row = database.execute(
            "SELECT w.bracket_id, w.week_number FROM finals_bracket_week w WHERE w.bbbffl_round_id=?",
            (round_id,),
        ).fetchone()
        if row is None:
            return None
        bracket_id, week_number = row["bracket_id"], row["week_number"]
        finals_round_id = round_id
        superscore_round_id = _resolve_superscore_round_id(database, season.season_id, week_number)
    elif stream == "superscore":
        round_key_row = database.execute(
            "SELECT round_key FROM bbbffl_round WHERE bbbffl_round_id=?", (round_id,)
        ).fetchone()
        if round_key_row is None or not round_key_row["round_key"].startswith("ss"):
            return None
        week_number = int(round_key_row["round_key"][2:])
        superscore_round_id = round_id
        bracket_row = database.execute(
            "SELECT w.bracket_id, w.bbbffl_round_id finals_round_id FROM finals_bracket_week w "
            "JOIN finals_bracket b ON b.bracket_id=w.bracket_id WHERE b.season_id=? AND w.week_number=?",
            (season.season_id, week_number),
        ).fetchone()
        bracket_id = bracket_row["bracket_id"] if bracket_row else None
        finals_round_id = bracket_row["finals_round_id"] if bracket_row else None
    else:
        return None

    finals_section = _build_finals_section(
        database,
        lifecycle,
        identities,
        round_review_repo,
        afl_client,
        season,
        bracket_id,
        week_number,
        finals_round_id,
        superscore_round_id,
    )
    superscore_section = _build_superscore_section(
        database, identities, afl_client, season, week_number, superscore_round_id
    )
    # Issue #216: the same full-season Round selector sequence the ordinary
    # dashboard offers (Rounds 1-N, then each Finals week in order) -- never
    # a week-scoped subset the operator could only reach by already knowing
    # a finals/superscore round_id, and never a separate SS1-4 entry (the
    # concurrent SuperScore round for whichever finals week is picked stays
    # composed into this same dashboard).
    round_options = season_round_options(database, season.season_id)
    return {
        "season": {"season_id": season.season_id, "year": season.year, "label": season.label},
        "stream": "finals_week",
        "week_number": week_number,
        "week_label": WEEK_LABELS.get(week_number, f"Finals Week {week_number}"),
        "round_options": round_options,
        "round": None,
        "next_action": _finals_week_next_action(finals_section, superscore_section, season.season_id),
        "finals": finals_section,
        "superscore": superscore_section,
    }


def _finals_week_next_action(finals_section: dict, superscore_section: dict, season_id: str) -> dict:
    """The Finals-week composed dashboard's own "next safe action" (issue
    #216), mirroring `app.scorer_dashboard._determine_next_action`'s
    vocabulary and category conventions for the ordinary dashboard rather
    than inventing a parallel one. Reads only facts `finals_section`/
    `superscore_section` already computed (lifecycle state, preflight
    readiness, the `progression`/`blocked_by_week` blocks above) -- never a
    fresh domain read of its own."""
    if not finals_section.get("available"):
        return NextAction(
            "finals_not_available",
            CATEGORY_ADVISORY,
            "Finals not yet configured",
            finals_section.get("message") or "No finals bracket exists for this season yet.",
            None,
        ).__dict__

    state = finals_section["lifecycle_state"]
    week_label = finals_section["week_label"]
    progression = finals_section.get("progression")
    blocked_by_week = finals_section.get("blocked_by_week")

    if progression is not None and progression["reason"] == "pairing_missing":
        return NextAction(
            "finals_bracket_progression_required",
            CATEGORY_BLOCKING,
            f"Progress the bracket to prepare {week_label}",
            f"{progression['from_week_label']} is {progression['from_week_state']}, but {week_label} has no "
            "pairing yet; preview and apply bracket progression below.",
            None,
            capability="roundsetup.manage",
        ).__dict__

    if state in ("not_created", "upcoming"):
        # Codex review (PR #217, P2): a bracket-progression preview can only
        # ever succeed once the *prior* week is `final` -- when this week's
        # own pairing is missing because that prior week is still
        # incomplete, direct the operator to finish it (with a direct link)
        # rather than to a preflight blocker list that never explains what
        # "satisfy every blocker" actually requires here.
        if blocked_by_week is not None:
            return NextAction(
                "finals_prior_week_incomplete",
                CATEGORY_BLOCKING,
                f"Complete {blocked_by_week['week_label']} first",
                f"{week_label} has no pairing yet because {blocked_by_week['week_label']} is still "
                f"{blocked_by_week['state']}; finish and publish it before {week_label} can be prepared.",
                SCORER_DASHBOARD_URL.format(season_id=season_id, round_id=blocked_by_week["round_id"]),
                capability="round.review",
            ).__dict__
        preflight = finals_section.get("preflight")
        if preflight is not None and preflight["readiness"]["safe_to_open"]:
            suffix = " (and its concurrent SuperScore round)" if superscore_section.get("available") else ""
            return NextAction(
                "finals_week_ready_to_open",
                CATEGORY_BLOCKING,
                f"Open {week_label}",
                f"{week_label} preflight is satisfied; open the finals week{suffix}.",
                None,
                capability="roundsetup.manage",
            ).__dict__
        return NextAction(
            "finals_week_preflight_incomplete",
            CATEGORY_BLOCKING,
            f"Complete {week_label} preflight",
            f"Satisfy every preflight blocker before {week_label} can open.",
            None,
            capability="roundsetup.manage",
        ).__dict__

    if state in ("open", "live"):
        return NextAction(
            "finals_week_open",
            CATEGORY_WAITING,
            f"{week_label} {state}",
            f"{week_label} is {state}; continue scoring, DNP/Interchange rulings and SuperScore review below.",
            None,
        ).__dict__

    if state == "review":
        return NextAction(
            "finals_week_review",
            CATEGORY_DECISION_REQUIRED,
            f"{week_label} ready for review",
            "Resolve any remaining DNP/Interchange rulings, then publish.",
            None,
            capability="round.review",
        ).__dict__

    if state == "final":
        if progression is not None and progression["reason"] == "ready_to_progress":
            return NextAction(
                "finals_bracket_progression_ready",
                CATEGORY_BLOCKING,
                f"Progress the bracket to prepare {progression['target_week_label']}",
                f"{week_label} is published; preview and apply bracket progression below to prepare "
                f"{progression['target_week_label']}.",
                None,
                capability="roundsetup.manage",
            ).__dict__
        # Codex review (PR #217, P2): Finals and SuperScore have separate
        # review/publication lifecycles -- Finals reaching `final` says
        # nothing about whether the concurrent SuperScore round has also
        # been calculated/reviewed/published, so this must not report the
        # week as fully done while SuperScore work remains.
        if superscore_section.get("available") and superscore_section.get("lifecycle_state") != "final":
            ss_state = superscore_section["lifecycle_state"]
            return NextAction(
                "finals_week_superscore_incomplete",
                CATEGORY_DECISION_REQUIRED,
                f"{week_label} published -- SuperScore still needs attention",
                f"Finals for {week_label} is published, but the concurrent SuperScore round is still "
                f"{ss_state}; continue its review/publication workflow below.",
                None,
                capability="round.review",
            ).__dict__
        return NextAction(
            "finals_week_published",
            CATEGORY_ADVISORY,
            f"{week_label} published",
            f"{week_label} is published.",
            None,
        ).__dict__

    return NextAction(
        "finals_week_unknown",
        CATEGORY_ADVISORY,
        "Review finals week state",
        "Unrecognised finals week lifecycle state.",
        None,
    ).__dict__


def _build_finals_section(
    database,
    lifecycle,
    identities,
    round_review_repo,
    afl_client,
    season,
    bracket_id,
    week_number,
    finals_round_id,
    superscore_round_id,
):
    if bracket_id is None or finals_round_id is None:
        return {
            "available": False,
            "week_number": week_number,
            "week_label": WEEK_LABELS.get(week_number, f"Finals Week {week_number}"),
            "message": f"No finals bracket exists for {season.label} yet.",
        }

    bracket_repo = FinalsBracketRepository(database)
    pairings = bracket_repo.list_pairings(bracket_id, week_number=week_number)
    entry_names = {entry.season_entry_id: entry.team_name for entry in identities.list_entries(season.season_id)}

    bye = None
    matchups = []
    for pairing in pairings:
        if pairing.slot == "bye":
            bye = {
                "season_entry_id": pairing.home_season_entry_id,
                "team_name": entry_names.get(pairing.home_season_entry_id),
            }
            continue
        matchups.append(
            {
                "slot": pairing.slot,
                "slot_label": SLOT_LABELS.get(pairing.slot, pairing.slot),
                "home_season_entry_id": pairing.home_season_entry_id,
                "home_team_name": entry_names.get(pairing.home_season_entry_id),
                "away_season_entry_id": pairing.away_season_entry_id,
                "away_team_name": entry_names.get(pairing.away_season_entry_id),
                "matchup_id": pairing.matchup_id,
            }
        )

    round_row = database.execute(
        "SELECT competition_id FROM bbbffl_round WHERE bbbffl_round_id=?", (finals_round_id,)
    ).fetchone()
    competition_id = round_row["competition_id"]
    persisted = CompetitionLifecycleRepository(database).get_round(finals_round_id)
    lifecycle_state = persisted.state if persisted else "not_created"

    # Issue #211 P1 (Codex review, round 2): `open_finals_and_superscore_
    # week` explicitly supports retrying just the SuperScore half once
    # Finals has already opened (its own open transition is irreversible,
    # so a later validator/sync/setup failure leaves Finals open and
    # SuperScore still unopened) -- but `finalsActionsHtml` in
    # scorer_dashboard.html only ever showed the (paired) open-week button
    # while `lifecycle_state` itself read `not_created`/`upcoming`. Once
    # Finals opened, reloading the dashboard removed the only UI path back
    # to that supported retry. Surfacing whether SuperScore's own open is
    # still pending lets the template keep the button visible in exactly
    # that state, regardless of Finals' own lifecycle_state.
    superscore_open_pending = False
    if superscore_round_id is not None:
        superscore_persisted = CompetitionLifecycleRepository(database).get_round(superscore_round_id)
        superscore_open_pending = superscore_persisted is None or superscore_persisted.state == "upcoming"

    preflight = None
    if lifecycle_state in ("not_created", "upcoming"):
        preflight = build_finals_week_preflight(database, bracket_id, week_number)

    review = None
    if lifecycle_state in ("live", "review", "final") and any(m["matchup_id"] for m in matchups):
        # Presentational read only -- freshness is confirmed again (and
        # actually enforced) by `publish_finals_round`/`correct_finals_result`
        # themselves at the moment an operator actually publishes/corrects;
        # this dashboard read never claims evidence is fresh on their behalf.
        review = build_finals_round_review(
            lifecycle, round_review_repo, identities, finals_round_id, evidence_fresh=None
        )

    participant_ids = list_round_participant_entry_ids(database, finals_round_id)
    readiness = compute_round_readiness(
        database,
        identities,
        afl_client,
        season_id=season.season_id,
        competition_id=competition_id,
        round_id=finals_round_id,
        entry_ids=participant_ids,
        lifecycle_state=lifecycle_state,
    )

    # Issue #216: surface the existing bracket-advance action (issue #190/
    # #201's `FinalsBracketRepository.advance_bracket`/`preview_advance_
    # bracket`, previously CLI-only) directly on whichever finals week the
    # operator is looking at, in whichever of the two directions is
    # relevant -- never a silent auto-advance:
    #   - this week itself has no pairing yet (`week_number > 1` and no
    #     bye/matchups were derived) *and* the prior week is actually
    #     `final` -- the operator opened/selected the now-ready-to-progress
    #     week before running that progression;
    #   - or this week just published and the *next* week's pairing has not
    #     been derived yet -- the natural "what's next" nudge right after
    #     the week an operator just finished.
    # Codex review (PR #217, P2): a progression preview/apply can only ever
    # succeed once the *source* week is `final` (`FinalsBracketRepository.
    # advance_bracket`'s own precondition) -- since every week's round now
    # exists from bracket creation (issue #216's own selector work), an
    # operator can reach an unmaterialised week whose predecessor is still
    # `not_created`/`upcoming`/`open`/`live`. Advertising bracket
    # progression as the next safe action there would send them to a
    # preview that can only report a diagnostic; `blocked_by_week` instead
    # names the prior week to actually finish first.
    progression = None
    blocked_by_week = None
    if week_number > 1 and bye is None and not matchups:
        from_week = week_number - 1
        from_week_state = _week_lifecycle_state(database, bracket_id, from_week)
        if from_week_state == "final":
            progression = {
                "reason": "pairing_missing",
                "from_week": from_week,
                "from_week_label": WEEK_LABELS.get(from_week, f"Finals Week {from_week}"),
                "from_week_state": from_week_state,
                "target_week": week_number,
                "target_week_label": WEEK_LABELS.get(week_number, f"Finals Week {week_number}"),
                "preview_url": FINALS_ADVANCE_PREVIEW_URL.format(bracket_id=bracket_id, from_week=from_week),
                "apply_url": FINALS_ADVANCE_URL.format(bracket_id=bracket_id, from_week=from_week),
            }
        else:
            blocked_by_week = {
                "week_number": from_week,
                "week_label": WEEK_LABELS.get(from_week, f"Finals Week {from_week}"),
                "state": from_week_state,
                "round_id": bracket_repo.get_week_round_id(bracket_id, from_week),
            }
    elif lifecycle_state == "final" and week_number in (1, 2, 3):
        next_pairings = bracket_repo.list_pairings(bracket_id, week_number=week_number + 1)
        if not next_pairings:
            target_week = week_number + 1
            progression = {
                "reason": "ready_to_progress",
                "from_week": week_number,
                "from_week_label": WEEK_LABELS.get(week_number, f"Finals Week {week_number}"),
                "from_week_state": lifecycle_state,
                "target_week": target_week,
                "target_week_label": WEEK_LABELS.get(target_week, f"Finals Week {target_week}"),
                "preview_url": FINALS_ADVANCE_PREVIEW_URL.format(bracket_id=bracket_id, from_week=week_number),
                "apply_url": FINALS_ADVANCE_URL.format(bracket_id=bracket_id, from_week=week_number),
            }

    return {
        "available": True,
        "bracket_id": bracket_id,
        "round_id": finals_round_id,
        "week_number": week_number,
        "week_label": WEEK_LABELS.get(week_number, f"Finals Week {week_number}"),
        "lifecycle_state": lifecycle_state,
        "bye": bye,
        "matchups": matchups,
        "preflight": preflight,
        "review": review,
        "lineups": readiness["team_rows"],
        "lockout": {"triggers": readiness["trigger_rows"]},
        "trigger_plan_configured": readiness["trigger_plan_configured"],
        "lockout_evidence_unavailable": readiness["lockout_evidence_error"],
        "superscore_open_pending": superscore_open_pending,
        "progression": progression,
        "blocked_by_week": blocked_by_week,
        "open_week_url": (
            FINALS_OPEN_PAIRED_URL.format(bracket_id=bracket_id, week_number=week_number)
            if superscore_round_id is not None
            else FINALS_OPEN_URL.format(bracket_id=bracket_id, week_number=week_number)
        ),
        "advance_to_review_url": FINALS_ADVANCE_TO_REVIEW_URL.format(bracket_id=bracket_id, week_number=week_number),
        "publish_url": FINALS_PUBLISH_URL.format(bracket_id=bracket_id, week_number=week_number),
        "round_review_api_url": FINALS_ROUND_REVIEW_API_URL.format(round_id=finals_round_id),
        "calculate_url": FINALS_ROUND_REVIEW_API_URL.format(round_id=finals_round_id) + "/calculate",
        "dnp_url": FINALS_ROUND_REVIEW_API_URL.format(round_id=finals_round_id) + "/dnp",
        "interchange_url": FINALS_ROUND_REVIEW_API_URL.format(round_id=finals_round_id) + "/interchange",
        "override_url": FINALS_ROUND_REVIEW_API_URL.format(round_id=finals_round_id) + "/override",
    }


def _entry_action_required(review_status: str, adjudication_available: bool, effective_entry: dict | None) -> bool:
    """Whether a SuperScore entry's collapsed row (issue #216) should
    visibly flag scorer attention -- derived only from facts the existing
    DNP/vacancy/Interchange review/readiness model already computes: this
    dashboard's own `review_status`/`adjudication_available`, and each
    slot's `dnp_recommendation`/`dnp_ruling` in `effective_entry` (the
    identical `actionable` test `scorer_dashboard.html`'s `slotHtml`
    already applies when the row is expanded -- never a second, divergent
    rule). Once published, an entry's rulings are frozen and no further
    scorer action is possible from this dashboard."""
    if review_status == "published":
        return False
    if review_status in ("not_submitted", "stale_calculation"):
        return True
    if adjudication_available:
        return True
    if effective_entry is not None:
        for slot in effective_entry.get("slots") or ():
            if slot.get("dnp_ruling") is None and slot.get("dnp_recommendation") in (
                "review_required",
                "recommend_dnp",
            ):
                return True
    return False


def _build_superscore_section(database, identities, afl_client, season, week_number, superscore_round_id):
    round_label = f"SuperScore {week_number}"
    if superscore_round_id is None:
        return {
            "available": False,
            "week_number": week_number,
            "round_label": round_label,
            "message": f"SuperScore is not configured for week {week_number} of {season.label} yet.",
        }

    round_row = database.execute(
        "SELECT competition_id, label FROM bbbffl_round WHERE bbbffl_round_id=?", (superscore_round_id,)
    ).fetchone()
    competition_id = round_row["competition_id"]
    round_label = humanize_round_label("superscore", round_row["label"])
    persisted = CompetitionLifecycleRepository(database).get_round(superscore_round_id)
    lifecycle_state = persisted.state if persisted else "not_created"

    state_rows = database.execute(
        "SELECT season_entry_id, review_version FROM superscore_entry_review_state "
        "WHERE bbbffl_round_id=? ORDER BY season_entry_id",
        (superscore_round_id,),
    ).fetchall()
    entry_ids = [row["season_entry_id"] for row in state_rows]
    review_versions = {row["season_entry_id"]: row["review_version"] for row in state_rows}

    readiness = compute_round_readiness(
        database,
        identities,
        afl_client,
        season_id=season.season_id,
        competition_id=competition_id,
        round_id=superscore_round_id,
        entry_ids=entry_ids,
        lifecycle_state=lifecycle_state,
    )

    calc_rows = database.execute(
        "SELECT season_entry_id, revision, computed_as_of_review_version, total_score, snapshot "
        "FROM superscore_entry_calculation WHERE bbbffl_round_id=?",
        (superscore_round_id,),
    ).fetchall()
    calc_by_entry = {row["season_entry_id"]: row for row in calc_rows}

    # No matchup/opponent involved -- `leaderboard` is the entry-scoped,
    # rank-ordered SuperScore result read model (issue #192/#193), never an
    # ordinary-style head-to-head projection.
    leaderboard = SuperScoreLeaderboardService(database, afl_client, identities).leaderboard(superscore_round_id)
    published_entries = {entry["season_entry_id"]: entry for entry in leaderboard["entries"]} if leaderboard else {}

    entries = []
    for team in readiness["team_rows"]:
        entry_id = team["season_entry_id"]
        calc = calc_by_entry.get(entry_id)
        current_review_version = review_versions.get(entry_id)
        published = published_entries.get(entry_id)
        if published is not None:
            review_status = "published"
        elif calc is not None and calc["computed_as_of_review_version"] == current_review_version:
            review_status = "calculated"
        elif calc is not None:
            review_status = "stale_calculation"
        elif team["submission_state"] in NO_AUTHORITATIVE_SUBMISSION_STATES:
            review_status = "not_submitted"
        else:
            review_status = "submitted"
        snapshot = json.loads(calc["snapshot"]) if calc is not None else None
        if published is not None:
            # Once published, the leaderboard's frozen total_score is the
            # authoritative figure -- it must never drift out of step with
            # the (equally frozen) rank alongside it, even if a later,
            # unpublished recalculation changes `calc`.
            total_score = published["total_score"]
        elif calc is not None:
            total_score = float(calc["total_score"])
        else:
            total_score = None
        effective_entry = snapshot["effective_entry"] if snapshot is not None else None
        entries.append(
            {
                **team,
                "review_version": current_review_version,
                "review_status": review_status,
                "total_score": total_score,
                "rank": published["rank"] if published is not None else None,
                "is_joint_winner": published["is_joint_winner"] if published is not None else False,
                "effective_entry": effective_entry,
                "ruling_url": SUPERSCORE_RULING_URL.format(round_id=superscore_round_id, season_entry_id=entry_id),
                "action_required": _entry_action_required(
                    review_status, team["adjudication_available"], effective_entry
                ),
            }
        )

    return {
        "available": True,
        "round_id": superscore_round_id,
        "competition_id": competition_id,
        "week_number": week_number,
        "round_label": round_label,
        "lifecycle_state": lifecycle_state,
        "entries": entries,
        "entry_count": len(entries),
        "leaderboard": leaderboard,
        "lockout": {"triggers": readiness["trigger_rows"]},
        "calculate_url": SUPERSCORE_CALCULATE_URL.format(round_id=superscore_round_id),
        "advance_to_review_url": SUPERSCORE_ADVANCE_TO_REVIEW_URL.format(round_id=superscore_round_id),
        "publish_url": SUPERSCORE_PUBLISH_URL.format(round_id=superscore_round_id),
    }
