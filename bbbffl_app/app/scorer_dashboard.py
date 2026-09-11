"""Server-side read model for the Scorer Operations Dashboard (issue #147).

This module aggregates existing authoritative domain reads -- competition
lifecycle (`app.competition_lifecycle`), evidence-backed round preflight
(`app.round_preflight`, issue #152), round review/sign-off
(`app.round_review`, issue #58), lockout/trigger evidence (`app.lockouts`),
weekly lineups (`app.lineups`/`app.coach_lineup`), human-readable identity
(`app.identity`/`app.player_pool`/`app.season`, issue #151) and audit
history (`app.audit`) -- into one coherent per-round view: a deterministic
"next safe action", a categorised attention queue, per-team lineup
readiness, lockout/trigger state, review/publication readiness and recent
audited activity.

Deliberately narrow role: this is an *aggregation and navigation* read
model, never a second implementation of any workflow. It performs no
domain mutations, persists no dashboard-specific state, and never invents
its own mapping/lockout-recommendation or DNP/interchange/sign-off rule --
every fact shown here is read fresh, on every call, from the same
authoritative repositories the owning workflow pages already use (issue
#153: nothing here can drift from or contradict those pages), and every
actionable item links to the existing page that actually performs the
mutation.
"""

import dataclasses
from contextlib import nullcontext
from dataclasses import dataclass

from app.afl_client import AflApiError
from app.audit import (
    DNP_CHANGED,
    ENTITY_TYPE_LINEUP,
    INTERCHANGE_CHANGED,
    LINEUP_ADJUDICATED,
    LINEUP_CORRECTED,
    LINEUP_SUBMITTED,
    OVERRIDE_CHANGED,
)
from app.coach_lineup import ORDINARY_POSITIONS
from app.lineups import WeeklyLineupRepository
from app.lockouts import (
    LockoutRepository,
    LockoutTriggerRepository,
    LockState,
    MatchResolutionError,
    RoundMatchFactsProvider,
)
from app.round_mapping import RoundMappingRepository
from app.round_preflight import build_round_preflight
from app.round_review import build_round_review, calculation_staleness_for_entry

# -- Attention-queue categories, in priority order -------------------------
CATEGORY_BLOCKING = "blocking"
CATEGORY_DECISION_REQUIRED = "decision_required"
CATEGORY_WAITING = "waiting"
CATEGORY_ADVISORY = "advisory"
CATEGORY_COMPLETED = "completed"
CATEGORY_ORDER = (
    CATEGORY_BLOCKING,
    CATEGORY_DECISION_REQUIRED,
    CATEGORY_WAITING,
    CATEGORY_ADVISORY,
    CATEGORY_COMPLETED,
)

# Per-team lineup-readiness summary states. Deliberately distinct strings,
# never reused as a mutation/domain vocabulary -- purely how this dashboard
# presents facts `app.lineups`/`app.coach_lineup` already hold.
SUBMISSION_MISSING = "missing"
SUBMISSION_DRAFT_ONLY = "draft_only"
SUBMISSION_DIVERGED = "diverged"
SUBMISSION_INCOMPLETE = "incomplete"
SUBMISSION_SUBMITTED = "submitted"

# A team in either state has no authoritative submission on record -- a
# saved-but-never-submitted private draft is exactly the case issue #146's
# missed-submission adjudication resolves (its own "evidenced draft"
# resolution reads the draft's own content), so both states are equally
# "missing" for adjudication-eligibility/attention purposes (Codex review,
# PR #159): only `SUBMISSION_MISSING` was checked before, silently
# excluding a coach who saved a draft but never pressed Submit.
NO_AUTHORITATIVE_SUBMISSION_STATES = (SUBMISSION_MISSING, SUBMISSION_DRAFT_ONLY)

ROUND_CENTRE_URL = "/scorer/round-centre/{round_id}"
PREFLIGHT_URL = "/admin/round-preflight/{round_id}"
LINEUP_CORRECTION_URL = "/scorer/lineup-correction/{round_id}"
LINEUP_ADJUDICATION_URL = "/scorer/lineup-adjudication/{round_id}"
DELEGATED_LINEUP_URL = "/operations/rounds/{round_id}/lineup"
PUBLIC_ROUND_CENTRE_URL = "/seasons/{season_id}/rounds/{round_id}"


@dataclass(frozen=True)
class NextAction:
    code: str
    category: str
    title: str
    detail: str
    url: str | None
    capability: str | None = None


class _CachedMatchFacts:
    """Wraps a `MatchFactsProvider` and memoizes `matches_for(...)` for the
    lifetime of one dashboard build (Codex review, PR #159): building a
    dashboard evaluates trigger activation once and then per-position lock
    state for every team's lineup, each of which independently asks its
    `match_facts` collaborator for this round's matches. Without this, a
    ten-team round issues roughly one live AFL match-list request per team
    (`app.afl_resilience.ResilientAflClient` retries/caches transport
    failures, but still attempts a live request on every call) -- wildly
    more than the round's evidence actually changes within one read.
    `evaluation_at` is passed straight through, never cached, so replay/
    live clock semantics are unaffected."""

    def __init__(self, inner):
        self._inner = inner
        self._cache: dict[str, list] = {}

    def matches_for(self, bbbffl_round_id: str) -> list:
        if bbbffl_round_id not in self._cache:
            self._cache[bbbffl_round_id] = self._inner.matches_for(bbbffl_round_id)
        return self._cache[bbbffl_round_id]

    def evaluation_at(self):
        inner_evaluation_at = getattr(self._inner, "evaluation_at", None)
        return inner_evaluation_at() if callable(inner_evaluation_at) else None


