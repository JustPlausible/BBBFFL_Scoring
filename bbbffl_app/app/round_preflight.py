"""Human-facing, fail-closed read model for opening an ordinary BBBFFL round.

Issue #152 extends this from a raw-ID entry form into an evidence-backed
recommendation surface: `build_round_preflight` still returns exactly the
authoritative, persisted mapping/trigger/readiness state it always has, but
now also surfaces purely advisory, deterministic suggestions (a recommended
AFL season/round mapping, a recommended selective/main lockout plan,
advisory replay-checkpoint instants) computed fresh on every read and never
stored. Nothing here ever writes a recommendation; `accept_preflight_mapping`
and `configure_preflight_trigger` remain the only mutation boundaries, and
both still require an explicit operator decision (a reason, and for mapping
acceptance an explicit confirmation) plus revision-matched concurrency
protection -- an operator may always deliberately diverge from a
recommendation, since BBBFFL and AFL round numbering can legitimately
differ (see docs/round-afl-mapping.md)."""

from contextlib import nullcontext

from app.afl_client import is_recognized_match_status, normalize_match_status
from app.lockouts import LockoutTriggerRepository, StaleTriggerRevisionError, TriggerValidationError
from app.opening_round import (
    OpeningRoundNominationRepository,
    OpeningRoundRuleRepository,
    build_opening_round_readiness,
    describe_accepted_rules,
)
from app.round_mapping import (
    AflApiReferenceValidator,
    RoundMappingRepository,
    StaleMappingRevisionError,
    recommend_mapping,
)

# Re-exported for app/routes/round_preflight.py, which must not import
# app.lockouts/app.round_mapping directly (see test_architecture.py's
# route/persistence-boundary check) -- this service-layer module is its
# only permitted source for these.
__all__ = [
    "StaleMappingRevisionError",
    "StaleTriggerRevisionError",
    "TriggerValidationError",
    "accept_preflight_mapping",
    "build_round_preflight",
    "configure_preflight_trigger",
    "open_preflight_round",
    "recommend_lockout_plan",
]


def accept_preflight_mapping(
    database,
    lifecycle,
    afl_client,
    round_id,
    season_id,
    afl_round_id,
    *,
    actor,
    reason,
    confirmed=False,
    expected_revision=None,
):
    """Mutate mapping only before lifecycle has frozen its revision.

    `confirmed`/`reason` are both mandatory here regardless of whether the
    chosen `(season_id, afl_round_id)` matches this round's own deterministic
    recommendation (`app.round_mapping.recommend_mapping`) -- nothing is
    ever auto-accepted; the operator must explicitly confirm and explain
    every accepted mapping, including one that deliberately diverges from
    the recommendation. `expected_revision`, when supplied, must match the
    mapping's current revision (0 meaning "no accepted mapping yet") or this
    raises `StaleMappingRevisionError` rather than silently overwriting a
    revision a concurrent operator has since accepted/corrected.
    """
    if lifecycle.get_round(round_id) is not None:
        raise RuntimeError(
            "The round lifecycle has already frozen its AFL mapping. A lifecycle-level recovery is required; "
            "the mapping cannot be changed underneath it."
        )
    if not confirmed:
        raise ValueError("Accepting an AFL round mapping requires explicit operator confirmation.")
    if not reason:
        raise ValueError("Accepting an AFL round mapping requires an explicit reason.")
    repo = RoundMappingRepository(database)
    validator = AflApiReferenceValidator(afl_client)
    # This `resolve()` only decides *which* repository method to call --
    # never the revision comparison itself, which `accept`/`correct` (via
    # `_activate`) perform atomically under the same row lock that advances
    # `current_revision` (issue #152 review, P2: a check performed here,
    # before that lock is taken, cannot close the race where two concurrent
    # callers both observe the same stale revision and both pass a
    # standalone comparison before either commits). A stale/wrong choice of
    # accept-vs-correct is itself still safe: `_activate` independently
    # rejects a mismatched correction/acceptance state regardless of what
    # this read observed.
    existing = repo.resolve(round_id)
    if existing:
        return repo.correct(
            round_id,
            season_id,
            afl_round_id,
            validator,
            reason=reason,
            actor=actor,
            expected_revision=expected_revision,
        )
    return repo.accept(
        round_id, season_id, afl_round_id, validator, reason=reason, actor=actor, expected_revision=expected_revision
    )


