"""Allow-listed spectator read models for persisted ordinary rounds.

This module deliberately composes the existing calculation/review, official
result lifecycle, identity, and ladder boundaries.  It contains no scoring or
ladder rules and never serializes their internal/audit objects wholesale.
"""

from decimal import Decimal

from app.lineups import POSITIONS
from app.player_pool import PlayerPoolRepository
from app.round_review import build_round_review


def _number(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def authoritative_player_names(database, season_player_ids):
    """Resolve display names for players in an authoritative submission --
    built on `PlayerPoolRepository.labels_by_id` (issue #151's shared
    player-label read model, also used by `app.round_review` and the
    Scorer lineup-adjudication route) rather than a second, independent
    query, so this stays the single place season-player display names are
    resolved."""
    labels = PlayerPoolRepository(database).labels_by_id(season_player_ids)
    return {season_player_id: label.display_name for season_player_id, label in labels.items()}


def _deferred_source(scoring_source, source_afl_round_id):
    if scoring_source != "opening_round_deferred":
        return None
    return {
        "kind": "opening_round_deferred",
        "label": "Score carried from the deferred Opening Round match",
        "afl_round_id": source_afl_round_id,
    }


def _slot(selection, calculated, names, interchange):
    if calculated is None:
        return {
            "position": selection["position"],
            "player_name": names.get(selection["season_player_id"]),
            "participation": None,
            "effective_score": None,
            "outcome": "awaiting_score" if selection["season_player_id"] else "vacant",
            "confirmed_dnp": False,
            "deferred_source": None,
        }
    if calculated.interchange_applied:
        outcome = "replaced_by_interchange"
    elif calculated.dnp_ruling:
        outcome = "confirmed_dnp_zero"
    else:
        outcome = "scored" if calculated.season_player_id else "vacant_zero"
    effective_deferred = (
        _deferred_source(interchange.scoring_source, interchange.source_afl_round_id)
        if calculated.interchange_applied
        else _deferred_source(calculated.scoring_source, calculated.source_afl_round_id)
    )
    return {
        "position": calculated.slot,
        "player_name": names.get(calculated.season_player_id),
        "participation": "deferred_source" if effective_deferred else calculated.participation_state,
        "effective_score": calculated.effective_score,
        "outcome": outcome,
        "confirmed_dnp": calculated.dnp_ruling is True,
        "deferred_source": effective_deferred,
    }


def _side(side, submitted, names, official_score, has_calculation):
    interchange = side.interchange
    calculated_by_position = {slot.slot: slot for slot in side.slots}
    interchange_selection = next((slot for slot in submitted or [] if slot["position"] == "Interchange"), None)
    return {
        "team": {"name": side.team_name or "Team"},
        "lineup": {
            "submission_version": submitted[0]["version"],
            "players": [
                _slot(slot, calculated_by_position.get(slot["position"]), names, interchange)
                for slot in submitted
                if slot["position"] != "Interchange"
            ],
            "interchange": {
                "player_name": names.get(interchange_selection["season_player_id"] if interchange_selection else None),
                "confirmed_dnp": interchange.dnp_ruling is True,
                "replaces_position": interchange.target_position,
                "deferred_source": _deferred_source(interchange.scoring_source, interchange.source_afl_round_id),
            },
        }
        if submitted
        else None,
        "calculated_score": side.effective_score if has_calculation else None,
        "official_score": _number(official_score) if official_score is not None else None,
    }


def authoritative_submissions(database, round_):
    """Effective immutable submission slots only; draft rows are never read."""
    rows = database.execute(
        "SELECT w.season_entry_id, w.effective_submission_version AS version, "
        "s.position, s.season_player_id "
        "FROM weekly_lineup w JOIN weekly_lineup_submission_slot s "
        "ON s.lineup_id=w.lineup_id AND s.version=w.effective_submission_version "
        "WHERE w.season_id=? AND w.competition_id=? AND w.bbbffl_round_id=? "
        "ORDER BY w.season_entry_id, s.position",
        (round_.season_id, round_.competition_id, round_.bbbffl_round_id),
    ).fetchall()
    order = {position: index for index, position in enumerate(POSITIONS)}
    result = {}
    for row in rows:
        result.setdefault(row["season_entry_id"], []).append(dict(row))
    for slots in result.values():
        slots.sort(key=lambda slot: order[slot["position"]])
    return result


def build_public_round(database, lifecycle, review_repo, identities, round_id):
    """Return the dedicated public DTO for one persisted ordinary round."""
    round_ = lifecycle.get_round(round_id)
    if round_ is None:
        raise KeyError(round_id)
    review = build_round_review(lifecycle, review_repo, identities, round_id)
    submissions = authoritative_submissions(database, round_)
    player_ids = [slot["season_player_id"] for slots in submissions.values() for slot in slots]
    names = authoritative_player_names(database, player_ids)

    matchups = []
    for matchup in review.matchups:
        official = lifecycle.effective_result(matchup.matchup_id)
        if official is not None:
            score_state = "corrected_official" if official.version > 1 else "official"
        elif matchup.calculation_revision is None:
            score_state = "upcoming"
        elif round_.state == "review":
            score_state = "under_review"
        else:
            score_state = "calculated_live"
        matchups.append(
            {
                "order": matchup.matchup_order,
                "status": score_state,
                "status_label": {
                    "upcoming": "Upcoming — score not available",
                    "calculated_live": "Live calculated — not official",
                    "under_review": "Under review — not official",
                    "official": "Official final",
                    "corrected_official": "Corrected official final",
                }[score_state],
                # UTC on the wire, always -- public templates render this in
                # Australian local time and keep the raw UTC value only for
                # diagnostics (issue #161).
                "published_at": official.published_at if official is not None else None,
                "home": _side(
                    matchup.home,
                    submissions.get(matchup.home.season_entry_id),
                    names,
                    official.home_score if official else None,
                    matchup.calculation_revision is not None,
                ),
                "away": _side(
                    matchup.away,
                    submissions.get(matchup.away.season_entry_id),
                    names,
                    official.away_score if official else None,
                    matchup.calculation_revision is not None,
                ),
            }
        )
    return {
        "season_id": round_.season_id,
        "round_id": round_.bbbffl_round_id,
        "round_number": round_.fixture_round_number,
        "round_state": round_.state,
        "matchups": matchups,
    }


def _team_name(identities, entry_id):
    team = identities.get_public_team(entry_id)
    return team.team_name if team else "Team"


def _ordinary_competition_id(database, season_id):
    row = database.execute(
        "SELECT competition_id FROM competition_stream WHERE season_id=? AND stream_type='ordinary'",
        (season_id,),
    ).fetchone()
    return row["competition_id"] if row else None


def _round_definition(database, competition_id, round_number):
    """The `bbbffl_round`/`bbbffl_round_lifecycle` row for one fixture round
    number, or ``None`` when no round has been administratively defined yet
    for that number -- this is the one allow-listed place the public surface
    distinguishes "not yet opened" from "opened", the same way
    `app.scorer_dashboard.ordinary_rounds_with_lifecycle` does for the
    Scorer/Admin dashboards, without importing that Scorer-only module into
    the public read model."""
    return database.execute(
        "SELECT r.bbbffl_round_id, r.label, l.state "
        "FROM bbbffl_round r LEFT JOIN bbbffl_round_lifecycle l ON l.bbbffl_round_id=r.bbbffl_round_id "
        "WHERE r.competition_id=? AND r.sequence=?",
        (competition_id, round_number),
    ).fetchone()


def _round_summary(round_number, definition):
    # A bare `bbbffl_round` definition with no lifecycle row yet (state is
    # NULL from the LEFT JOIN in `_round_definition`) is not "opened" --
    # its `bbbffl_round_id` must never be exposed as a linkable `round_id`
    # here, or `_select_default_round_number` below would treat a merely
    # pre-defined future round as "in progress" the instant it exists.
    opened = definition is not None and definition["state"] is not None
    state = definition["state"] if opened else "scheduled"
    return {
        "round_number": round_number,
        "label": (definition["label"] if definition else None) or f"Round {round_number}",
        "round_id": definition["bbbffl_round_id"] if opened else None,
        "state": state,
        "published": state == "final",
    }


def _select_default_round_number(rounds):
    """Current-round policy for the season landing page: the earliest
    *opened* round that is not yet final (a round actually in progress)
    wins; otherwise the most recently *published* round; otherwise (nothing
    has ever been opened yet) the season's first fixture round.

    Deliberately does not treat an unopened future round number (no
    ``round_id`` -- see ``_round_summary``) as "current" merely because it
    is technically not final: `app.scorer_dashboard.select_current_round`
    avoids the same trap by only ever considering rounds an operator has
    opened; this mirrors that policy while still covering every fixture
    round number the season defines, opened or not, for navigation."""
    if not rounds:
        return None
    in_progress = next((row for row in rounds if row["round_id"] is not None and row["state"] != "final"), None)
    if in_progress is not None:
        return in_progress["round_number"]
    published = [row for row in rounds if row["state"] == "final"]
    if published:
        return published[-1]["round_number"]
    return rounds[0]["round_number"]


def build_public_season_rounds(database, seasons, season_id):
    """Every fixture round number 1..N for a season's ordinary competition
    -- the allow-listed index behind the public round selector/previous-
    next navigation and the season landing page's default-round choice.
    Round numbers ahead of whatever an operator has opened so far carry no
    `round_id` and state ``"scheduled"`` rather than being omitted."""
    season = seasons.get_season(season_id)
    if season is None:
        raise KeyError(season_id)
    competition_id = _ordinary_competition_id(database, season_id)
    if competition_id is None:
        raise KeyError(season_id)
    rounds = [
        _round_summary(number, _round_definition(database, competition_id, number))
        for number in range(1, season.regular_season_round_count + 1)
    ]
    return {
        "season_id": season_id,
        "competition_id": competition_id,
        "rounds": rounds,
        "default_round_number": _select_default_round_number(rounds),
    }


def _build_round_preview(identities, fixtures, season_id, round_number):
    """A scheduled-matchup preview for a fixture round that has not been
    administratively opened yet: team names only, drawn straight from the
    *frozen* fixture draw (`app.fixtures.FixtureRepository`) -- never a
    lineup, a score, or anything implying either is authoritative. Nothing
    is shown while the draw is still a mutable draft: an operator's
    in-progress edits are never published as though they were the
    scheduled fixture. `round_id` is always ``None`` here -- a bare
    `bbbffl_round` definition with no lifecycle row yet is still unopened,
    and its id must never be exposed as a linkable `round_id` (it would
    404 against the detailed view, see `_round_summary`)."""
    draw = fixtures.get_draw(season_id)
    matchups = fixtures.list_matchups(season_id, round_number) if draw is not None and draw.state == "frozen" else []
    return {
        "season_id": season_id,
        "round_id": None,
        "round_number": round_number,
        "round_state": "scheduled",
        "matchups": [
            {
                "order": matchup.matchup_order,
                "status": "scheduled",
                "status_label": "Scheduled — not yet started; teams, lineups and scores are not authoritative.",
                "home": {"team": {"name": _team_name(identities, matchup.home_season_entry_id)}},
                "away": {"team": {"name": _team_name(identities, matchup.away_season_entry_id)}},
            }
            for matchup in matchups
        ],
    }


def build_public_round_by_number(
    database, lifecycle, review_repo, identities, fixtures, season_id, competition_id, round_number, total_rounds
):
    """The public round-browser DTO for one fixture round number: the full
    `build_public_round` result (scores, statuses, lineups) once the round
    has been opened, or a scheduled-only preview beforehand -- either way
    wrapped with the same navigation/labelling fields so a template can
    render both uniformly."""
    definition = _round_definition(database, competition_id, round_number)
    if definition is not None and definition["state"] is not None:
        result = build_public_round(database, lifecycle, review_repo, identities, definition["bbbffl_round_id"])
    else:
        result = _build_round_preview(identities, fixtures, season_id, round_number)
    result["label"] = (definition["label"] if definition else None) or f"Round {round_number}"
    result["published"] = result["round_state"] == "final"
    result["prev_round_number"] = round_number - 1 if round_number > 1 else None
    result["next_round_number"] = round_number + 1 if round_number < total_rounds else None
    return result


def latest_ordinary_season_id(database, seasons):
    """The most recent (by year) season that has an ordinary competition at
    all -- lets `/` land on a season's public landing page as soon as its
    ordinary competition exists, rather than only once its first round has
    been opened."""
    for season in seasons.list_seasons():
        if _ordinary_competition_id(database, season.season_id) is not None:
            return season.season_id
    return None


def build_public_ladder(ladder_repository, identities, competition_id, through_round):
    """Allow-list the authoritative #59 snapshot without reordering it."""
    snapshot = ladder_repository.snapshot(competition_id, through_round)
    rows = []
    for row in snapshot.rows:
        team = identities.get_public_team(row.season_entry_id)
        rows.append(
            {
                "rank": row.rank,
                "team_name": team.team_name if team else "Team",
                "played": row.played,
                "wins": row.wins,
                "draws": row.draws,
                "losses": row.losses,
                "points_for": _number(row.points_for),
                "points_against": _number(row.points_against),
                "percentage": _number(row.percentage),
                "competition_points": row.competition_points,
                "tied": row.tied,
            }
        )
    return {"season_id": snapshot.season_id, "through_round": snapshot.through_round, "rows": rows}