def ordinary_rounds_with_lifecycle(database, season_id: str) -> list[dict]:
    """Every logical ordinary `bbbffl_round` definition for a season, left-
    joined against its (possibly absent) `bbbffl_round_lifecycle` row.

    This is the one authoritative place the *definitions-vs-lifecycle*
    distinction (issue #148) is read: a round that exists here with
    `round_state is None` is a configured round definition that has never
    been opened -- never conflate that with "0 rounds created". Shared
    verbatim by the Scorer Operations Dashboard (issue #147) and the
    Administrator Dashboard (issue #148, `app.admin_dashboard`) so both
    surfaces can never disagree about which rounds exist or their lifecycle
    state (issue #153)."""
    rows = database.execute(
        "SELECT r.bbbffl_round_id, r.label round_label, r.sequence, r.competition_id, "
        "c.season_id, c.label competition_label, "
        "l.state round_state, l.version round_version, l.afl_season_id, l.afl_round_id, "
        "l.mapping_id, l.mapping_revision "
        "FROM bbbffl_round r "
        "JOIN competition_stream c ON c.competition_id=r.competition_id "
        "LEFT JOIN bbbffl_round_lifecycle l ON l.bbbffl_round_id=r.bbbffl_round_id "
        "WHERE c.season_id=? AND c.stream_type='ordinary' "
        "ORDER BY r.sequence",
        (season_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def select_current_round(rounds: list[dict], requested_round_id: str | None) -> dict | None:
    """Deterministic round selection for the dashboard's default view.

    An explicit `requested_round_id` wins whenever it names a real ordinary
    round in this season. Otherwise the *current* round is the earliest
    (by fixture sequence) round whose persisted lifecycle is not yet
    `final` -- the round most likely to need scorer attention right now;
    once every round is final, the most recent (final) round is shown so
    the dashboard has something authoritative to describe ("prepare next
    round"). Never guesses from a browser-supplied identifier alone: the
    caller (the route layer) is responsible for confirming `requested_round_id`
    belongs to a season this principal is authorised to view before it
    ever reaches here.
    """
    if not rounds:
        return None
    if requested_round_id is not None:
        match = next((r for r in rounds if r["bbbffl_round_id"] == requested_round_id), None)
        if match is not None:
            return match
    current = next((r for r in rounds if (r["round_state"] or "not_created") != "final"), None)
    return current or rounds[-1]


def _round_option(row: dict) -> dict:
    return {
        "bbbffl_round_id": row["bbbffl_round_id"],
        "round_label": row["round_label"],
        "sequence": row["sequence"],
        "state": row["round_state"] or "not_created",
    }


def _entry_ids_for_round(database, identities, season_id: str, competition_id: str, round_id: str) -> list[str]:
    rows = database.execute(
        "SELECT home_season_entry_id, away_season_entry_id FROM bbbffl_matchup WHERE bbbffl_round_id=?",
        (round_id,),
    ).fetchall()
    if rows:
        ids = sorted({row["home_season_entry_id"] for row in rows} | {row["away_season_entry_id"] for row in rows})
        return ids
    row = database.execute(
        "SELECT d.fixture_draw_id FROM season_fixture_draw d WHERE d.season_id=? AND d.state='frozen'",
        (season_id,),
    ).fetchone()
    if row is not None:
        pairs = database.execute(
            "SELECT home_season_entry_id, away_season_entry_id FROM season_fixture_matchup WHERE fixture_draw_id=?",
            (row["fixture_draw_id"],),
        ).fetchall()
        ids = sorted(
            {pair["home_season_entry_id"] for pair in pairs} | {pair["away_season_entry_id"] for pair in pairs}
        )
        if ids:
            return ids
    return sorted(entry.season_entry_id for entry in identities.list_entries(season_id))


def _team_submission_state(draft, submission) -> str:
    if draft is None:
        return SUBMISSION_MISSING
    if submission is None:
        return SUBMISSION_DRAFT_ONLY
    if draft.revision > submission.based_on_draft_revision:
        return SUBMISSION_DIVERGED
    if any(submission.positions.get(position) is None for position in ORDINARY_POSITIONS):
        return SUBMISSION_INCOMPLETE
    return SUBMISSION_SUBMITTED


def _position_lock_summary(positions: dict) -> dict:
    editable = sum(1 for p in positions.values() if p.state == LockState.EDITABLE)
    # issue #185: an invalid selection (known AFL bye) that no trigger
    # actually covers is still an editable position -- counted separately
    # from `editable` so a Scorer can see it needs attention (a replacement
    # is required before ordinary submission succeeds) without it being
    # miscounted as either an ordinary open position or a lockout.
    invalid_selection = sum(1 for p in positions.values() if p.state == LockState.INVALID_SELECTION)
    locked_selective = sum(
        1 for p in positions.values() if p.state == LockState.LOCKED and p.reason == "selective_trigger_activated"
    )
    locked_main = sum(
        1 for p in positions.values() if p.state == LockState.LOCKED and p.reason == "main_lockout_triggered"
    )
    locked_other = sum(
        1
        for p in positions.values()
        if p.state == LockState.LOCKED and p.reason not in ("selective_trigger_activated", "main_lockout_triggered")
    )
    indeterminate = sum(1 for p in positions.values() if p.state == LockState.INDETERMINATE)
    return {
        "editable": editable,
        "invalid_selection": invalid_selection,
        "locked_selective": locked_selective,
        "locked_main": locked_main,
        "locked_other": locked_other,
        "indeterminate": indeterminate,
        "total": len(positions),
    }


def _build_team_readiness(
    lineups_repo,
    lockouts_repo,
    identities,
    match_facts,
    *,
    season_id: str,
    competition_id: str,
    round_id: str,
    entry_ids: list[str],
    lifecycle_state: str,
    any_trigger_activated: bool,
) -> list[dict]:
    entry_meta = {entry.season_entry_id: entry for entry in identities.list_entries(season_id)}
    rows = []
    for entry_id in entry_ids:
        meta = entry_meta.get(entry_id)
        team_name = meta.team_name if meta else None
        coach_name = meta.coach_display_name if meta else None
        draft = lineups_repo.get_draft(season_id, competition_id, round_id, entry_id)
        submission = lineups_repo.get_effective_submission(draft.lineup_id) if draft is not None else None
        state = _team_submission_state(draft, submission)
        lock_summary = None
        if draft is not None and lifecycle_state == "final":
            # A `final` round's lockout evidence is done and immutable --
            # its published result already reflects whatever locked/
            # unlocked while it was live, and nothing here ever re-derives
            # or changes that after the fact. Project it from what is
            # already durably persisted rather than `lock_state` (issue
            # #176: the round's mapped AFL round may by now sit outside the
            # currently active replay evidence package entirely, e.g. a
            # prior evidence-package boundary, and this historical summary
            # never actually needs to fetch it again to redisplay history).
            effective_positions = submission.positions if submission is not None else draft.positions
            view = lockouts_repo.persisted_lock_state(draft.lineup_id, round_id, entry_id, effective_positions)
            lock_summary = _position_lock_summary(view.positions)
        elif draft is not None and lifecycle_state not in ("not_created", "upcoming"):
            effective_positions = submission.positions if submission is not None else draft.positions
            try:
                view = lockouts_repo.lock_state(
                    draft.lineup_id, round_id, entry_id, effective_positions, match_facts=match_facts
                )
                lock_summary = _position_lock_summary(view.positions)
            except (AflApiError, MatchResolutionError):
                lock_summary = None
        adjudication_available = (
            state in NO_AUTHORITATIVE_SUBMISSION_STATES
            and lifecycle_state in ("live", "review")
            and any_trigger_activated
        )
        # Correction (issue #137) is the audited path into an *already-
        # locked* position specifically -- never a substitute for ordinary
        # resubmission/delegated entry while every position remains
        # editable (Codex review, PR #159). Gate on `lock_summary` actually
        # showing a locked position, not merely on round state.
        locked_positions = (
            lock_summary["locked_selective"] + lock_summary["locked_main"] + lock_summary["locked_other"]
            if lock_summary is not None
            else 0
        )
        correction_available = (
            state in (SUBMISSION_SUBMITTED, SUBMISSION_INCOMPLETE, SUBMISSION_DIVERGED)
            and lifecycle_state in ("open", "live", "review")
            and locked_positions > 0
        )
        corrections = lineups_repo.list_corrections(draft.lineup_id) if draft is not None else []
        rows.append(
            {
                "season_entry_id": entry_id,
                "lineup_id": draft.lineup_id if draft is not None else None,
                "team_name": team_name,
                "coach_name": coach_name,
                "submission_state": state,
                "has_private_draft": draft is not None,
                "draft_revision": draft.revision if draft is not None else None,
                "draft_updated_at": draft.updated_at if draft is not None else None,
                "submission_version": submission.version if submission is not None else None,
                "submitted_at": submission.submitted_at if submission is not None else None,
                "submission_source_type": submission.source_type if submission is not None else None,
                "lock_summary": lock_summary,
                "correction_available": correction_available,
                "correction_url": LINEUP_CORRECTION_URL.format(round_id=round_id) if correction_available else None,
                "correction_count": len(corrections),
                "adjudication_available": adjudication_available,
                "adjudication_url": (
                    LINEUP_ADJUDICATION_URL.format(round_id=round_id) if adjudication_available else None
                ),
                "delegated_lineup_url": DELEGATED_LINEUP_URL.format(round_id=round_id),
            }
        )
    return rows


def _build_trigger_rows(trigger_views, match_by_id: dict) -> list[dict]:
    rows = []
    for trigger in trigger_views:
        configured = []
        for match_dict in trigger.configured_matches:
            match_id = match_dict["afl_match_id"]
            match = match_by_id.get(match_id)
            configured.append(
                {
                    "afl_match_id": match_id,
                    "home_team": match.home_team.name if match else None,
                    "away_team": match.away_team.name if match else None,
                    "observed_status": match_dict["observed_status"],
                    "start_time_utc": match_dict["start_time_utc"],
                }
            )
        activating_match = match_by_id.get(trigger.activating_afl_match_id)
        rows.append(
            {
                "trigger_id": trigger.trigger_id,
                "trigger_key": trigger.trigger_key,
                "trigger_type": trigger.trigger_type,
                "sequence": trigger.sequence,
                "configured_matches": configured,
                "activated": trigger.activated,
                "activation_reason": trigger.activation_reason,
                "activating_afl_match_id": trigger.activating_afl_match_id,
                "activating_home_team": activating_match.home_team.name if activating_match else None,
                "activating_away_team": activating_match.away_team.name if activating_match else None,
                "effective_lock_at": trigger.effective_lock_at,
                "observed_status": trigger.observed_status,
            }
        )
    return rows


_BLOCKER_CODE_TITLES = {
    "mapping_missing": "AFL round mapping not yet accepted",
    "mapping_unresolved": "AFL round mapping not yet accepted",
    "fixture_invalid": "Fixture not ready",
    "afl_evidence_unavailable": "AFL match evidence unavailable",
    "afl_evidence_stale": "AFL match evidence is stale",
    "afl_matches_missing": "Mapped AFL round has no matches",
    "match_schedule_missing": "AFL match missing a scheduled start",
    "match_status_unknown": "AFL match has an unrecognised status",
    "lockout_match_unresolved": "Lockout trigger references an unmapped match",
    "main_lockout_incomplete": "Lockout plan is missing its main trigger",
    "opening_round_integrity_conflict": "Opening Round nomination conflict",
    "opening_round_nominations_incomplete": "Opening Round nominations incomplete",
}


def _preflight_attention(preflight: dict, round_id: str) -> list[dict]:
    url = PREFLIGHT_URL.format(round_id=round_id)
    items = []
    for blocker in preflight["readiness"]["blockers"]:
        items.append(
            {
                "category": CATEGORY_BLOCKING,
                "code": f"preflight:{blocker['code']}",
                "title": _BLOCKER_CODE_TITLES.get(blocker["code"], "Round preflight blocker"),
                "detail": blocker["message"],
                "state": "preflight",
                "timestamp": None,
                "capability": "roundsetup.manage",
                "url": url,
                "diagnostics": {"code": blocker["code"]},
            }
        )
    for advisory in preflight["readiness"]["advisories"]:
        items.append(
            {
                "category": CATEGORY_ADVISORY,
                "code": f"preflight:{advisory['code']}",
                "title": "Preflight advisory",
                "detail": advisory["message"],
                "state": "preflight",
                "timestamp": None,
                "capability": "roundsetup.manage",
                "url": url,
                "diagnostics": {"code": advisory["code"]},
            }
        )
    return items


def _classify_matchup_blocker(text: str) -> str:
    lowered = text.lower()
    if "dnp status unresolved" in lowered or "interchange" in lowered:
        return CATEGORY_DECISION_REQUIRED
    if "no calculated result" in lowered:
        return CATEGORY_DECISION_REQUIRED
    if "recalculate" in lowered or "corrected" in lowered:
        return CATEGORY_DECISION_REQUIRED
    if "afl evidence" in lowered:
        return CATEGORY_WAITING
    return CATEGORY_ADVISORY


def _review_attention(round_review: dict, round_id: str) -> list[dict]:
    url = ROUND_CENTRE_URL.format(round_id=round_id)
    items = []
    for blocker in round_review["blockers"]:
        items.append(
            {
                "category": CATEGORY_BLOCKING,
                "code": "round_review:round_blocker",
                "title": "Round not ready for review",
                "detail": blocker,
                "state": round_review["state"],
                "timestamp": None,
                "capability": "round.review",
                "url": url,
                "diagnostics": None,
            }
        )
    for matchup in round_review["matchups"]:
        for blocker in matchup["blockers"]:
            items.append(
                {
                    "category": _classify_matchup_blocker(blocker),
                    "code": "round_review:matchup_blocker",
                    "title": f"Matchup {matchup['matchup_order']}",
                    "detail": blocker,
                    "state": round_review["state"],
                    "timestamp": None,
                    "capability": "round.review",
                    "url": url,
                    "diagnostics": {"matchup_id": matchup["matchup_id"]},
                }
            )
    return items


def _team_attention(team_rows: list[dict], round_id: str, season_id: str) -> list[dict]:
    items = []
    for team in team_rows:
        label = team["team_name"] or f"Unknown team ({team['season_entry_id']})"
        if team["submission_state"] in NO_AUTHORITATIVE_SUBMISSION_STATES:
            if team["adjudication_available"]:
                items.append(
                    {
                        "category": CATEGORY_DECISION_REQUIRED,
                        "code": "lineup:missed_submission",
                        "title": f"{label}: missed initial submission",
                        "detail": (
                            "No authoritative submission exists and a lockout trigger has already activated -- "
                            "adjudicate under issue #146's missed-submission workflow."
                        ),
                        "state": team["submission_state"],
                        "timestamp": None,
                        "capability": "lineup.adjudicate_missed_submission",
                        "url": team["adjudication_url"],
                        "diagnostics": {"season_entry_id": team["season_entry_id"]},
                    }
                )
            else:
                items.append(
                    {
                        "category": CATEGORY_WAITING,
                        "code": "lineup:awaiting_submission",
                        "title": f"{label}: no submission yet",
                        "detail": "Waiting on the represented coach (or a delegated proxy entry) to submit a lineup.",
                        "state": team["submission_state"],
                        "timestamp": None,
                        "capability": "lineup.proxy",
                        "url": team["delegated_lineup_url"],
                        "diagnostics": {"season_entry_id": team["season_entry_id"]},
                    }
                )
        elif team["submission_state"] == SUBMISSION_DIVERGED:
            items.append(
                {
                    "category": CATEGORY_ADVISORY,
                    "code": "lineup:draft_diverges",
                    "title": f"{label}: private draft differs from the submitted lineup",
                    "detail": (
                        "The private draft has changed since the last authoritative submission; only the "
                        "submitted lineup is scored unless resubmitted or corrected."
                    ),
                    "state": team["submission_state"],
                    "timestamp": team["draft_updated_at"],
                    "capability": "lineup.proxy",
                    "url": team["delegated_lineup_url"],
                    "diagnostics": {"season_entry_id": team["season_entry_id"]},
                }
            )
        elif team["submission_state"] == SUBMISSION_INCOMPLETE:
            items.append(
                {
                    "category": CATEGORY_ADVISORY,
                    "code": "lineup:incomplete",
                    "title": f"{label}: submitted lineup has vacant positions",
                    "detail": "Deliberately vacant ordinary positions score zero; confirm this was intentional.",
                    "state": team["submission_state"],
                    "timestamp": team["submitted_at"],
                    "capability": None,
                    "url": ROUND_CENTRE_URL.format(round_id=round_id),
                    "diagnostics": {"season_entry_id": team["season_entry_id"]},
                }
            )
        if team.get("calculation_stale"):
            items.append(
                {
                    "category": CATEGORY_DECISION_REQUIRED,
                    "code": "lineup:calculation_stale",
                    "title": f"{label}: recalculation required",
                    "detail": (
                        "This team's effective submission changed (correction or adjudication) after the round "
                        "was last calculated; recalculate before relying on or signing off these scores."
                    ),
                    "state": team["submission_state"],
                    "timestamp": None,
                    "capability": "round.review",
                    "url": ROUND_CENTRE_URL.format(round_id=round_id),
                    "diagnostics": {"season_entry_id": team["season_entry_id"]},
                }
            )
    return items


def _trigger_attention(
    lifecycle_state: str,
    trigger_rows: list[dict],
    round_id: str,
    *,
    trigger_plan_configured: bool,
    evidence_unavailable: bool,
) -> list[dict]:
    if lifecycle_state not in ("open", "live"):
        return []
    if not trigger_plan_configured:
        return [
            {
                "category": CATEGORY_BLOCKING,
                "code": "lockout:not_configured",
                "title": "No lockout plan configured",
                "detail": "This round has no lockout trigger plan; configure it in Round Preflight.",
                "state": lifecycle_state,
                "timestamp": None,
                "capability": "roundsetup.manage",
                "url": PREFLIGHT_URL.format(round_id=round_id),
                "diagnostics": None,
            }
        ]
    if evidence_unavailable:
        # A plan *is* configured -- a provider outage evaluating its live
        # activation is never the same fact as nothing having been
        # configured (Codex review, PR #159). The `lockout:evidence_
        # unavailable` item `_build_round_dashboard` adds separately
        # already names the actual problem; this must not additionally
        # claim the plan itself is missing.
        return []
    if not any(row["activated"] for row in trigger_rows):
        return [
            {
                "category": CATEGORY_WAITING,
                "code": "lockout:awaiting_first",
                "title": "Awaiting first lockout",
                "detail": "No configured trigger has activated yet.",
                "state": lifecycle_state,
                "timestamp": None,
                "capability": None,
                "url": ROUND_CENTRE_URL.format(round_id=round_id),
                "diagnostics": None,
            }
        ]
    main = next((row for row in trigger_rows if row["trigger_type"] == "main"), None)
    if main is not None and not main["activated"]:
        return [
            {
                "category": CATEGORY_WAITING,
                "code": "lockout:awaiting_main",
                "title": "Awaiting main lockout",
                "detail": "A selective trigger has activated; the main lockout has not yet fired.",
                "state": lifecycle_state,
                "timestamp": None,
                "capability": None,
                "url": ROUND_CENTRE_URL.format(round_id=round_id),
                "diagnostics": None,
            }
        ]
    return []


def _sort_key(item: dict) -> tuple:
    return (CATEGORY_ORDER.index(item["category"]), item.get("title") or "")


def _determine_next_action(
    *,
    lifecycle_state: str,
    preflight: dict | None,
    trigger_rows: list[dict],
    team_rows: list[dict],
    round_review: dict | None,
    round_id: str,
    season_id: str,
    next_round_id: str | None,
    all_matches_finished: bool,
    trigger_plan_configured: bool,
    evidence_unavailable: bool,
) -> dict:
    round_url = ROUND_CENTRE_URL.format(round_id=round_id)
    preflight_url = PREFLIGHT_URL.format(round_id=round_id)

    if lifecycle_state in ("not_created", "upcoming"):
        blockers = preflight["readiness"]["blockers"] if preflight else []
        lockout_only = bool(blockers) and all(
            b["code"] in ("lockout_match_unresolved", "main_lockout_incomplete") for b in blockers
        )
        if not blockers and preflight is not None and preflight["readiness"]["safe_to_open"]:
            return NextAction(
                "open_round",
                CATEGORY_BLOCKING,
                "Open round",
                "Preflight is satisfied; open the round.",
                preflight_url,
                capability="roundsetup.manage",
            ).__dict__
        if lockout_only:
            return NextAction(
                "configure_lockout_plan",
                CATEGORY_BLOCKING,
                "Configure lockout plan",
                "Mapping is accepted; configure the selective/main lockout trigger plan before opening.",
                preflight_url,
                capability="roundsetup.manage",
            ).__dict__
        return NextAction(
            "complete_preflight",
            CATEGORY_BLOCKING,
            "Complete round preflight",
            "Accept the AFL mapping and satisfy every preflight blocker before this round can open.",
            preflight_url,
            capability="roundsetup.manage",
        ).__dict__

    missing = [t for t in team_rows if t["submission_state"] in NO_AUTHORITATIVE_SUBMISSION_STATES]
    any_activated = any(row["activated"] for row in trigger_rows)
    main_row = next((row for row in trigger_rows if row["trigger_type"] == "main"), None)
    main_activated = bool(main_row and main_row["activated"])
    adjudication_pending = any(t["adjudication_available"] for t in missing)

    if lifecycle_state == "open":
        if not trigger_plan_configured:
            return NextAction(
                "configure_lockout_plan",
                CATEGORY_BLOCKING,
                "Configure lockout plan",
                "This round has opened without a lockout trigger plan; configure it now.",
                preflight_url,
                capability="roundsetup.manage",
            ).__dict__
        if evidence_unavailable:
            # A plan exists but its live activation could not be evaluated
            # -- never conflate that provider outage with "not configured"
            # (Codex review, PR #159): the operator needs to wait for
            # evidence, not repeat configuration that already happened.
            return NextAction(
                "await_lockout_evidence",
                CATEGORY_WAITING,
                "Await lockout evidence",
                "A lockout plan is configured, but the AFL match evidence needed to evaluate its activation is "
                "currently unavailable.",
                round_url,
            ).__dict__
        if not any_activated:
            return NextAction(
                "await_first_lockout",
                CATEGORY_WAITING,
                "Await first lockout",
                "No configured trigger has activated yet.",
                round_url,
            ).__dict__
        if adjudication_pending:
            return NextAction(
                "review_missed_submission_adjudication",
                CATEGORY_DECISION_REQUIRED,
                "Review missed-submission adjudication",
                "A trigger has locked at least one team with no submission; adjudicate under issue #146.",
                LINEUP_ADJUDICATION_URL.format(round_id=round_id),
                capability="lineup.adjudicate_missed_submission",
            ).__dict__
        if missing and not main_activated:
            return NextAction(
                "complete_remaining_lineups",
                CATEGORY_WAITING,
                "Complete remaining lineups",
                f"{len(missing)} team(s) have not submitted a lineup yet.",
                DELEGATED_LINEUP_URL.format(round_id=round_id),
                capability="lineup.proxy",
            ).__dict__
        if not main_activated:
            return NextAction(
                "await_main_lockout",
                CATEGORY_WAITING,
                "Await main lockout",
                "Selective lockout has activated; the main lockout has not yet fired.",
                round_url,
            ).__dict__
        return NextAction(
            "advance_to_live",
            CATEGORY_BLOCKING,
            "Advance round to live",
            "The main lockout has activated; advance the round to live.",
            round_url,
            capability="round.review",
        ).__dict__

    if lifecycle_state == "live":
        if adjudication_pending:
            return NextAction(
                "review_missed_submission_adjudication",
                CATEGORY_DECISION_REQUIRED,
                "Review missed-submission adjudication",
                "At least one team never submitted a lineup and lockout has activated; adjudicate under issue #146.",
                LINEUP_ADJUDICATION_URL.format(round_id=round_id),
                capability="lineup.adjudicate_missed_submission",
            ).__dict__
        if not all_matches_finished:
            # `live` begins at main lockout and can span the entire set of
            # mapped AFL matches -- `transition` performs no match-
            # completion validation of its own, so this dashboard must
            # never advertise "advance to review" as safe while games are
            # still in progress (Codex review, PR #159).
            return NextAction(
                "await_match_completion",
                CATEGORY_WAITING,
                "Await match completion",
                "Not every mapped AFL match has finished; advancing to review is not yet safe.",
                round_url,
            ).__dict__
        return NextAction(
            "advance_to_review",
            CATEGORY_BLOCKING,
            "Advance round to review",
            "Every mapped match has finished; advance the round to review before calculating official scores.",
            round_url,
            capability="round.review",
        ).__dict__

    if lifecycle_state == "review":
        if round_review is None:
            return NextAction(
                "calculate_scores",
                CATEGORY_BLOCKING,
                "Calculate/refresh scores",
                "No calculation exists yet.",
                round_url,
                capability="round.review",
            ).__dict__
        if not all(m["evidence_fresh"] for m in round_review["matchups"]):
            return NextAction(
                "await_final_evidence",
                CATEGORY_WAITING,
                "Await final AFL evidence",
                "AFL evidence behind at least one calculated result was not confirmed fresh.",
                round_url,
            ).__dict__
        if any(m["calculation_revision"] is None for m in round_review["matchups"]):
            return NextAction(
                "calculate_scores",
                CATEGORY_BLOCKING,
                "Calculate/refresh scores",
                "At least one matchup has not been calculated yet.",
                round_url,
                capability="round.review",
            ).__dict__
        if any(t.get("calculation_stale") for t in team_rows):
            return NextAction(
                "recalculate_after_correction",
                CATEGORY_DECISION_REQUIRED,
                "Recalculate after correction",
                "A correction or adjudication changed an effective submission after the last calculation.",
                round_url,
                capability="round.review",
            ).__dict__
        if not round_review["ready_for_signoff"]:
            return NextAction(
                "resolve_scorer_decisions",
                CATEGORY_DECISION_REQUIRED,
                "Resolve scorer decisions",
                "Unresolved DNP/Interchange rulings or matchup blockers remain before sign-off.",
                round_url,
                capability="round.review",
            ).__dict__
        return NextAction(
            "ready_for_signoff",
            CATEGORY_DECISION_REQUIRED,
            "Ready for atomic sign-off",
            "Every matchup is calculated and blocker-free; publish all five results.",
            round_url,
            capability="round.review",
        ).__dict__

    if lifecycle_state == "final":
        if next_round_id:
            return NextAction(
                "published_prepare_next_round",
                CATEGORY_ADVISORY,
                "Published — prepare next round",
                "This round is published. Move on to the next round's preflight.",
                PREFLIGHT_URL.format(round_id=next_round_id),
                capability="roundsetup.manage",
            ).__dict__
        return NextAction(
            "published_season_complete",
            CATEGORY_ADVISORY,
            "Published — season complete",
            "This round is published and no further ordinary round is configured for this season.",
            None,
        ).__dict__

    return NextAction(
        "unknown", CATEGORY_ADVISORY, "Review round state", "Unrecognised round lifecycle state.", round_url
    ).__dict__


def _recent_activity(audit_events, round_id: str, matchup_ids: list[str], lineup_ids: list[str], *, limit: int = 15):
    events = list(audit_events.list_events(entity_type="competition.round", entity_id=round_id, limit=10))
    for matchup_id in matchup_ids:
        events += audit_events.list_events(entity_type="competition.matchup", entity_id=matchup_id, limit=5)
    for lineup_id in lineup_ids:
        events += audit_events.list_events(entity_type=ENTITY_TYPE_LINEUP, entity_id=lineup_id, limit=5)
    matchup_id_set = set(matchup_ids)
    for action in (DNP_CHANGED, INTERCHANGE_CHANGED, OVERRIDE_CHANGED):
        candidates = audit_events.list_events(action=action, limit=150)
        events += [event for event in candidates if event.entity_id.split(":", 1)[0] in matchup_id_set]
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
    return deduped


_ACTIVITY_LABELS = {
    "competition.round.created": "Round created",
    "competition.round.transitioned": "Round lifecycle transitioned",
    "competition.round.finalized": "Round published",
    "competition.round.corrected": "Round result corrected",
    "competition.result.published": "Official result published",
    "competition.result.corrected": "Official result corrected",
    LINEUP_SUBMITTED: "Lineup submitted",
    LINEUP_CORRECTED: "Locked lineup corrected",
    LINEUP_ADJUDICATED: "Missed submission adjudicated",
    DNP_CHANGED: "DNP ruling recorded",
    INTERCHANGE_CHANGED: "Interchange ruling recorded",
    OVERRIDE_CHANGED: "Score override recorded",
}


def _activity_row(event, *, team_by_lineup: dict, team_by_entry: dict, matchup_entries: dict) -> dict:
    label = _ACTIVITY_LABELS.get(event.action, event.action)
    team_name = None
    if event.entity_type == ENTITY_TYPE_LINEUP:
        team_name = team_by_lineup.get(event.entity_id)
    elif event.entity_type == "competition.matchup":
        home, away = matchup_entries.get(event.entity_id, (None, None))
        home_name = team_by_entry.get(home)
        away_name = team_by_entry.get(away)
        if home_name or away_name:
            team_name = f"{home_name or 'Unknown'} v {away_name or 'Unknown'}"
    elif ":" in event.entity_id:
        matchup_id, entry_id = event.entity_id.split(":", 2)[:2]
        team_name = team_by_entry.get(entry_id)
    return {
        "event_id": event.event_id,
        "action": event.action,
        "label": label,
        "team_name": team_name,
        "occurred_at": event.occurred_at,
        "actor_role": event.actor_role,
        "reason": event.reason,
        "diagnostics": {
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "correlation_id": event.correlation_id,
        },
    }


def build_scorer_dashboard(
    database,
    lifecycle,
    identities,
    seasons_repo,
    round_review_repo,
    audit_events,
    afl_client,
    season_id: str,
    *,
    round_id: str | None = None,
) -> dict:
    """The full Scorer Operations Dashboard read model for one season.

    Every value here is recomputed fresh from authoritative repositories on
    this call -- nothing is cached across requests (issue #153), and
    nothing here mutates anything: the trigger/lockout evaluation below
    calls the exact same `LockoutRepository` methods (and therefore the
    same durable materialisation) the lineup/lockout pages already use, so
    this view can never show a stale/empty activation merely because no
    other page happened to be opened first.
    """
    season = seasons_repo.get_season(season_id)
    if season is None:
        raise KeyError(season_id)

    rounds = ordinary_rounds_with_lifecycle(database, season_id)
    selected = select_current_round(rounds, round_id)
    round_options = [_round_option(row) for row in rounds]
    season_view = {"season_id": season.season_id, "year": season.year, "label": season.label}

    if selected is None:
        return {
            "season": season_view,
            "round_options": round_options,
            "round": None,
            "next_action": NextAction(
                "no_rounds_configured",
                CATEGORY_ADVISORY,
                "No ordinary rounds configured",
                "This season has no ordinary competition rounds yet.",
                None,
            ).__dict__,
            "attention": [],
            "lineups": [],
            "lockout": {"triggers": []},
            "review": None,
            "recent_activity": [],
        }

    return _build_round_dashboard(
        database,
        lifecycle,
        identities,
        seasons_repo,
        round_review_repo,
        audit_events,
        afl_client,
        season,
        selected,
        rounds,
        round_options,
    )


def _build_round_dashboard(
    database,
    lifecycle,
    identities,
    seasons_repo,
    round_review_repo,
    audit_events,
    afl_client,
    season,
    selected: dict,
    rounds: list[dict],
    round_options: list[dict],
) -> dict:
    round_id = selected["bbbffl_round_id"]
    competition_id = selected["competition_id"]
    lifecycle_state = selected["round_state"] or "not_created"

    preflight = None
    if lifecycle_state in ("not_created", "upcoming"):
        preflight = build_round_preflight(database, lifecycle, identities, afl_client, round_id)

    entry_ids = _entry_ids_for_round(database, identities, season.season_id, competition_id, round_id)
    entry_name_map = {entry.season_entry_id: entry.team_name for entry in identities.list_entries(season.season_id)}

    lineups_repo = WeeklyLineupRepository(database)
    lockouts_repo = LockoutRepository(database)
    # Cached for the lifetime of this one build (Codex review, PR #159): a
    # ten-team round would otherwise ask `match_facts` for this round's
    # matches roughly once per team (trigger materialisation plus each
    # team's own position-lock read), each a live AFL request -- one real
    # fetch is all this read ever needs.
    match_facts = _CachedMatchFacts(RoundMatchFactsProvider(RoundMappingRepository(database), afl_client))

    trigger_rows: list[dict] = []
    matches: list = []
    evidence_fresh = True
    evidence_batch = getattr(afl_client, "evidence_batch", None)
    scope = evidence_batch() if callable(evidence_batch) else nullcontext(afl_client)
    lockout_evidence_error = None
    team_rows: list[dict] = []
    # A round's *configured* trigger plan is a pure persisted fact, entirely
    # independent of whether live AFL evidence can be fetched right now --
    # read it unconditionally so a provider outage below is never confused
    # with "nothing was ever configured" (Codex review, PR #159).
    trigger_plan_configured = False
    # A `final` round is already published, read-only governance/operational
    # history -- its lockout/lineup evidence was durably materialized while
    # it was live and never changes afterwards (a correction is a separate,
    # explicitly-invoked mutation elsewhere, never something a dashboard
    # read triggers). It therefore needs no live AFL match/lockout evidence
    # to render (issue #176: at a replay evidence-package boundary, a
    # finalized historical round's mapped AFL round can legitimately sit
    # outside the currently active package -- requiring it here would fail
    # closed for evidence this read never actually needs), but its
    # persisted trigger/lock facts are still shown, via `LockoutRepository`'s
    # persisted-only projection, exactly like a round still being played.
    if lifecycle_state == "final":
        # Never asserted as confirmed fresh (Codex review, PR #177): no live
        # evidence was fetched to confirm anything here, and `evidence_fresh
        # =True` would misrepresent a check that was deliberately skipped as
        # one that was performed and passed. `build_round_review` treats
        # `None` distinctly from both `True` and `False` for exactly this.
        evidence_fresh = None
        trigger_plan_configured = bool(LockoutTriggerRepository(database).list_triggers(round_id))
        trigger_rows = _build_trigger_rows(lockouts_repo.persisted_trigger_state(round_id), {})
        any_trigger_activated = any(row["activated"] for row in trigger_rows)
        team_rows = _build_team_readiness(
            lineups_repo,
            lockouts_repo,
            identities,
            match_facts,
            season_id=season.season_id,
            competition_id=competition_id,
            round_id=round_id,
            entry_ids=entry_ids,
            lifecycle_state=lifecycle_state,
            any_trigger_activated=any_trigger_activated,
        )
    elif lifecycle_state != "not_created":
        trigger_plan_configured = bool(LockoutTriggerRepository(database).list_triggers(round_id))
        with scope as evidence:
            try:
                matches = match_facts.matches_for(round_id)
            except (AflApiError, MatchResolutionError):
                matches = []
            match_by_id = {match.match_id: match for match in matches}
            try:
                trigger_views = lockouts_repo.describe_triggers(round_id, match_facts=match_facts)
                trigger_rows = _build_trigger_rows(trigger_views, match_by_id)
            except (AflApiError, MatchResolutionError) as exc:
                trigger_rows = []
                lockout_evidence_error = str(exc)
            any_trigger_activated = any(row["activated"] for row in trigger_rows)
            team_rows = _build_team_readiness(
                lineups_repo,
                lockouts_repo,
                identities,
                match_facts,
                season_id=season.season_id,
                competition_id=competition_id,
                round_id=round_id,
                entry_ids=entry_ids,
                lifecycle_state=lifecycle_state,
                any_trigger_activated=any_trigger_activated,
            )
            is_fresh = getattr(evidence, "is_evidence_fresh", None)
            evidence_fresh = is_fresh() if callable(is_fresh) else True
    else:
        team_rows = _build_team_readiness(
            lineups_repo,
            lockouts_repo,
            identities,
            match_facts,
            season_id=season.season_id,
            competition_id=competition_id,
            round_id=round_id,
            entry_ids=entry_ids,
            lifecycle_state=lifecycle_state,
            any_trigger_activated=False,
        )

    all_matches_finished = bool(matches) and all(match.state in ("postgame", "completed") for match in matches)

    matchups = lifecycle.list_matchups(round_id) if lifecycle_state != "not_created" else []
    round_review_view = None
    if lifecycle_state in ("live", "review", "final") and matchups:
        review = build_round_review(
            lifecycle,
            round_review_repo,
            identities,
            round_id,
            evidence_fresh=evidence_fresh,
            season_repo=seasons_repo,
        )
        round_review_view = _round_review_dict(review)
        staleness_by_entry = {}
        for matchup in matchups:
            for entry_id in (matchup.home_season_entry_id, matchup.away_season_entry_id):
                staleness = calculation_staleness_for_entry(lifecycle, round_review_repo, matchup, entry_id)
                staleness_by_entry[entry_id] = staleness["stale"]
        for team in team_rows:
            team["calculation_stale"] = staleness_by_entry.get(team["season_entry_id"], False)
    else:
        for team in team_rows:
            team["calculation_stale"] = False

    attention: list[dict] = []
    if preflight is not None:
        attention += _preflight_attention(preflight, round_id)
    if round_review_view is not None:
        attention += _review_attention(round_review_view, round_id)
    attention += _team_attention(team_rows, round_id, season.season_id)
    attention += _trigger_attention(
        lifecycle_state,
        trigger_rows,
        round_id,
        trigger_plan_configured=trigger_plan_configured,
        evidence_unavailable=lockout_evidence_error is not None,
    )
    if lockout_evidence_error:
        attention.append(
            {
                "category": CATEGORY_WAITING,
                "code": "lockout:evidence_unavailable",
                "title": "Lockout evidence unavailable",
                "detail": lockout_evidence_error,
                "state": lifecycle_state,
                "timestamp": None,
                "capability": None,
                "url": ROUND_CENTRE_URL.format(round_id=round_id),
                "diagnostics": None,
            }
        )
    attention.sort(key=_sort_key)

    next_round = next(
        (row for row in rounds if row["sequence"] > selected["sequence"]),
        None,
    )
    next_action = _determine_next_action(
        lifecycle_state=lifecycle_state,
        preflight=preflight,
        trigger_rows=trigger_rows,
        team_rows=team_rows,
        round_review=round_review_view,
        round_id=round_id,
        season_id=season.season_id,
        next_round_id=next_round["bbbffl_round_id"] if next_round else None,
        all_matches_finished=all_matches_finished,
        trigger_plan_configured=trigger_plan_configured,
        evidence_unavailable=lockout_evidence_error is not None,
    )

    matchup_ids = [m.matchup_id for m in matchups]
    lineup_ids = [row["lineup_id"] for row in team_rows if row["lineup_id"]]
    recent_events = _recent_activity(audit_events, round_id, matchup_ids, lineup_ids)
    team_by_lineup = {
        row["lineup_id"]: row["team_name"] or f"Unknown team ({row['season_entry_id']})"
        for row in team_rows
        if row["lineup_id"]
    }
    matchup_entries = {m.matchup_id: (m.home_season_entry_id, m.away_season_entry_id) for m in matchups}
    recent_activity = [
        _activity_row(
            event, team_by_lineup=team_by_lineup, team_by_entry=entry_name_map, matchup_entries=matchup_entries
        )
        for event in recent_events
    ]

    round_view = {
        "bbbffl_round_id": round_id,
        "round_label": selected["round_label"],
        "sequence": selected["sequence"],
        "season_id": season.season_id,
        "competition_id": competition_id,
        "state": lifecycle_state,
        "afl_season_id": selected["afl_season_id"],
        "afl_round_id": selected["afl_round_id"],
        "mapping_revision": selected["mapping_revision"],
        "round_centre_url": ROUND_CENTRE_URL.format(round_id=round_id),
        "preflight_url": PREFLIGHT_URL.format(round_id=round_id),
        "public_round_centre_url": PUBLIC_ROUND_CENTRE_URL.format(season_id=season.season_id, round_id=round_id),
    }

    return {
        "season": {"season_id": season.season_id, "year": season.year, "label": season.label},
        "round_options": round_options,
        "round": round_view,
        "preflight": preflight,
        "next_action": next_action,
        "attention": attention,
        "lineups": team_rows,
        "lockout": {"triggers": trigger_rows},
        "review": round_review_view,
        "recent_activity": recent_activity,
    }


def _round_review_dict(review) -> dict:
    return dataclasses.asdict(review)
