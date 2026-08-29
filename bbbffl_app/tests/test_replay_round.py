"""One round: controlled evidence/time through every production domain."""

import json
from pathlib import Path

from sqlalchemy import text

from app.audit import ActorContext
from app.calculations import MatchupCalculationService
from app.identity import IdentityRepository
from app.lineup_validation import LineupValidationService
from app.lockouts import LockoutRepository, LockoutTriggerRepository, RoundMatchFactsProvider
from app.replay import (
    ReplayAflDataSource,
    ReplayClock,
    build_completed_round_report,
    write_replay_report,
)
from app.round_mapping import RoundMappingRepository
from app.round_review import RoundReviewRepository, attempt_signoff, build_round_review
from tests.round_review_helpers import full_round, progress_to_review

FIXTURE = Path(__file__).parent / "fixtures" / "replay_round_2026" / "evidence.json"
OPERATOR = ActorContext.anonymous_operator(role="scorer")


def _semantic_run(output: Path):
    before = ReplayClock.from_iso("2026-03-19T08:29:00Z")
    after = ReplayClock.from_iso("2026-03-19T08:31:00Z")
    final = ReplayClock.from_iso("2026-03-19T12:01:00Z")
    before_source = ReplayAflDataSource(FIXTURE, clock=before)
    after_source = ReplayAflDataSource(FIXTURE, clock=after)
    final_source = ReplayAflDataSource(FIXTURE, clock=final)

    # full_round is only the clean relational initialiser. Its AFL facts are
    # deliberately discarded: every identity, match and stat consumed below
    # is replaced by and resolved from the checked-in replay manifest.
    db, lifecycle, round_, _, _, _ = full_round(year=2026, afl_round=1344)
    scope = db.execute(
        "SELECT c.season_id,c.competition_id FROM bbbffl_round r JOIN competition_stream c "
        "ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?",
        (round_.bbbffl_round_id,),
    ).fetchone()
    with db.engine.begin() as conn:
        lineups = (
            conn.execute(
                text(
                    "SELECT l.lineup_id,l.season_entry_id,n.team_name FROM weekly_lineup l "
                    "JOIN season_entry_team_name_history n ON n.season_entry_id=l.season_entry_id "
                    "AND n.ended_at IS NULL ORDER BY n.team_name"
                )
            )
            .mappings()
            .all()
        )
        for lineup in lineups:
            manifest_lineup = next(
                item for item in final_source.lineup_inputs if item["historical_entry"] == lineup["team_name"]
            )
            conn.execute(
                text(
                    "UPDATE weekly_lineup_submission SET actor_type='anonymous_operator', actor_role='scorer', "
                    "source_type='scorer_proxy', source_detail='2026-round-1-representative@2.0.0', "
                    "reason='historical lineup reconstructed by replay operator' WHERE lineup_id=:id"
                ),
                {"id": lineup["lineup_id"]},
            )
            slots = conn.execute(
                text("SELECT s.position,s.season_player_id FROM weekly_lineup_submission_slot s WHERE s.lineup_id=:id"),
                {"id": lineup["lineup_id"]},
            ).mappings()
            for slot in slots:
                canonical = manifest_lineup["positions"][slot["position"]]
                player = final_source.get_player(canonical)
                conn.execute(
                    text(
                        "UPDATE season_player_pool SET canonical_player_id=:canonical,display_name=:name,"
                        "afl_team_id=:team_id,afl_team_name=:team_name WHERE season_player_id=:player_id"
                    ),
                    {
                        "canonical": canonical,
                        "name": player.name,
                        "team_id": player.current_team.team_id,
                        "team_name": player.current_team.name,
                        "player_id": slot["season_player_id"],
                    },
                )
                conn.execute(
                    text(
                        "INSERT INTO player_ownership_period "
                        "(ownership_period_id,season_player_id,season_id,season_entry_id,acquired_at,reason,created_at) "
                        "VALUES (:id,:player,:season,:entry,'2026-01-01T00:00:00+00:00',"
                        "'replay initial draft','2026-01-01T00:00:00+00:00')"
                    ),
                    {
                        "id": f"own-{slot['season_player_id']}",
                        "player": slot["season_player_id"],
                        "season": scope["season_id"],
                        "entry": lineup["season_entry_id"],
                    },
                )

    lifecycle.transition(round_.bbbffl_round_id, "open")
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