def configure_preflight_trigger(database, round_id, payload, afl_client, *, actor, reason):
    """Configure one lockout-trigger slot from *this round's currently
    accepted mapped matches only* -- issue #152 replaces routine free-typed
    AFL match IDs with selection from that authoritative list, enforced
    here server-side (never trusted from the browser alone):

    - an accepted mapping must exist;
    - every submitted AFL match ID must belong to that mapping's current
      match evidence (this needs an `afl_client` call, so it happens here,
      deliberately outside any lock);
    - sequence uniqueness/ordering and `payload.expected_revision` are then
      validated -- and the resulting write performed -- atomically, under
      one lock, by `LockoutTriggerRepository.configure` (issue #152 review,
      P2: validating those against a snapshot read here, before any lock is
      held, cannot close the race where two concurrent configuration
      attempts for this round both pass validation before either commits).
    - the accepted mapping's own revision, observed here alongside its
      matches, is carried through as `expected_mapping_revision` so
      `configure` can atomically confirm, under its own lock, that the
      mapping has not been concurrently corrected between this membership
      check and that write -- otherwise a trigger could be persisted
      referencing a since-superseded mapping's matches, immediately
      unresolved against the round's *new* current mapping (issue #152
      review, second pass, P2).
    """
    mapping_repo = RoundMappingRepository(database)
    mapping = mapping_repo.resolve(round_id)
    if mapping is None:
        raise TriggerValidationError(
            "An accepted AFL mapping is required before configuring lockout triggers for this round."
        )
    try:
        mapped_matches = afl_client.get_matches(mapping.afl_round_id)
    except Exception as exc:  # evidence failure must block trigger membership validation, never bypass it
        raise TriggerValidationError(f"Mapped AFL match evidence is unavailable: {exc}") from exc
    valid_match_ids = {match.match_id for match in mapped_matches}
    unknown_ids = [match_id for match_id in payload.afl_match_ids if match_id not in valid_match_ids]
    if unknown_ids:
        raise TriggerValidationError(
            f"AFL match(es) {unknown_ids} are not part of the currently accepted mapping's matches."
        )

    return LockoutTriggerRepository(database).configure(
        round_id,
        payload.trigger_key,
        payload.trigger_type,
        payload.sequence,
        payload.afl_match_ids,
        actor=actor,
        reason=reason,
        expected_revision=payload.expected_revision,
        expected_mapping_revision=mapping.revision,
    )


def open_preflight_round(lifecycle, round_id, *, actor):
    if lifecycle.get_round(round_id) is None:
        lifecycle.create_ordinary_round(
            round_id, actor=actor, reason="Round context frozen after successful operator preflight"
        )
    return lifecycle.transition(
        round_id, "open", actor=actor, reason="Explicit Open Round action after successful preflight"
    )


