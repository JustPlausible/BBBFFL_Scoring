"""Human-facing, fail-closed read model for opening an ordinary BBBFFL round."""

from app.afl_client import is_recognized_match_status
from app.lockouts import LockoutTriggerRepository
from app.opening_round import OpeningRoundNominationRepository
from app.round_mapping import RoundMappingRepository


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
            "SELECT f.*, h.team_name home_team_name, a.team_name away_team_name "
            "FROM season_fixture_matchup f JOIN season_entry h ON h.season_entry_id=f.home_season_entry_id "
            "JOIN season_entry a ON a.season_entry_id=f.away_season_entry_id "
            "WHERE f.fixture_draw_id=? AND f.bbbffl_round_number=? ORDER BY f.matchup_order",
            (draw["fixture_draw_id"], logical["sequence"]),
        ).fetchall()
        pairs = [dict(row) for row in rows]
    if len(pairs) != 5:
        blockers.append(
            {"code": "fixture_invalid", "message": "A frozen fixture with exactly five matchups is required."}
        )

    afl_matches, evidence_error = [], None
    if mapping:
        try:
            afl_matches = afl_client.get_matches(mapping.afl_round_id)
        except Exception as exc:  # evidence failure is operationally meaningful
            evidence_error = str(exc)
            blockers.append(
                {"code": "afl_evidence_unavailable", "message": f"Mapped AFL match evidence is unavailable: {exc}"}
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

    nominations = OpeningRoundNominationRepository(database).list_for_round(round_id)
    opening = []
    for nomination in nominations:
        entry = database.execute(
            "SELECT team_name FROM season_entry WHERE season_entry_id=?", (nomination.season_entry_id,)
        ).fetchone()
        context = OpeningRoundNominationRepository(database).deferred_context(
            round_id, nomination.season_entry_id, nomination.position
        )
        opening.append(
            {**nomination.__dict__, "team_name": entry["team_name"] if entry else "Unknown team", **(context or {})}
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
        "lockout_triggers": trigger_views,
        "opening_round": {"applies": bool(opening), "deferred_selections": opening},
        "readiness": {
            "safe_to_open": not blockers and state in {"not_created", "upcoming"},
            "blockers": blockers,
            "advisories": advisories,
        },
    }
