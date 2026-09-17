"""Allow-listed spectator read models for persisted finals weeks and their
concurrent SuperScore round (issue #213) -- the finals/SuperScore
counterpart to `app.public_rounds`, extending the existing public Round
Centre from Round 20 through Finals Week 1, Finals Week 2, the
Preliminary Final and the Grand Final rather than inventing a parallel
public navigation system.

Reuses, never reimplements:

- `app.finals.FinalsBracketRepository.list_pairings` for a week's
  bye/pairing structure -- the exact bracket read model
  `app/round_preflight.py`'s finals branch and
  `app.finals_superscore_dashboard._build_finals_section` already render
  for the Scorer.
- `app.finals_review.build_finals_round_review` (never the ordinary,
  five-matchup-only `app.round_review.build_round_review`) for a finals
  matchup's calculated/official state, and `app.public_rounds._side`/
  `authoritative_submissions`/`authoritative_player_names` to turn that
  into the identical public lineup-evidence shape ordinary rounds already
  expose -- both stream-agnostic, so reused as-is rather than duplicated.
- `app.superscore_results.SuperScoreLeaderboardService.leaderboard` for
  the published SuperScore result -- returns ``None`` until a leaderboard
  has actually been published, which is this module's entire "not yet
  published" safety boundary; no draft/calculation table is ever read
  here.
- `app.public_rounds._round_definition`/`_round_summary`/
  `_select_default_round_number` -- all already stream-agnostic (they key
  off `bbbffl_round.sequence`, which is the finals week number/SuperScore
  round number exactly as it is the ordinary round number), so a finals
  week's "defined but not yet opened" placeholder reuses the identical
  policy an ordinary round's does, rather than a second copy of it.

Never reads a scorer-only table (`superscore_entry_review_state`,
`superscore_entry_calculation`, DNP/interchange rulings, override
reasons) and never exposes an internal id as primary UI content beyond
the same `bbbffl_round_id`/`season_entry_id` public routes already key
on.
"""

from app.finals import SLOT_LABELS, WEEK_LABELS, FinalsBracketRepository
from app.finals_review import build_finals_round_review
from app.public_rounds import (
    MATCH_STATUS_LABELS,
    _ordinary_competition_id,
    _round_definition,
    _round_summary,
    _select_default_round_number,
    _side,
    _team_name,
    authoritative_player_names,
    authoritative_submissions,
    match_score_state,
)
from app.stream_presentation import humanize_round_label
from app.superscore_results import SuperScoreLeaderboardService

SCHEDULED_STATUS_LABEL = "Scheduled — not yet started; lineups and scores are not yet available."


def finals_competition_id(database, season_id):
    """`_ordinary_competition_id`'s finals sibling -- the season's
    `finals`-typed `competition_stream.competition_id`, or ``None`` if
    none has been provisioned for this season yet."""
    row = database.execute(
        "SELECT competition_id FROM competition_stream WHERE season_id=? AND stream_type='finals'",
        (season_id,),
    ).fetchone()
    return row["competition_id"] if row else None


def _superscore_round_row(database, season_id, week_number):
    return database.execute(
        "SELECT sr.bbbffl_round_id, sr.label FROM bbbffl_round sr "
        "JOIN competition_stream sc ON sc.competition_id=sr.competition_id "
        "WHERE sc.season_id=? AND sc.stream_type='superscore' AND sr.round_key=?",
        (season_id, f"ss{week_number}"),
    ).fetchone()


def build_public_superscore_round(database, afl_client, identities, season_id, week_number):
    """The public DTO for the SuperScore round concurrent with one finals
    week -- entry-based (all ten season entries), never a fabricated
    opponent/matchup. ``available`` is false only when SS{week_number}
    has not been provisioned for this season at all; once provisioned but
    not yet published, ``entries`` stays empty rather than exposing any
    draft/calculated score."""
    fallback_label = f"SuperScore {week_number}"
    round_row = _superscore_round_row(database, season_id, week_number)
    if round_row is None:
        return {
            "available": False,
            "week_number": week_number,
            "round_id": None,
            "round_label": fallback_label,
            "published": False,
            "published_at": None,
            "entries": [],
        }
    round_label = humanize_round_label("superscore", round_row["label"]) or fallback_label
    leaderboard = SuperScoreLeaderboardService(database, afl_client, identities).leaderboard(
        round_row["bbbffl_round_id"]
    )
    if leaderboard is None:
        return {
            "available": True,
            "week_number": week_number,
            "round_id": round_row["bbbffl_round_id"],
            "round_label": round_label,
            "published": False,
            "published_at": None,
            "entries": [],
        }
    return {
        "available": True,
        "week_number": week_number,
        "round_id": round_row["bbbffl_round_id"],
        "round_label": round_label,
        "published": True,
        "published_at": leaderboard["published_at"],
        "entries": leaderboard["entries"],
    }


