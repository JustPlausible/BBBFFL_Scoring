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
from app.finals import SLOT_LABELS, WEEK_LABELS, FinalsBracketRepository
from app.finals_participation import list_round_participant_entry_ids
from app.finals_preflight import build_finals_week_preflight
from app.finals_review import build_finals_round_review
from app.scorer_dashboard import NO_AUTHORITATIVE_SUBMISSION_STATES, compute_round_readiness, round_stream_type
from app.stream_presentation import humanize_round_label
from app.superscore_results import SuperScoreLeaderboardService

SUPERSCORE_CALCULATE_URL = "/api/season-superscore/scorer/rounds/{round_id}/calculate"
SUPERSCORE_ADVANCE_TO_REVIEW_URL = "/api/season-superscore/scorer/rounds/{round_id}/advance-to-review"
SUPERSCORE_PUBLISH_URL = "/api/season-superscore/scorer/rounds/{round_id}/publish"
SUPERSCORE_RULING_URL = "/api/scorer/superscore/rounds/{round_id}/entries/{season_entry_id}"
FINALS_OPEN_URL = "/api/admin/finals/{bracket_id}/weeks/{week_number}/open"
FINALS_ADVANCE_TO_REVIEW_URL = "/api/admin/finals/{bracket_id}/weeks/{week_number}/advance-to-review"
FINALS_PUBLISH_URL = "/api/admin/finals/{bracket_id}/weeks/{week_number}/publish"
FINALS_ROUND_REVIEW_API_URL = "/api/admin/round-review/{round_id}"


def _resolve_superscore_round_id(database, season_id: str, week_number: int) -> str | None:
    row = database.execute(
        "SELECT sr.bbbffl_round_id FROM bbbffl_round sr "
        "JOIN competition_stream sc ON sc.competition_id=sr.competition_id "
        "WHERE sc.season_id=? AND sc.stream_type='superscore' AND sr.round_key=?",
        (season_id, f"ss{week_number}"),
    ).fetchone()
    return row["bbbffl_round_id"] if row else None


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
    )
    superscore_section = _build_superscore_section(
        database, identities, afl_client, season, week_number, superscore_round_id
    )
    # A small, week-scoped navigation affordance (never the full-season
    # ordinary round list -- that stays `app.scorer_dashboard`'s own
    # concern) so the round selector has *something* to switch between
    # while viewing this week, rather than disappearing entirely.
    round_options = []
    if finals_section["available"]:
        round_options.append(
            {
                "bbbffl_round_id": finals_section["round_id"],
                "round_label": finals_section["week_label"],
                "state": finals_section["lifecycle_state"],
            }
        )
    if superscore_section["available"]:
        round_options.append(
            {
                "bbbffl_round_id": superscore_section["round_id"],
                "round_label": superscore_section["round_label"],
                "state": superscore_section["lifecycle_state"],
            }
        )
    return {
        "season": {"season_id": season.season_id, "year": season.year, "label": season.label},
        "stream": "finals_week",
        "week_number": week_number,
        "week_label": WEEK_LABELS.get(week_number, f"Finals Week {week_number}"),
        "round_options": round_options,
        "round": None,
        "finals": finals_section,
        "superscore": superscore_section,
    }


def _build_finals_section(
    database, lifecycle, identities, round_review_repo, afl_client, season, bracket_id, week_number, finals_round_id
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
        "open_week_url": FINALS_OPEN_URL.format(bracket_id=bracket_id, week_number=week_number),
        "advance_to_review_url": FINALS_ADVANCE_TO_REVIEW_URL.format(bracket_id=bracket_id, week_number=week_number),
        "publish_url": FINALS_PUBLISH_URL.format(bracket_id=bracket_id, week_number=week_number),
        "round_review_api_url": FINALS_ROUND_REVIEW_API_URL.format(round_id=finals_round_id),
        "calculate_url": FINALS_ROUND_REVIEW_API_URL.format(round_id=finals_round_id) + "/calculate",
        "dnp_url": FINALS_ROUND_REVIEW_API_URL.format(round_id=finals_round_id) + "/dnp",
        "interchange_url": FINALS_ROUND_REVIEW_API_URL.format(round_id=finals_round_id) + "/interchange",
        "override_url": FINALS_ROUND_REVIEW_API_URL.format(round_id=finals_round_id) + "/override",
    }


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
        entries.append(
            {
                **team,
                "review_version": current_review_version,
                "review_status": review_status,
                "total_score": float(calc["total_score"]) if calc is not None else None,
                "rank": published["rank"] if published is not None else None,
                "is_joint_winner": published["is_joint_winner"] if published is not None else False,
                "effective_entry": snapshot["effective_entry"] if snapshot is not None else None,
                "ruling_url": SUPERSCORE_RULING_URL.format(round_id=superscore_round_id, season_entry_id=entry_id),
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
