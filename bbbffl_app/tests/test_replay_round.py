"""One round: controlled evidence/time through every production domain."""

import json
from pathlib import Path

from app.audit import ActorContext
from app.calculations import MatchupCalculationService
from app.identity import IdentityRepository
from app.lineup_proxy import LineupProxyService
from app.lineup_validation import LineupValidationService
from app.lockouts import LockoutRepository, LockoutTriggerRepository, RoundMatchFactsProvider
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.replay import (
    ReplayAflDataSource,
    ReplayClock,
    build_completed_round_report,
    write_replay_report,
)
from app.round_mapping import RoundMappingRepository
from app.round_review import RoundReviewRepository, attempt_signoff, build_round_review
from tests.db_helpers import migrated_connection
from tests.round_review_helpers import progress_to_review
from tests.test_competition_lifecycle import operational

FIXTURE = Path(__file__).parent / "fixtures" / "replay_round_2026" / "evidence.json"
OPERATOR = ActorContext.anonymous_operator(role="scorer")


def _semantic_run(output: Path):
    before = ReplayClock.from_iso("2026-03-19T08:29:00Z")
    after = ReplayClock.from_iso("2026-03-19T08:31:00Z")
    final = ReplayClock.from_iso("2026-03-19T12:01:00Z")
    before_source = ReplayAflDataSource(FIXTURE, clock=before)
    after_source = ReplayAflDataSource(FIXTURE, clock=after)
    final_source = ReplayAflDataSource(FIXTURE, clock=final)

    db = migrated_connection()
    lifecycle, round_, entries = operational(db, year=2026, afl_round=1344)
    scope = db.execute(
        "SELECT c.season_id,c.competition_id FROM bbbffl_round r JOIN competition_stream c "
        "ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()
    lifecycle.transition(round_.bbbffl_round_id, "open")
    pool = PlayerPoolRepository(db)
    ownership = OwnershipRepository(db)
    ownership.configure_squad_limit(
        scope["season_id"], 9, actor=OPERATOR, reason="representative replay squad configuration"
    )
    proxy = LineupProxyService(db, before_source)
    identity_repository = IdentityRepository(db)
    entries_by_team = {identity_repository.get_public_team(entry.season_entry_id).team_name: entry for entry in entries}
    for manifest_lineup in final_source.lineup_inputs:
        entry = entries_by_team[manifest_lineup["historical_entry"]]
        positions = {}
        for position, canonical in manifest_lineup["positions"].items():
            evidence_player = final_source.get_player(canonical)
            season_player = pool.refresh_player(
                scope["season_id"],
                canonical,
                evidence_player.name,
                afl_team_id=evidence_player.current_team.team_id,
                afl_team_name=evidence_player.current_team.name,
                source_provider="replay:2026-round-1-representative@2.0.0",
                source_fetched_at="2026-03-19T08:29:00+00:00",
            )
            ownership.acquire(
                season_player.season_player_id,
                entry.season_entry_id,
                effective_at="2026-01-01T00:00:00+00:00",
                actor=OPERATOR,
                reason="representative replay initial squad",
            )
            positions[position] = season_player.season_player_id
        draft = proxy.create_or_amend(
            scope["season_id"],
            scope["competition_id"],
            round_.bbbffl_round_id,
            entry.season_entry_id,
            positions,
            expected_revision=0,
            actor=OPERATOR,
        )
        proxy.submit(
            draft.lineup_id,
            expected_draft_revision=draft.revision,
            expected_submission_version=0,
            actor=OPERATOR,
            reason="historical lineup reconstructed from replay evidence 2026-round-1-representative@2.0.0",
        )
    lineups = db.execute(
        "SELECT l.lineup_id,l.season_entry_id,n.team_name FROM weekly_lineup l "
        "JOIN season_entry_team_name_history n ON n.season_entry_id=l.season_entry_id "
        "AND n.ended_at IS NULL ORDER BY n.team_name"
    ).fetchall()
    LockoutTriggerRepository(db).create(
        round_.bbbffl_round_id,
        "main",
        "main",
        1,
        [2601],
        actor=OPERATOR,
        reason="representative replay main lockout",
    )
    mapping = RoundMappingRepository(db)
    validation_service = LineupValidationService(db, before_source)
    lockouts = LockoutRepository(db)
    validation, lockout_by_entry = [], {}
    for lineup in lineups:
        positions = {
            row["position"]: row["season_player_id"]
            for row in db.execute(
                "SELECT position,season_player_id FROM weekly_lineup_submission_slot WHERE lineup_id=?",
                (lineup["lineup_id"],),
            ).fetchall()
        }
        checked = validation_service.validate_submission(lineup["lineup_id"], positions)
        assert checked.valid
        validation.append({"historical_entry": lineup["team_name"], **checked.to_dict()})
        lockout_by_entry[lineup["team_name"]] = {"positions_input": positions}

    # Replay advances monotonically. Evaluate every lineup at the pre-lock
    # checkpoint before advancing any lineup to the post-lock checkpoint:
    # trigger activation is intentionally durable production state and must
    # never be "time-travelled" backwards for the next entry.
    for label, clock, source in (("before", before, before_source), ("after", after, after_source)):
        for lineup in lineups:
            positions = lockout_by_entry[lineup["team_name"]]["positions_input"]
            view = lockouts.lock_state(
                lineup["lineup_id"],
                round_.bbbffl_round_id,
                lineup["season_entry_id"],
                positions,
                match_facts=RoundMatchFactsProvider(mapping, source),
                evaluation_at=clock.now(),
            )
            lockout_by_entry[lineup["team_name"]][label] = {
                "effective_at": view.evaluated_at,
                "positions": {
                    position: {"state": state.state.value, "reason": state.reason, "afl_match_id": state.afl_match_id}
                    for position, state in view.positions.items()
                },
            }
    lockout_evidence = [
        {
            "historical_entry": lineup["team_name"],
            "before": lockout_by_entry[lineup["team_name"]]["before"],
            "after": lockout_by_entry[lineup["team_name"]]["after"],
        }
        for lineup in lineups
    ]

    progress_to_review(lifecycle, round_.bbbffl_round_id)
    MatchupCalculationService(db, final_source).calculate_round(
        round_.bbbffl_round_id,
        upstream_revision=final_source.manifest["version"],
        observed_at=final.now().isoformat(),
    )
    review_repo = RoundReviewRepository(db)
    identities = IdentityRepository(db)
    review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    assert review.ready_for_signoff
    attempt_signoff(
        lifecycle,
        review_repo,
        identities,
        round_.bbbffl_round_id,
        actor=OPERATOR,
        reason="representative 2026 replay sign-off",
    )
    report = build_completed_round_report(
        db,
        lifecycle,
        round_.bbbffl_round_id,
        final_source,
        clocks={"before_lockout": before, "after_lockout": after, "finalisation": final},
        lockout=lockout_evidence,
        validation=validation,
    )
    write_replay_report(report, output, output.with_suffix(".txt"))
    return report


def test_one_round_replay_composes_and_real_report_is_semantically_deterministic(tmp_path):
    first = _semantic_run(tmp_path / "first.json")
    second = _semantic_run(tmp_path / "second.json")
    assert first == second
    assert json.loads((tmp_path / "first.json").read_text()) == first
    assert len(first["lineups"]) == 10
    assert len(first["scoring"]) == len(first["official_results"]) == 5
    assert all(result["version"] == 1 and result["effective"] for result in first["official_results"])
    assert all(row["played"] == 1 for row in first["ladder"])
    assert {
        slot["scoring_source"]
        for matchup in first["scoring"]
        for side in ("home", "away")
        for slot in matchup[side]["slots"]
    } == {"ordinary"}
    assert all(lineup["source_type"] == "scorer_proxy" for lineup in first["lineups"])
    assert all(
        {position["state"] for position in item["before"]["positions"].values()} == {"editable"}
        and {position["state"] for position in item["after"]["positions"].values()} == {"locked"}
        for item in first["lockout"]
    )
    assert {record["evidence_class"] for record in first["evidence"]} >= {
        "known_fact",
        "reconstructable_behaviour",
        "synthetic_scenario",
    }