def _pairing_preview(pairing, identities):
    return {
        "slot": pairing.slot,
        "slot_label": SLOT_LABELS.get(pairing.slot, pairing.slot),
        "status": "scheduled",
        "status_label": SCHEDULED_STATUS_LABEL,
        "published_at": None,
        "home": {"team": {"name": _team_name(identities, pairing.home_season_entry_id)}},
        "away": {"team": {"name": _team_name(identities, pairing.away_season_entry_id)}},
    }


def _finals_matchups(database, lifecycle, review_repo, identities, bracket_id, week_number, round_id, round_):
    """Bye + matchup cards for one *opened* finals week -- `round_` is the
    already-resolved `CompetitionRound` (its lifecycle row exists)."""
    pairings = FinalsBracketRepository(database).list_pairings(bracket_id, week_number=week_number)
    review = build_finals_round_review(lifecycle, review_repo, identities, round_id)
    review_by_matchup = {matchup.matchup_id: matchup for matchup in review["matchups"]}
    submissions = authoritative_submissions(database, round_)
    player_ids = [slot["season_player_id"] for slots in submissions.values() for slot in slots]
    names = authoritative_player_names(database, player_ids)

    bye = None
    matchups = []
    for pairing in pairings:
        if pairing.slot == "bye":
            bye = {"team_name": _team_name(identities, pairing.home_season_entry_id)}
            continue
        matchup_review = review_by_matchup.get(pairing.matchup_id) if pairing.matchup_id else None
        if matchup_review is None:
            # This pairing exists (both sides are known) but has not been
            # materialised into a real matchup yet -- the week has been
            # opened for lineup submission but this particular slot's
            # `bbbffl_matchup` row (created by `open_finals_week`) is not
            # yet visible through `build_finals_round_review`. Show the
            # same team-names-only preview a not-yet-opened week shows.
            matchups.append(_pairing_preview(pairing, identities))
            continue
        official = lifecycle.effective_result(pairing.matchup_id)
        score_state = match_score_state(matchup_review, round_.state, official)
        matchups.append(
            {
                "slot": pairing.slot,
                "slot_label": SLOT_LABELS.get(pairing.slot, pairing.slot),
                "status": score_state,
                "status_label": MATCH_STATUS_LABELS[score_state],
                "published_at": official.published_at if official is not None else None,
                "home": _side(
                    matchup_review.home,
                    submissions.get(matchup_review.home.season_entry_id),
                    names,
                    official.home_score if official else None,
                    matchup_review.calculation_revision is not None,
                ),
                "away": _side(
                    matchup_review.away,
                    submissions.get(matchup_review.away.season_entry_id),
                    names,
                    official.away_score if official else None,
                    matchup_review.calculation_revision is not None,
                ),
            }
        )
    return bye, matchups


def _finals_preview(database, identities, bracket_id, week_number):
    """Bye + matchup cards for a finals week whose pairing is already
    determined but has not been opened for lineup submission yet -- team
    names only, exactly like `app.public_rounds._build_round_preview`'s
    ordinary fixture-draw preview. Returns ``(None, [])`` when the
    pairing itself is not yet determined (an earlier finals week has not
    finished), never a fabricated placeholder matchup."""
    if bracket_id is None:
        return None, []
    pairings = FinalsBracketRepository(database).list_pairings(bracket_id, week_number=week_number)
    bye = None
    matchups = []
    for pairing in pairings:
        if pairing.slot == "bye":
            bye = {"team_name": _team_name(identities, pairing.home_season_entry_id)}
            continue
        matchups.append(_pairing_preview(pairing, identities))
    return bye, matchups


def finals_round_context(database, round_id):
    """`(bracket_id, week_number, season_id)` for a finals `bbbffl_round_id`,
    or ``None`` if `round_id` does not belong to any finals bracket week --
    the one place both `build_public_finals_round` and the public routing
    layer (`app.routes.public.round_page`'s finals redirect) resolve a
    finals round_id's bracket/week/season, rather than two copies of the
    same join."""
    row = database.execute(
        "SELECT w.bracket_id, w.week_number, b.season_id FROM finals_bracket_week w "
        "JOIN finals_bracket b ON b.bracket_id = w.bracket_id WHERE w.bbbffl_round_id=?",
        (round_id,),
    ).fetchone()
    if row is None:
        return None
    return {"bracket_id": row["bracket_id"], "week_number": row["week_number"], "season_id": row["season_id"]}


