"""Shared setup for tests/test_finals*.py: a completed 20-round ordinary
season (via tests.finals_seeding_helpers.build_2026_replay_season, itself
built on tests.midseason_draft_helpers.build_season) plus a `finals`-typed
competition stream ready for `app.finals.FinalsBracketRepository`."""

from datetime import datetime, timezone

from sqlalchemy import text

from app.season import SeasonRepository
from tests.finals_seeding_helpers import build_2026_replay_season


class KnownRound:
    def __init__(self, pairs):
        self.pairs = set(pairs)

    def round_exists(self, afl_season_id, afl_round_id):
        return (afl_season_id, afl_round_id) in self.pairs


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_finals_ready_season(**kwargs):
    """A fully-finalised 20-round 2026-shaped ordinary season (see
    `tests.finals_seeding_helpers.build_2026_replay_season`) plus a sibling
    `finals`-typed `competition_stream` under the same season -- everything
    `FinalsBracketRepository.create_bracket` needs except the seed
    resolution itself (left to the caller: apply a `finals_seeding_snapshot`
    for the snapshot path, or call straight through for the ladder path)."""
    built = build_2026_replay_season(**kwargs)
    database, season = built["database"], built["season"]
    seasons = SeasonRepository(database)
    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (season.season_id,)
    ).fetchone()
    finals_competition = seasons.create_competition(
        season.season_id, rules_row["rules_version_id"], "finals", "Finals", "finals"
    )
    built["finals_competition"] = finals_competition
    built["ordinary_competition_id"] = built["competition"].competition_id
    return built


def accept_week_mapping(database, bbbffl_round_id, *, year, afl_round_id):
    """Accept an AFL mapping for a finals week round -- required before
    `FinalsBracketRepository.open_finals_week` can create its lifecycle row,
    exactly like an ordinary round's preflight mapping acceptance."""
    from app.round_mapping import RoundMappingRepository

    RoundMappingRepository(database).accept(bbbffl_round_id, year, afl_round_id, KnownRound({(year, afl_round_id)}))


def seed_official_result(database, matchup_id: str, home_score, away_score, *, version: int = 1) -> None:
    """Directly seed a finals matchup's first official result, bypassing
    the round-level `publish_results`/`correct_results` (hard-coded to
    exactly five matchups, out of scope for a finals week's variable match
    count -- #191's job) exactly as `tests.test_stream_lifecycle` already
    does for the identical reason. Also advances the owning round's
    lifecycle to `final` if every matchup in it now has a result, mirroring
    what a real (future, #191-built) finals publish command would leave
    behind -- `app.finals`'s own reads never depend on the round-level
    state, only on `bbbffl_matchup`/`bbbffl_official_result`, but keeping
    this consistent avoids tests accidentally relying on an impossible
    round state."""
    now = _now()
    with database.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bbbffl_official_result VALUES (:matchup_id, :version, :home, :away, :now, NULL, NULL, NULL)"
            ),
            {"matchup_id": matchup_id, "version": version, "home": home_score, "away": away_score, "now": now},
        )
        conn.execute(
            text("UPDATE bbbffl_matchup SET effective_official_version=:version WHERE matchup_id=:matchup_id"),
            {"version": version, "matchup_id": matchup_id},
        )


def correct_official_result(database, matchup_id: str, home_score, away_score, *, reason: str):
    """The normal audited correction boundary Steve's confirmed policy
    refers to -- `CompetitionLifecycleRepository.correct_matchup_result`
    already works unchanged for a finals matchup under #197's chosen path
    (it is keyed by `matchup_id`, never `fixture_matchup_id`, and does not
    require the round to hold exactly five matchups)."""
    from app.competition_lifecycle import CompetitionLifecycleRepository

    with database.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE bbbffl_round_lifecycle SET state='final' WHERE bbbffl_round_id="
                "(SELECT bbbffl_round_id FROM bbbffl_matchup WHERE matchup_id=:matchup_id)"
            ),
            {"matchup_id": matchup_id},
        )
    return CompetitionLifecycleRepository(database).correct_matchup_result(
        matchup_id, home_score, away_score, reason=reason
    )
