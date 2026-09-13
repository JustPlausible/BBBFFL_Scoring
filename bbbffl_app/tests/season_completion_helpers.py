"""Shared setup for tests/test_season_completion*.py and
tests/test_completed_season_fence.py (issue #195): a full 2026-shaped
replay season driven all the way through a published Grand Final (every
finals week `final`) and four published SuperScore leaderboards (SS1-SS4
`final`) -- exactly `app.season_completion.complete_season`'s readiness
gate, built from the same primitives `tests/test_finals.py` and
`tests/test_superscore_results.py` already establish for issues #190/#193,
never a fresh simulation of either."""

from sqlalchemy import text

from app.afl_client import Match, PlayerStatLine, Team
from app.audit import ActorContext
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.db import transaction
from app.finals import FinalsBracketRepository
from app.finals_preflight import open_finals_week
from app.lineups import POSITIONS
from app.season import SeasonRepository, _now
from app.superscore_results import SuperScoreLeaderboardService
from app.superscore_round import confirm_afl_mapping, ensure_round, ensure_stream, open_round
from tests.finals_helpers import KnownRound, accept_week_mapping, build_finals_ready_season, seed_official_result

ACTOR = ActorContext.anonymous_operator("test")
FINALS_AFL_ROUNDS = {1: 9021, 2: 9022, 3: 9023, 4: 9024}


class _Facts:
    """A duck-typed AFL client stand-in, matching `tests/test_superscore_
    results.py`'s own `Facts` -- one completed match, one canonical stat
    line per entry via each entry's own F1 selection."""

    def __init__(self, stats):
        self.stats = stats
        self.match = Match(9900, Team(1, "A"), Team(2, "B"), "completed")

    def get_matches(self, _round_id):
        return [self.match]

    def get_match_player_stats(self, _match_id):
        return self.stats


def _finalize_round(database, bbbffl_round_id):
    """Directly force a round's lifecycle to `final`, bypassing the
    intermediate transitions -- the same shortcut `tests/finals_helpers.
    correct_official_result` already takes for an ordinary/finals round."""
    with database.engine.begin() as conn:
        conn.execute(
            text("UPDATE bbbffl_round_lifecycle SET state='final' WHERE bbbffl_round_id=:rid"),
            {"rid": bbbffl_round_id},
        )


def _play_out_finals(built, year):
    database = built["database"]
    repo = FinalsBracketRepository(database)
    bracket = repo.create_bracket(
        built["season"].season_id,
        built["finals_competition"].competition_id,
        built["ordinary_competition_id"],
        actor=ACTOR,
        reason="season-completion test setup",
    )["bracket"]
    for week in (1, 2, 3, 4):
        round_id = repo.get_week_round_id(bracket.bracket_id, week)
        accept_week_mapping(database, round_id, year=year, afl_round_id=FINALS_AFL_ROUNDS[week])

    open_finals_week(database, bracket.bracket_id, 1, actor=ACTOR)
    pairings = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=1)}
    seed_official_result(database, pairings["qf"].matchup_id, 100, 50)
    seed_official_result(database, pairings["ef"].matchup_id, 100, 50)
    _finalize_round(database, repo.get_week_round_id(bracket.bracket_id, 1))
    repo.advance_bracket(bracket.bracket_id, 1, actor=ACTOR, reason="advance from week 1")

    open_finals_week(database, bracket.bracket_id, 2, actor=ACTOR)
    pairings = {p.slot: p for p in repo.list_pairings(bracket.bracket_id, week_number=2)}
    seed_official_result(database, pairings["second_semi"].matchup_id, 100, 50)
    seed_official_result(database, pairings["first_semi"].matchup_id, 100, 50)
    _finalize_round(database, repo.get_week_round_id(bracket.bracket_id, 2))
    repo.advance_bracket(bracket.bracket_id, 2, actor=ACTOR, reason="advance from week 2")

    open_finals_week(database, bracket.bracket_id, 3, actor=ACTOR)
    pf = repo.list_pairings(bracket.bracket_id, week_number=3)[0]
    seed_official_result(database, pf.matchup_id, 100, 50)
    _finalize_round(database, repo.get_week_round_id(bracket.bracket_id, 3))
    repo.advance_bracket(bracket.bracket_id, 3, actor=ACTOR, reason="advance from week 3")

    open_finals_week(database, bracket.bracket_id, 4, actor=ACTOR)
    gf = repo.list_pairings(bracket.bracket_id, week_number=4)[0]
    seed_official_result(database, gf.matchup_id, 100, 50)
    _finalize_round(database, repo.get_week_round_id(bracket.bracket_id, 4))

    built["bracket"] = bracket
    built["grand_final_pairing"] = gf
    built["grand_final_matchup_id"] = gf.matchup_id
    return bracket


def _setup_round_without_the_pre_existing_postgres_count_for_update_bug(database, bbbffl_round_id, entries, *, reason):
    """A test-only stand-in for `app.superscore_round.setup_round`.

    `setup_round`/`_create_review_state_rows`'s own verification query
    (`SELECT COUNT(*) ... FOR UPDATE`) is rejected outright by real
    PostgreSQL ("FOR UPDATE is not allowed with aggregate functions") --
    a pre-existing bug in issue #192/#193's own code, confirmed present on
    the unmodified base branch and unrelated to issue #195's write fence.
    Fixing it would mean touching `app.superscore_round`, which issue
    #195's scope explicitly restricts to "adding the shared guard" only
    (this module needs no guard -- it does not change official results).
    This helper performs the identical `create_non_ordinary_round` +
    `superscore_entry_review_state` row creation `setup_round` does,
    without that one query, so `tests/test_season_completion_postgresql.py`
    can build a genuine completable season against real PostgreSQL."""
    lifecycle = CompetitionLifecycleRepository(database)
    round_row = lifecycle.get_round(bbbffl_round_id)
    if round_row is None:
        round_row = lifecycle.create_non_ordinary_round(bbbffl_round_id, actor=ACTOR, reason=reason)
    now = _now()
    with transaction(database) as conn:
        for entry in entries:
            conn.execute(
                "INSERT INTO superscore_entry_review_state "
                "(bbbffl_round_id, season_entry_id, review_version, created_at, updated_at) "
                "VALUES (?, ?, 0, ?, ?) ON CONFLICT (bbbffl_round_id, season_entry_id) DO NOTHING",
                (bbbffl_round_id, entry.season_entry_id, now, now),
            )
    return round_row


