"""Shared setup for tests/test_midseason_draft.py: a season with a
configurable number of season entries and finalised rounds, and starting
squads seeded directly onto the authoritative ownership ledger (no full
preseason draft is needed to exercise the mid-season draft's own
lifecycle)."""

from app.competition_lifecycle import CompetitionLifecycleRepository
from app.fixtures import FixtureRepository
from app.identity import IdentityRepository
from app.player_pool import OwnershipRepository, PlayerPoolRepository
from app.round_mapping import RoundMappingRepository
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection


class KnownRound:
    def __init__(self, pairs):
        self.pairs = set(pairs)

    def round_exists(self, afl_season_id, afl_round_id):
        return (afl_season_id, afl_round_id) in self.pairs


def dominant_scores(lifecycle, round_id, entries):
    """Every entry beats every entry that appears later in `entries`, with
    the winning margin keyed off the *winner's own* rank (not just the
    win/loss outcome) -- a standard round-robin draw pairs some indices
    (e.g. 1 & 2) against an identical opponent set, so win/loss counts
    alone would tie them; scoring wins by the winner's rank keeps
    cumulative points-for strictly monotonic by index, so entries[0] is
    ladder-first and entries[-1] is ladder-last with no ties."""
    order = {entry.season_entry_id: index for index, entry in enumerate(entries)}
    result = {}
    for match in lifecycle.list_matchups(round_id):
        home_rank = order[match.home_season_entry_id]
        away_rank = order[match.away_season_entry_id]
        winner_score = 300 - 10 * min(home_rank, away_rank)
        loser_score = 50
        if home_rank < away_rank:
            result[match.matchup_id] = (winner_score, loser_score)
        else:
            result[match.matchup_id] = (loser_score, winner_score)
    return result


def build_season(
    database=None,
    *,
    year=2200,
    entry_count=10,
    trigger_round=10,
    regular_season_round_count=None,
    squad_limit=4,
    score_fn=dominant_scores,
):
    """Ten (by default) season entries, `trigger_round` rounds finalised
    through `app.competition_lifecycle`, and every entry given `squad_limit`
    owned players directly on `player_ownership_period` -- exactly what
    `app.midseason_draft` needs (a computable ladder, and squads to
    delist/trade/replace) without a full preseason draft's machinery."""
    database = database or migrated_connection()
    regular_season_round_count = regular_season_round_count or max(trigger_round, 10)

    seasons = SeasonRepository(database)
    season = seasons.create_season(year, str(year), regular_season_round_count=regular_season_round_count)
    rules = seasons.create_rules_version(season.season_id, "ordinary", 1, "Rules")
    competition = seasons.create_competition(
        season.season_id, rules.rules_version_id, "ordinary", "Ordinary", "ordinary"
    )

    identities = IdentityRepository(database)
    entries = []
    for number in range(entry_count):
        coach = identities.create_coach(f"Coach {year}-{number}")
        entries.append(
            identities.create_entry(season.season_id, f"licence-{year}-{number}", coach.coach_id, f"Team {number}")
        )

    fixtures = FixtureRepository(database)
    fixtures.save_draft(season.season_id, [entry.season_entry_id for entry in entries])
    fixtures.freeze(season.season_id)

    mapping = RoundMappingRepository(database)
    logical_rounds = {}
    for number in range(1, regular_season_round_count + 1):
        logical_rounds[number] = seasons.create_round(
            competition.competition_id, f"round-{number}", f"Round {number}", number
        )
        mapping.accept(logical_rounds[number].bbbffl_round_id, year, 1000 + number, KnownRound({(year, 1000 + number)}))

    lifecycle = CompetitionLifecycleRepository(database)
    for number in range(1, trigger_round + 1):
        round_ = lifecycle.create_ordinary_round(logical_rounds[number].bbbffl_round_id)
        lifecycle.transition(round_.bbbffl_round_id, "open")
        lifecycle.transition(round_.bbbffl_round_id, "live")
        lifecycle.transition(round_.bbbffl_round_id, "review")
        lifecycle.publish_results(round_.bbbffl_round_id, score_fn(lifecycle, round_.bbbffl_round_id, entries))

    ownership = OwnershipRepository(database)
    ownership.configure_squad_limit(season.season_id, squad_limit)
    player_pool = PlayerPoolRepository(database)
    canonical = 1
    for entry in entries:
        for _ in range(squad_limit):
            canonical += 1
            player = player_pool.refresh_player(season.season_id, 900_000 + canonical, f"Starting player {canonical}")
            ownership.acquire(player.season_player_id, entry.season_entry_id)

    return {
        "database": database,
        "season": season,
        "competition": competition,
        "entries": entries,
        "logical_rounds": logical_rounds,
        "lifecycle": lifecycle,
        "ownership": ownership,
        "player_pool": player_pool,
        "identities": identities,
        "squad_limit": squad_limit,
        "regular_season_round_count": regular_season_round_count,
    }
