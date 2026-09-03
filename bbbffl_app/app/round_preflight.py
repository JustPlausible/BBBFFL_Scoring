"""Human-facing, fail-closed read model for opening an ordinary BBBFFL round."""

from contextlib import nullcontext

from app.afl_client import is_recognized_match_status
from app.lockouts import LockoutTriggerRepository
from app.opening_round import (
    OpeningRoundNominationRepository,
    OpeningRoundRuleRepository,
    build_opening_round_readiness,
    describe_accepted_rules,
)
from app.round_mapping import AflApiReferenceValidator, RoundMappingRepository


def accept_preflight_mapping(database, lifecycle, afl_client, round_id, season_id, afl_round_id, *, actor, reason):
    """Mutate mapping only before lifecycle has frozen its revision."""
    if lifecycle.get_round(round_id) is not None:
        raise RuntimeError(
            "The round lifecycle has already frozen its AFL mapping. A lifecycle-level recovery is required; "
            "the mapping cannot be changed underneath it."
        )
    repo = RoundMappingRepository(database)
    validator = AflApiReferenceValidator(afl_client)
    existing = repo.resolve(round_id)
    if existing:
        return repo.correct(round_id, season_id, afl_round_id, validator, reason=reason, actor=actor)
    return repo.accept(round_id, season_id, afl_round_id, validator, reason=reason, actor=actor)


def configure_preflight_trigger(database, round_id, payload, *, actor, reason):
    repo = LockoutTriggerRepository(database)
    existing = repo.get(round_id, payload.trigger_key)
    kwargs = {
        "trigger_type": payload.trigger_type,
        "sequence": payload.sequence,
        "afl_match_ids": payload.afl_match_ids,
        "actor": actor,
        "reason": reason,
    }
    if existing:
        return repo.replace(round_id, payload.trigger_key, **kwargs)
    return repo.create(
        round_id,
        payload.trigger_key,
        payload.trigger_type,
        payload.sequence,
        payload.afl_match_ids,
        actor=actor,
        reason=reason,
    )


def open_preflight_round(lifecycle, round_id, *, actor):
    if lifecycle.get_round(round_id) is None:
        lifecycle.create_ordinary_round(
            round_id, actor=actor, reason="Round context frozen after successful operator preflight"
        )
    return lifecycle.transition(
        round_id, "open", actor=actor, reason="Explicit Open Round action after successful preflight"
    )


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
        trigger_views.append(
            {
                **trigger.__dict__,
                "scope": (
                    "Players involved in the activating AFL match(es)"
                    if trigger.trigger_type == "selective"
                    else "All remaining selections"
                ),
                "activating_matches": [
                    next((v for v in match_views if v["match_id"] == mid), {"match_id": mid, "unresolved": True})
                    for mid in trigger.afl_match_ids
                ],
            }
        )
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

    # Issue #131: where an ordinary round depends on Opening Round deferred
    # selections (an accepted rule targets this round), incomplete
    # nominations are a readiness blocker, not a silent gap -- and never
    # inferred/created here, only reported, with a direct navigation path
    # back to the existing Opening Round Operations workflow.
    rule_repo = OpeningRoundRuleRepository(database)
    round_rules = [
        rule for rule in rule_repo.list_accepted_for_season(logical["season_id"]) if rule.bbbffl_round_id == round_id
    ]
    if round_rules:
        opening_round_readiness = build_opening_round_readiness(database, logical["season_id"])
        target_rule_ids = {rule.rule_id for rule in round_rules}
        incomplete_entries = [
            {
                "season_entry_id": entry.season_entry_id,
                "team_name": entry.team_name,
                "missing_count": len(set(entry.missing_rule_ids) & target_rule_ids),
            }
            for entry in opening_round_readiness.entries
            if set(entry.missing_rule_ids) & target_rule_ids
        ]
        if incomplete_entries:
            blockers.append(
                {
                    "code": "opening_round_nominations_incomplete",
                    "message": (
                        f"This round depends on Opening Round deferred selections; "
                        f"{len(incomplete_entries)} entry/entries have an incomplete nomination. "
                        "Complete nominations in Opening Round Operations before opening this round."
                    ),
                    "url": f"/operations/seasons/{logical['season_id']}/opening-round",
                    "entries": incomplete_entries,
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
        "mapping_history": [item.__dict__ for item in history],
        "fixture_matchups": pairs,
        "afl_matches": match_views,
        "afl_evidence_fresh": evidence_fresh,
        "lockout_triggers": trigger_views,
        "opening_round": {"applies": bool(opening), "deferred_selections": opening},
        "readiness": {
            "safe_to_open": not blockers and state in {"not_created", "upcoming"},
            "blockers": blockers,
            "advisories": advisories,
        },
    }