def _seed_and_publish_superscore_round(database, competition_id, round_id, entries, *, number, year):
    stats = {}
    now = _now()
    with database.engine.begin() as conn:
        for index, entry in enumerate(entries):
            lineup_id = f"ss{number}-lineup-{year}-{index}"
            canonical = 9_000_000 + year * 100 + number * 10 + index
            player_id = f"ss{number}-player-{year}-{index}"
            conn.execute(
                text(
                    "INSERT INTO season_player_pool (season_player_id,season_id,canonical_player_id,display_name,"
                    "afl_team_id,eligible,source_provider,source_fetched_at,created_at,updated_at) "
                    "VALUES (:p,:s,:c,:n,1,TRUE,'test',:now,:now,:now)"
                ),
                {"p": player_id, "s": entry.season_id, "c": canonical, "n": f"SS{number} Player {index}", "now": now},
            )
            conn.execute(
                text(
                    "INSERT INTO weekly_lineup (lineup_id,season_id,competition_id,bbbffl_round_id,season_entry_id,"
                    "draft_revision,effective_submission_version,created_at,updated_at) "
                    "VALUES (:l,:s,:c,:r,:e,1,1,:now,:now)"
                ),
                {
                    "l": lineup_id,
                    "s": entry.season_id,
                    "c": competition_id,
                    "r": round_id,
                    "e": entry.season_entry_id,
                    "now": now,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO weekly_lineup_submission (lineup_id,version,based_on_draft_revision,submitted_at,"
                    "actor_type,actor_role,source_type) VALUES (:l,1,1,:now,'coach','coach','coach')"
                ),
                {"l": lineup_id, "now": now},
            )
            for position in POSITIONS:
                selected = player_id if position == "F1" else None
                conn.execute(
                    text("INSERT INTO weekly_lineup_submission_slot VALUES (:l,1,:pos,:p)"),
                    {"l": lineup_id, "pos": position, "p": selected},
                )
            stats[canonical] = PlayerStatLine(canonical, goals=index + 1)
    open_round(database, round_id, actor=ACTOR, reason=f"open SS{number} for season completion test")
    lifecycle = CompetitionLifecycleRepository(database)
    lifecycle.transition(round_id, "live", actor=ACTOR, reason=f"SS{number} live")
    lifecycle.transition(round_id, "review", actor=ACTOR, reason=f"SS{number} review")
    service = SuperScoreLeaderboardService(database, _Facts(stats))
    service.publish(round_id, actor=ACTOR, reason=f"publish SS{number} for season completion test")
    return stats


def _play_out_superscore(built, year):
    database = built["database"]
    season_id = built["season"].season_id
    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (season_id,)
    ).fetchone()
    stream = ensure_stream(
        database,
        season_id,
        rules_row["rules_version_id"],
        built["ordinary_competition_id"],
        actor=ACTOR,
        reason="superscore stream setup for season completion test",
    )
    validator = KnownRound({(year, afl_round_id) for afl_round_id in FINALS_AFL_ROUNDS.values()})
    superscore_rounds = {}
    superscore_stats = {}
    for number in range(1, 5):
        round_id = ensure_round(database, stream.competition_id, number, number)
        confirm_afl_mapping(
            database,
            validator,
            round_id,
            year,
            FINALS_AFL_ROUNDS[number],
            actor=ACTOR,
            reason=f"SS{number} mapping for season completion test",
        )
        _setup_round_without_the_pre_existing_postgres_count_for_update_bug(
            database, round_id, built["entries"], reason=f"SS{number} setup for season completion test"
        )
        superscore_rounds[number] = round_id
        superscore_stats[number] = _seed_and_publish_superscore_round(
            database, stream.competition_id, round_id, built["entries"], number=number, year=year
        )
    built["superscore_stream"] = stream
    built["superscore_rounds"] = superscore_rounds
    built["superscore_stats"] = superscore_stats


def build_completable_season(year=3000, **kwargs):
    """A `build_finals_ready_season`-shaped season (20-round ordinary
    competition, `finals` stream) plus a `superscore` stream, driven all
    the way through: every finals week published and `final`, and every
    one of SS1-SS4 published (via the real `SuperScoreLeaderboardService.
    publish` path) and `final`. Exactly what `app.season_completion.
    complete_season`'s readiness gate (step 2) requires before it will
    proceed. Returns the same `built` dict `build_finals_ready_season`
    does, with `bracket`/`grand_final_pairing`/`grand_final_matchup_id`/
    `superscore_stream`/`superscore_rounds` added."""
    built = build_finals_ready_season(year=year, **kwargs)
    # `build_season`/`build_2026_replay_season` leave the season in its
    # default `setup` lifecycle state -- `app.season_completion.
    # complete_season` requires `active`, matching `LEGAL_TRANSITIONS`
    # (`setup -> active -> completed`).
    SeasonRepository(built["database"]).transition_lifecycle(
        built["season"].season_id, "active", actor=ACTOR, reason="activate season for completion test"
    )
    _play_out_finals(built, year)
    _play_out_superscore(built, year)
    return built