def recommend_lockout_plan(match_views: list[dict]) -> dict | None:
    """A deterministic, purely advisory "earliest plausible" selective/main
    lockout plan (issue #152) computed from this round's currently mapped
    matches -- never persisted by this function or any caller; an operator
    must still explicitly configure each stage via `configure_preflight_trigger`.

    Fails closed (`None`) whenever evidence is incomplete or ambiguous: any
    match missing a scheduled start time, or carrying an unrecognised
    status, makes the whole plan unsafe to suggest. Groups matches by their
    earliest shared scheduled start: if every match shares one start time, a
    single main trigger covering all of them is recommended; otherwise the
    earliest-starting group becomes a selective trigger (sequence 1) and
    every other match becomes the main trigger (sequence 2).
    """
    if not match_views:
        return None
    if any(match["start_time_utc"] is None or not is_recognized_match_status(match["status"]) for match in match_views):
        return None
    ordered = sorted(match_views, key=lambda match: (match["start_time_utc"], match["match_id"]))
    earliest_start = ordered[0]["start_time_utc"]
    earliest_ids = [match["match_id"] for match in ordered if match["start_time_utc"] == earliest_start]
    remaining_ids = [match["match_id"] for match in ordered if match["match_id"] not in earliest_ids]
    if not remaining_ids:
        return {
            "stages": [
                {
                    "trigger_key": "recommended-main",
                    "trigger_type": "main",
                    "sequence": 1,
                    "afl_match_ids": [match["match_id"] for match in ordered],
                    "evidence": (
                        f"Every mapped AFL match is scheduled to start at {earliest_start}; a single "
                        "main/remaining lockout covering all of them is the earliest plausible plan."
                    ),
                }
            ]
        }
    return {
        "stages": [
            {
                "trigger_key": "recommended-early",
                "trigger_type": "selective",
                "sequence": 1,
                "afl_match_ids": earliest_ids,
                "evidence": f"The earliest scheduled AFL match(es) in the mapped round start at {earliest_start}.",
            },
            {
                "trigger_key": "recommended-main",
                "trigger_type": "main",
                "sequence": 2,
                "afl_match_ids": remaining_ids,
                "evidence": "Covers every other AFL match in the mapped round.",
            },
        ]
    }


def _replay_checkpoint_recommendations(afl_client, match_views: list[dict], triggers: list) -> list[dict]:
    """Advisory-only replay checkpoint instants (issue #152) -- never a
    filesystem path (see app/replay_checkpoint.py's schema: a checkpoint is
    identified only by `stage`/`effective_at`/`finalised_round_ids`), and
    never applied/written here: this is a read model suggestion for an
    operator to action, if they choose, through the existing replay
    checkpoint tooling. Only produced when the configured client carries
    replay metadata at all (a `clock` attribute -- absent on the live
    `AflApiClient`).

    A "just after this trigger" instant (`stage="scheduled"`) is safe to
    derive from scheduled start times alone, since a trigger's own lock
    boundary is itself schedule-based (`evaluate_match_lock`). A
    "final-results" instant is not: recommending the latest match's
    scheduled *start* would suggest finalising the round the moment its
    last match begins, before it has actually concluded (issue #152
    review, P1). Lacking any actual conclusion-time evidence in `Match`,
    the only safe evidence-backed final-results recommendation is "right
    now", and only once every relevant match's own currently observed
    status already reads as concluded (postgame/completed) -- otherwise no
    final-results recommendation is made at all, rather than guessing one.
    """
    if not hasattr(afl_client, "clock"):
        return []
    recommendations = []
    matches_by_id = {match["match_id"]: match for match in match_views}
    for trigger in triggers:
        covered = [
            matches_by_id[match_id]
            for match_id in trigger.afl_match_ids
            if match_id in matches_by_id and matches_by_id[match_id]["start_time_utc"] is not None
        ]
        if not covered:
            continue
        earliest = min(match["start_time_utc"] for match in covered)
        recommendations.append(
            {
                "label": f"Just after {trigger.trigger_key} ({trigger.trigger_type})",
                "stage": "scheduled",
                "recommended_effective_at": earliest,
                "evidence": (
                    f"The earliest AFL match associated with trigger {trigger.trigger_key!r} is scheduled to "
                    f"start at {earliest}; a replay checkpoint recorded just after this instant is expected to "
                    "demonstrate this trigger's activation."
                ),
            }
        )
    if match_views and all(
        is_recognized_match_status(match["status"])
        and normalize_match_status(match["status"]) in ("postgame", "completed")
        for match in match_views
    ):
        clock = getattr(afl_client, "clock", None)
        now = clock.now() if clock is not None else None
        if now is not None:
            recommendations.append(
                {
                    "label": "Safe final-results checkpoint",
                    "stage": "final-results",
                    "recommended_effective_at": now.isoformat(),
                    "evidence": (
                        "Every relevant AFL match currently shows a concluded status (postgame/completed), so "
                        "recording a final-results checkpoint now is expected to be safe."
                    ),
                }
            )
    return recommendations


