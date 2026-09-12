"""Shared setup for tests/test_finals_seeding.py and
tests/test_finals_seeding_cli.py: a 2026-year, 20-round, fully-finalised
ordinary season using the ten real 2026 BBBFFL team names, whose
mathematical ladder deliberately differs in order from the fixed historical
finals seed `app.finals_seeding.HISTORICAL_FINALS_SEED_TEAM_NAMES` -- the
exact "two truths" shape issue #187 describes.

Team creation order below is deliberately the *mathematical* Round 20
ladder order from issue #187's worked example (Bridesmaids first, Running
Hots second, ...), and `dominant_scores` (from `tests.midseason_draft_
helpers`) makes every earlier-created team beat every later-created team in
every round -- so `app.ladder.calculate_ladder` reproduces exactly that
creation order. The fixed historical finals seed
(`HISTORICAL_FINALS_SEED_TEAM_NAMES`) starts with Running Hots, not
Bridesmaids, so the two orders provably disagree without needing to
reproduce the exact historical Round 12/13 score lines."""

from tests.midseason_draft_helpers import build_season

MATHEMATICAL_ORDER_TEAM_NAMES = [
    "Bridesmaids",
    "Running Hots",
    "Evil Absolutes",
    "JHAS",
    "Wolverines",
    "The Crabs",
    "One Percenters",
    "Motherruckers",
    "Pommy Rules",
    "The Plague",
]


def build_2026_replay_season(database=None, **kwargs):
    """A season named/shaped for issue #187: year 2026, 20 regular-season
    rounds, all 20 finalised, ten entries named per
    `MATHEMATICAL_ORDER_TEAM_NAMES`. Extra keyword arguments are forwarded to
    `build_season` (e.g. `database=` to reuse an existing connection)."""
    kwargs.setdefault("year", 2026)
    kwargs.setdefault("entry_count", 10)
    kwargs.setdefault("trigger_round", 20)
    kwargs.setdefault("regular_season_round_count", 20)
    kwargs.setdefault("team_names", MATHEMATICAL_ORDER_TEAM_NAMES)
    return build_season(database, **kwargs)


def all_draws(lifecycle, round_id, _entries):
    """A `score_fn` for `build_season`/`build_2026_replay_season`: every
    match in every round is drawn 100-100, so a fair round-robin leaves
    every entry with identical played/wins/draws/losses/PF/PA -- a
    guaranteed, maximal unresolved ladder tie (`app.ladder.LadderRow.
    tied`), for exercising `app.finals_seeding.resolve_finals_seed_order`'s
    refusal to silently treat that tie as a real seed decision."""
    return {match.matchup_id: (100, 100) for match in lifecycle.list_matchups(round_id)}