def build_public_finals_round(database, lifecycle, review_repo, identities, afl_client, round_id):
    """The public DTO for one finals week, keyed by its `bbbffl_round_id`
    -- the finals analogue of `app.public_rounds.build_public_round`.
    Works whether or not the week has been opened yet (unlike the
    ordinary builder, which only ever runs once a round is opened): an
    unopened week's Week 1 pairing is already determined at bracket
    creation, so it is shown as a preview exactly like an unopened
    ordinary round's fixture draw. Raises ``KeyError`` if `round_id` does
    not belong to any finals bracket week."""
    context = finals_round_context(database, round_id)
    if context is None:
        raise KeyError(round_id)
    bracket_id, week_number, season_id = context["bracket_id"], context["week_number"], context["season_id"]
    round_ = lifecycle.get_round(round_id)
    if round_ is None:
        bye, matchups = _finals_preview(database, identities, bracket_id, week_number)
        round_state = "scheduled"
    else:
        bye, matchups = _finals_matchups(
            database, lifecycle, review_repo, identities, bracket_id, week_number, round_id, round_
        )
        round_state = round_.state
    return {
        "season_id": season_id,
        "stream": "finals",
        "round_id": round_id,
        "bracket_id": bracket_id,
        "week_number": week_number,
        "label": WEEK_LABELS.get(week_number, f"Finals Week {week_number}"),
        "round_state": round_state,
        "published": round_state == "final",
        "bye": bye,
        "matchups": matchups,
        "superscore": build_public_superscore_round(database, afl_client, identities, season_id, week_number),
    }


def build_public_finals_week_by_number(
    database, lifecycle, review_repo, identities, afl_client, season_id, finals_competition_id_, week_number
):
    """The public DTO for finals week `week_number` (1-4) of one season --
    the finals analogue of `app.public_rounds.build_public_round_by_number`.
    A ``None`` `finals_competition_id_`, or a week whose `bbbffl_round` has
    not been created yet (the finals bracket itself does not exist), is a
    plain scheduled placeholder -- never a 404, so the season sequence can
    list Finals Week 1 through Grand Final before the bracket exists."""
    definition = _round_definition(database, finals_competition_id_, week_number) if finals_competition_id_ else None
    if definition is None:
        return {
            "season_id": season_id,
            "stream": "finals",
            "round_id": None,
            "bracket_id": None,
            "week_number": week_number,
            "label": WEEK_LABELS.get(week_number, f"Finals Week {week_number}"),
            "round_state": "scheduled",
            "published": False,
            "bye": None,
            "matchups": [],
            "superscore": build_public_superscore_round(database, afl_client, identities, season_id, week_number),
        }
    return build_public_finals_round(
        database, lifecycle, review_repo, identities, afl_client, definition["bbbffl_round_id"]
    )


def build_public_season_sequence(database, seasons, season_id):
    """The full public season navigation sequence (issue #213): every
    ordinary fixture round 1..N exactly as `app.public_rounds.
    build_public_season_rounds` already lists them, followed by Finals
    Week 1, Finals Week 2, the Preliminary Final and the Grand Final --
    one continuous `round_number` sequence so the existing round
    selector/previous-next navigation (`app.routes.public.round_by_number`)
    traverses Round 20 straight into the finals phase without a second,
    parallel navigation concept. Finals slots reuse the identical
    "opened" policy `app.public_rounds._round_summary` already applies to
    ordinary rounds: `round_id` stays `None` until that week's own round
    has actually been opened, so `_select_default_round_number` never
    treats a merely-defined future finals week as already in progress."""
    season = seasons.get_season(season_id)
    if season is None:
        raise KeyError(season_id)
    ordinary_id = _ordinary_competition_id(database, season_id)
    if ordinary_id is None:
        raise KeyError(season_id)
    total_ordinary = season.regular_season_round_count
    rounds = []
    for number in range(1, total_ordinary + 1):
        summary = _round_summary(number, _round_definition(database, ordinary_id, number))
        summary["stream"] = "ordinary"
        summary["week_number"] = None
        rounds.append(summary)

    finals_id = finals_competition_id(database, season_id)
    if finals_id is not None:
        for week in range(1, 5):
            definition = _round_definition(database, finals_id, week)
            opened = definition is not None and definition["state"] is not None
            state = definition["state"] if opened else "scheduled"
            rounds.append(
                {
                    "round_number": total_ordinary + week,
                    "label": (definition["label"] if definition else None)
                    or WEEK_LABELS.get(week, f"Finals Week {week}"),
                    "round_id": definition["bbbffl_round_id"] if opened else None,
                    "state": state,
                    "published": state == "final",
                    "stream": "finals",
                    "week_number": week,
                }
            )

    return {
        "season_id": season_id,
        "ordinary_competition_id": ordinary_id,
        "finals_competition_id": finals_id,
        "total_ordinary_rounds": total_ordinary,
        "rounds": rounds,
        "default_round_number": _select_default_round_number(rounds),
    }