def build_round_preflight(database, lifecycle, identities, afl_client, round_id: str) -> dict:
    logical = database.execute(
        "SELECT r.*, c.season_id, c.label competition_label, c.stream_key, c.stream_type, "
        "s.year, s.label season_label FROM bbbffl_round r "
        "JOIN competition_stream c ON c.competition_id=r.competition_id "
        "JOIN bbbffl_season s ON s.season_id=c.season_id WHERE r.bbbffl_round_id=?",
        (round_id,),
    ).fetchone()
    if not logical:
        raise KeyError(round_id)

    blockers, advisories = [], []
    mapping_repo = RoundMappingRepository(database)
    mapping = mapping_repo.resolve(round_id)
    history = mapping_repo.history(round_id)
    if mapping is None:
        current = history[-1] if history else None
        blockers.append(
            {
                "code": "mapping_missing" if current is None else "mapping_unresolved",
                "message": (
                    "No authoritative AFL mapping has been accepted."
                    if current is None
                    else f"The current mapping decision is {current.state}; explicitly accept one AFL season and round."
                ),
            }
        )

    # -- Issue #152: human-readable AFL season/round selection evidence,
    # advisory only -- never gates readiness, never itself resolved/accepted.
    afl_seasons = []
    list_seasons = getattr(afl_client, "get_seasons", None)
    if callable(list_seasons):
        try:
            afl_seasons = [
                {"season_id": s.season_id, "year": s.year, "name": s.name, "is_current": s.is_current}
                for s in list_seasons()
            ]
        except Exception as exc:
            advisories.append(
                {"code": "afl_seasons_unavailable", "message": f"AFL season list is unavailable for selection: {exc}"}
            )
    mapping_recommendation = recommend_mapping(
        afl_client,
        bbbffl_year=logical["year"],
        bbbffl_sequence=logical["sequence"],
        bbbffl_stream_type=logical["stream_type"],
    )
    mapping_context = None
    if mapping is not None:
        try:
            season_year = next((s["year"] for s in afl_seasons if s["season_id"] == mapping.afl_season_id), None)
            round_number = next(
                (
                    r.round_number
                    for r in afl_client.get_rounds(mapping.afl_season_id)
                    if r.round_id == mapping.afl_round_id
                ),
                None,
            )
            mapping_context = {"afl_season_year": season_year, "afl_round_number": round_number}
        except Exception:
            mapping_context = None

    draw = database.execute("SELECT * FROM season_fixture_draw WHERE season_id=?", (logical["season_id"],)).fetchone()
    pairs = []
    if draw and draw["state"] == "frozen":
        rows = database.execute(
            "SELECT * FROM season_fixture_matchup "
            "WHERE fixture_draw_id=? AND bbbffl_round_number=? ORDER BY matchup_order",
            (draw["fixture_draw_id"], logical["sequence"]),
        ).fetchall()
        names = {entry.season_entry_id: entry.team_name for entry in identities.list_entries(logical["season_id"])}
        pairs = [
            {
                **dict(row),
                "home_team_name": names.get(row["home_season_entry_id"], "Unknown team"),
                "away_team_name": names.get(row["away_season_entry_id"], "Unknown team"),
            }
            for row in rows
        ]
    if len(pairs) != 5:
        blockers.append(
            {"code": "fixture_invalid", "message": "A frozen fixture with exactly five matchups is required."}
        )

    afl_matches, evidence_error = [], None
    evidence_fresh = True
    if mapping:
        evidence_batch = getattr(afl_client, "evidence_batch", None)
        scope = evidence_batch() if callable(evidence_batch) else nullcontext(afl_client)
        with scope as evidence:
            try:
                afl_matches = afl_client.get_matches(mapping.afl_round_id)
            except Exception as exc:  # evidence failure is operationally meaningful
                evidence_error = str(exc)
                blockers.append(
                    {
                        "code": "afl_evidence_unavailable",
                        "message": f"Mapped AFL match evidence is unavailable: {exc}",
                    }
                )
            freshness = getattr(evidence, "is_evidence_fresh", None)
            evidence_fresh = freshness() if callable(freshness) else True
        if not evidence_fresh:
            blockers.append(
                {
                    "code": "afl_evidence_stale",
                    "message": "AFL match evidence is being served from a stale cache; refresh live evidence before opening.",
                }
            )
    if mapping and not afl_matches and evidence_error is None:
        blockers.append(
            {"code": "afl_matches_missing", "message": "The mapped AFL round contains no resolvable match evidence."}
        )
    # Issue #152: matches are always presented in scheduled-start
    # chronological order (unresolved/missing start times sort last, never
    # first, so a genuinely unscheduled match never masquerades as "next").
    afl_matches = sorted(afl_matches, key=lambda m: (m.start_time_utc is None, m.start_time_utc or "", m.match_id))
    match_by_id = {match.match_id: match for match in afl_matches}
    match_views = [
        {
            "match_id": m.match_id,
            "home_team": m.home_team.name,
            "away_team": m.away_team.name,
            "start_time_utc": m.start_time_utc,
            "status": m.status,
            "lifecycle": m.state,
        }
        for m in afl_matches
    ]
    for match in afl_matches:
        if not match.start_time_utc:
            blockers.append(
                {
                    "code": "match_schedule_missing",
                    "message": f"AFL match {match.match_id} has no scheduled start evidence.",
                }
            )
        if not is_recognized_match_status(match.status):
            blockers.append(
                {
                    "code": "match_status_unknown",
                    "message": f"AFL match {match.match_id} has an unrecognised lifecycle status ({match.status!r}).",
                }
            )

    triggers = LockoutTriggerRepository(database).list_triggers(round_id)
    # Issue #152: `observed_status`/`start_time_utc` on `activating_matches`
    # above is always this match's *current* AFL evidence; the durable
    # activation row queried here is BBBFFL's own, separate, irreversible
    # record of whether *this trigger* has actually fired -- read-only, never
    # materialized/recomputed from this view (see app/lockouts.py's
    # "Historical irreversibility"), so simply viewing preflight can never
    # itself cause or backdate an activation.
    activation_by_trigger_id = {
        row["trigger_id"]: dict(row)
        for row in database.execute(
            "SELECT a.trigger_id, a.afl_match_id, a.observed_status, a.effective_lock_at, a.activation_reason "
            "FROM bbbffl_round_lockout_trigger_activation a "
            "JOIN bbbffl_round_lockout_trigger t ON t.trigger_id=a.trigger_id "
            "WHERE t.bbbffl_round_id=?",
            (round_id,),
        ).fetchall()
    }
    match_trigger_coverage: dict[int, list[dict]] = {}
    trigger_views = []
    for trigger in triggers:
        unresolved = [mid for mid in trigger.afl_match_ids if mid not in match_by_id]
        if unresolved:
            blockers.append(
                {
                    "code": "lockout_match_unresolved",
                    "message": f"Lockout trigger {trigger.trigger_key} refers to AFL match(es) not in the mapped round: {unresolved}.",
                }
            )
        activation = activation_by_trigger_id.get(trigger.trigger_id)
        activation_view = {
            "activated": activation is not None,
            "activation_reason": activation["activation_reason"] if activation else None,
            "effective_lock_at": activation["effective_lock_at"] if activation else None,
            "observed_status_at_activation": activation["observed_status"] if activation else None,
        }
        participating_clubs = sorted(
            {
                name
                for mid in trigger.afl_match_ids
                if mid in match_by_id
                for name in (match_by_id[mid].home_team.name, match_by_id[mid].away_team.name)
            }
        )
        for mid in trigger.afl_match_ids:
            match_trigger_coverage.setdefault(mid, []).append(
                {"trigger_key": trigger.trigger_key, "trigger_type": trigger.trigger_type, **activation_view}
            )
        trigger_views.append(
            {
                **trigger.__dict__,
                "scope": (
                    "Players involved in the activating AFL match(es)"
                    if trigger.trigger_type == "selective"
                    else "All remaining selections"
                ),
                "participating_clubs": participating_clubs,
                "activating_matches": [
                    next((v for v in match_views if v["match_id"] == mid), {"match_id": mid, "unresolved": True})
                    for mid in trigger.afl_match_ids
                ],
                # Never conflate this durable BBBFFL activation fact with the
                # AFL match evidence shown in `activating_matches` above.
                "activation": activation_view,
            }
        )
    for view in match_views:
        view["lockout_trigger_coverage"] = match_trigger_coverage.get(view["match_id"], [])
    mains = [t for t in triggers if t.trigger_type == "main"]
    if len(mains) != 1:
        blockers.append(
            {
                "code": "main_lockout_incomplete",
                "message": "Exactly one main/remaining lockout trigger must be configured.",
            }
        )
    if not any(t.trigger_type == "selective" for t in triggers):
        advisories.append(
            {
                "code": "no_selective_lockout",
                "message": "No selective early lockout is configured; the main trigger will lock all remaining selections.",
            }
        )

    lockout_recommendation = recommend_lockout_plan(match_views) if mapping and match_views else None
    replay_checkpoint_recommendations = _replay_checkpoint_recommendations(afl_client, match_views, triggers)

    # Issue #133: where an ordinary round depends on Opening Round deferred
    # selections (an accepted rule targets this round), the round remains
    # blocked until *every* required season entry has explicitly confirmed
    # its Opening Round submission -- never inferred/created here, only
    # reported, with a direct navigation path back to Opening Round
    # Operations. This is deliberately season-wide, not scoped to entries
    # eligible for *this* round's rules specifically: the historical
    # submission boundary is per season entry, not per club/rule (issue
    # #133's "a coach may nominate zero or more eligible owned players").
    # Integrity conflicts (duplicate/mismatched/conflicting nominations) are
    # reported as a separate blocker so a confirmed-but-corrupted nomination
    # can never silently pass this gate.
    rule_repo = OpeningRoundRuleRepository(database)
    season_accepted_rules = rule_repo.list_accepted_for_season(logical["season_id"])
    round_rules = [rule for rule in season_accepted_rules if rule.bbbffl_round_id == round_id]
    if season_accepted_rules:
        opening_round_readiness = build_opening_round_readiness(database, logical["season_id"])
        if round_rules:
            unconfirmed_entries = [
                {"season_entry_id": entry.season_entry_id, "team_name": entry.team_name}
                for entry in opening_round_readiness.entries
                if not entry.is_confirmed
            ]
            if unconfirmed_entries:
                blockers.append(
                    {
                        "code": "opening_round_nominations_incomplete",
                        "message": (
                            f"This round depends on Opening Round deferred selections; "
                            f"{len(unconfirmed_entries)} entry/entries have not yet confirmed their Opening Round "
                            "submission. Confirm each entry's submission in Opening Round Operations before "
                            "opening this round."
                        ),
                        "url": f"/operations/seasons/{logical['season_id']}/opening-round",
                        "entries": unconfirmed_entries,
                    }
                )
        # Matched against *both* the rule's current target (`target_rule_ids`)
        # and each diagnostic's own persisted round (`bbbffl_round_id`/
        # `bbbffl_round_ids`) -- a `conflicting_nominations` entry is, by
        # definition, one whose nomination has drifted to a round no rule
        # currently targets, so `round_rules` alone would be empty for
        # exactly the round that nomination is still active in (still
        # returned by `list_for_round` below, still selectable/scorable).
        # Matching by rule ID alone would let that round open with corrupted
        # deferred-selection data (PR #134 review, P1).
        target_rule_ids = {rule.rule_id for rule in round_rules}

        def _affects_this_round(item: dict) -> bool:
            if item.get("rule_id") in target_rule_ids:
                return True
            persisted_rounds = item.get("bbbffl_round_ids")
            if persisted_rounds is not None:
                return round_id in persisted_rounds
            return item.get("bbbffl_round_id") == round_id

        integrity_issues = [
            item
            for item in (
                *opening_round_readiness.duplicate_nominations,
                *opening_round_readiness.mismatched_nominations,
                *opening_round_readiness.conflicting_nominations,
            )
            if _affects_this_round(item)
        ]
        if integrity_issues:
            blockers.append(
                {
                    "code": "opening_round_integrity_conflict",
                    "message": (
                        f"{len(integrity_issues)} Opening Round nomination integrity issue(s) affect this round's "
                        "deferred selections; resolve them in Opening Round Operations before opening this round."
                    ),
                    "url": f"/operations/seasons/{logical['season_id']}/opening-round",
                }
            )

    nominations = OpeningRoundNominationRepository(database).list_for_round(round_id)
    opening = []
    entry_names = {entry.season_entry_id: entry.team_name for entry in identities.list_entries(logical["season_id"])}
    described_rules = (
        {rule["rule_id"]: rule for rule in describe_accepted_rules(database, afl_client, logical["season_id"])}
        if nominations
        else {}
    )
    for nomination in nominations:
        context = OpeningRoundNominationRepository(database).deferred_context(
            round_id, nomination.season_entry_id, nomination.position
        )
        player_row = database.execute(
            "SELECT display_name, afl_team_name FROM season_player_pool WHERE season_player_id=?",
            (nomination.season_player_id,),
        ).fetchone()
        rule_view = described_rules.get(nomination.rule_id)
        opening.append(
            {
                **nomination.__dict__,
                "team_name": entry_names.get(nomination.season_entry_id, "Unknown team"),
                "player_display_name": player_row["display_name"] if player_row else None,
                "afl_club_name": player_row["afl_team_name"] if player_row else None,
                "rule_display_label": rule_view["display_label"] if rule_view else None,
                "afl_opening_round_label": rule_view["afl_opening_round_label"] if rule_view else None,
                "afl_bye_round_label": rule_view["afl_bye_round_label"] if rule_view else None,
                "bbbffl_round_label": rule_view["bbbffl_round_label"] if rule_view else None,
                **(context or {}),
            }
        )

    persisted = lifecycle.get_round(round_id)
    state = persisted.state if persisted else "not_created"
    if persisted and persisted.state != "upcoming":
        advisories.append(
            {
                "code": "already_opened",
                "message": f"This round is already {persisted.state}; its persisted lifecycle is authoritative.",
            }
        )
    return {
        "round": {**dict(logical), "lifecycle_state": state},
        "mapping": mapping.__dict__ if mapping else None,
        "mapping_context": mapping_context,
        "mapping_history": [item.__dict__ for item in history],
        "mapping_recommendation": mapping_recommendation.__dict__ if mapping_recommendation else None,
        "afl_seasons": afl_seasons,
        "fixture_matchups": pairs,
        "afl_matches": match_views,
        "afl_evidence_fresh": evidence_fresh,
        "lockout_triggers": trigger_views,
        "lockout_recommendation": lockout_recommendation,
        "replay_checkpoint_recommendations": replay_checkpoint_recommendations,
        "opening_round": {"applies": bool(opening), "deferred_selections": opening},
        "readiness": {
            "safe_to_open": not blockers and state in {"not_created", "upcoming"},
            "blockers": blockers,
            "advisories": advisories,
        },
    }
