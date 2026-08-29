"""One-round vertical replay through the production competition services."""

from datetime import datetime, timezone

from sqlalchemy import text

from app.afl_client import Match, Team
from app.audit import ActorContext
from app.calculations import MatchupCalculationService
from app.identity import IdentityRepository
from app.ladder import LadderRepository
from app.lineup_validation import LineupValidationService
from app.lockouts import LockoutRepository
from app.round_review import RoundReviewRepository, attempt_signoff, build_round_review
from tests.round_review_helpers import Facts, full_round, progress_to_review

OPERATOR = ActorContext.anonymous_operator(role="scorer")


class MatchFacts:
    def __init__(self, match):
        self.match = match

    def matches_for(self, round_id):
        return [self.match]


def _semantic_run():
    db, lifecycle, round_, entries, stats, _ = full_round(year=2026, afl_round=1344)
    scope = db.execute(
        "SELECT c.season_id,c.competition_id FROM bbbffl_round r JOIN competition_stream c "
        "ON c.competition_id=r.competition_id WHERE r.bbbffl_round_id=?", (round_.bbbffl_round_id,)
    ).fetchone()
    # Initial replay seeding is the sole direct fixture-load boundary. It
    # supplies ownership and honestly labels the historical proxy action;
    # every subsequent transition uses production services.
    with db.engine.begin() as conn:
        lineups = conn.execute(text("SELECT lineup_id,season_entry_id FROM weekly_lineup")).mappings().all()
        for lineup in lineups:
            conn.execute(text("UPDATE weekly_lineup_submission SET actor_type='anonymous_operator', actor_role='scorer', source_type='scorer_proxy', source_detail='2026 replay manifest', reason='historical lineup reconstructed by replay operator' WHERE lineup_id=:id"), {"id": lineup["lineup_id"]})
            players = conn.execute(text("SELECT season_player_id FROM weekly_lineup_submission_slot WHERE lineup_id=:id"), {"id": lineup["lineup_id"]}).scalars()
            for player in players:
                conn.execute(
                    text(
                        "INSERT INTO player_ownership_period "
                        "(ownership_period_id,season_player_id,season_id,season_entry_id,acquired_at,reason,created_at) "
                        "VALUES (:id,:p,:s,:e,'2026-01-01T00:00:00+00:00','replay initial draft',"
                        "'2026-01-01T00:00:00+00:00')"
                    ),
                    {"id": f"own-{player}", "p": player, "s": scope["season_id"], "e": lineup["season_entry_id"]},
                )

    lifecycle.transition(round_.bbbffl_round_id, "open")
    validation = LineupValidationService(db)
    validation_results = []
    before = datetime(2026, 3, 19, 8, 29, tzinfo=timezone.utc)
    after = datetime(2026, 3, 19, 8, 31, tzinfo=timezone.utc)
    match = Match(2601, Team(1, "Alpha"), Team(2, "Beta"), "UPCOMING", "2026-03-19T08:30:00Z")
    lockouts = LockoutRepository(db)
    lock_evidence = []
    for lineup in lineups:
        positions = {r["position"]: r["season_player_id"] for r in db.execute("SELECT position,season_player_id FROM weekly_lineup_submission_slot WHERE lineup_id=?", (lineup["lineup_id"],)).fetchall()}
        result = validation.validate_submission(lineup["lineup_id"], positions)
        assert result.valid
        validation_results.append(result.to_dict())
        pre = lockouts.lock_state(lineup["lineup_id"], round_.bbbffl_round_id, lineup["season_entry_id"], positions, match_facts=MatchFacts(match), evaluation_at=before)
        post = lockouts.lock_state(lineup["lineup_id"], round_.bbbffl_round_id, lineup["season_entry_id"], positions, match_facts=MatchFacts(match), evaluation_at=after)
        lock_evidence.append({"entry": lineup["season_entry_id"], "before": {k: v.state.value for k, v in pre.positions.items()}, "after": {k: v.state.value for k, v in post.positions.items()}})

    progress_to_review(lifecycle, round_.bbbffl_round_id)
    MatchupCalculationService(db, Facts(stats)).calculate_round(round_.bbbffl_round_id, observed_at="2026-03-23T12:00:00+00:00")
    review_repo = RoundReviewRepository(db)
    identities = IdentityRepository(db)
    review = build_round_review(lifecycle, review_repo, identities, round_.bbbffl_round_id)
    assert review.ready_for_signoff
    attempt_signoff(lifecycle, review_repo, identities, round_.bbbffl_round_id, actor=OPERATOR, reason="representative 2026 replay sign-off")
    matchups = lifecycle.list_matchups(round_.bbbffl_round_id)
    ladder = LadderRepository(db).snapshot(scope["competition_id"], 1)
    official = [lifecycle.effective_result(m.matchup_id) for m in matchups]
    scoring_sources = [slot["scoring_source"] for result in official for side in ("home", "away") for slot in result.input_snapshot[side]["slots"]]
    return {
        "validation_count": len(validation_results), "lockout": lock_evidence,
        "matchups": [(str(x.home_score), str(x.away_score), x.version) for x in official],
        "scoring_sources": scoring_sources,
        "ladder": [(r.rank, r.played, r.wins, r.draws, r.losses, str(r.points_for)) for r in ladder.rows],
        "proxy": [tuple(row) for row in db.execute("SELECT source_type,actor_type,actor_role FROM weekly_lineup_submission ORDER BY lineup_id")],
    }


def test_one_round_replay_composes_and_is_semantically_deterministic():
    first = _semantic_run()
    second = _semantic_run()
    assert first == second
    assert first["validation_count"] == 10
    assert len(first["matchups"]) == 5 and all(version == 1 for _, _, version in first["matchups"])
    assert all(row[1] == 1 for row in first["ladder"])
    assert set(first["scoring_sources"]) == {"ordinary"}
    assert all(row == ("scorer_proxy", "anonymous_operator", "scorer") for row in first["proxy"])
    assert all(set(item["before"].values()) == {"unlocked"} for item in first["lockout"])
    assert all(set(item["after"].values()) == {"locked"} for item in first["lockout"])
