"""Shared setup for tests/test_ladder_tie_ruling*.py (issue #241): a
20-round ordinary season (via `tests.finals_seeding_helpers.
build_2026_replay_season`, itself built on `tests.midseason_draft_helpers.
build_season`, whose default `dominant_scores` score function guarantees
entries[0] is ladder-first through entries[9] ladder-last with no
incidental ties) with two adjacent-rank entries pushed into a deliberate,
exact tie by correcting their own official results after the fact --
generalising the technique `tests.test_season_completion.
test_completion_atomically_refuses_an_internal_consistency_tie_introduced_
after_bracket_freeze` already uses for a last-place pair, to any adjacent
rank pair.

This is deliberately a *result correction*, never a fixture/seed
manipulation: every score written here goes through the same audited
`app.competition_lifecycle.CompetitionLifecycleRepository.
correct_matchup_result` boundary a real Scorer correction would use.

## Why round-number, not opponent identity, decides the correction

The fixture draw does not give the two chosen entries an identical,
symmetric set of opponents (a real round-robin schedule can play some pairs
more often than others across a season not evenly divisible by a clean
double round-robin). Deciding each corrected match's outcome from the
*opponent's* original rank would make the pair's final points-for/against
depend on exactly how many times each opponent happens to appear in each
entry's own schedule -- not reliably equal between the two entries.
Deciding it from the *round number* instead (both entries play exactly one
match per round, so both see the identical sequence of round numbers) makes
the win/loss/draw counts -- and therefore points-for/against/percentage/
competition points -- provably identical between the two regardless of the
fixture's actual opponent distribution."""

from app.season import SeasonRepository
from tests.finals_helpers import correct_official_result
from tests.finals_seeding_helpers import build_2026_replay_season


def build_tied_season(database=None, *, year=2200, tied_ranks=(9, 10), round_count=20, win_rounds=None, **kwargs):
    """Build a season shaped exactly like `build_2026_replay_season`, then
    force the two entries at `tied_ranks` (1-indexed, adjacent, e.g. `(9,
    10)` for last place or `(5, 6)` for the Finals cutoff) into an exact
    mathematical tie: identical competition points, percentage and points
    for, while every other entry's own match results (and therefore its
    ranking relative to every *other* entry) are untouched.

    `win_rounds` controls roughly where the pair lands: the pair wins every
    non-head-to-head match in rounds `1..win_rounds` and loses every one in
    the rest, so a high `win_rounds` lands the pair near the top of the
    ladder and a low one near the bottom. Defaults to a value derived from
    `tied_ranks` itself (higher ranks near the top get a higher default).

    Returns the same `built` dict `build_2026_replay_season` does, plus
    `ordinary_competition_id` and `tied_pair` (the two tied entries'
    `season_entry_id`, sorted -- ready to pass as `decided_order`'s member
    set, or in either order, to `app.ladder_tie_ruling.record_ruling`)."""
    kwargs.setdefault("regular_season_round_count", round_count)
    kwargs.setdefault("trigger_round", round_count)
    built = build_2026_replay_season(database=database, year=year, **kwargs)
    database = built["database"]
    entries = built["entries"]
    competition_id = built["competition"].competition_id

    lower_index, higher_index = sorted(rank - 1 for rank in tied_ranks)
    if higher_index != lower_index + 1:
        raise ValueError("tied_ranks must name two adjacent ladder ranks")
    if win_rounds is None:
        win_rounds = round(round_count * (len(entries) - 1 - lower_index) / (len(entries) - 1))
    pair_ids = {entries[lower_index].season_entry_id, entries[higher_index].season_entry_id}

    matches = database.execute(
        "SELECT m.matchup_id, m.home_season_entry_id, m.away_season_entry_id, l.fixture_round_number FROM "
        "bbbffl_matchup m JOIN bbbffl_round_lifecycle l ON l.bbbffl_round_id = m.bbbffl_round_id "
        "WHERE l.competition_id=? AND l.fixture_round_number<=?",
        (competition_id, round_count),
    ).fetchall()
    for match in matches:
        home, away = match["home_season_entry_id"], match["away_season_entry_id"]
        home_in_pair, away_in_pair = home in pair_ids, away in pair_ids
        if not home_in_pair and not away_in_pair:
            continue
        if home_in_pair and away_in_pair:
            correct_official_result(database, match["matchup_id"], 40, 40, reason="fixture: forced tie head-to-head")
            continue
        pair_wins = match["fixture_round_number"] <= win_rounds
        pair_score, opponent_score = (45, 40) if pair_wins else (40, 45)
        scores = (pair_score, opponent_score) if home_in_pair else (opponent_score, pair_score)
        correct_official_result(database, match["matchup_id"], *scores, reason="fixture: forced tie vs external")

    built["ordinary_competition_id"] = competition_id
    built["tied_pair"] = sorted(pair_ids)
    return built


def build_tied_finals_ready_season(**kwargs):
    """`tests.finals_helpers.build_finals_ready_season`'s exact shape (a
    sibling `finals`-typed competition stream ready for
    `app.finals.FinalsBracketRepository`), built on `build_tied_season`
    instead of the plain untied `build_2026_replay_season`."""
    built = build_tied_season(**kwargs)
    database, season = built["database"], built["season"]
    seasons = SeasonRepository(database)
    rules_row = database.execute(
        "SELECT rules_version_id FROM season_rules_version WHERE season_id=?", (season.season_id,)
    ).fetchone()
    finals_competition = seasons.create_competition(
        season.season_id, rules_row["rules_version_id"], "finals", "Finals", "finals"
    )
    built["finals_competition"] = finals_competition
    return built
