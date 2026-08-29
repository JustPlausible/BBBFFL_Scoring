"""Allow-listed spectator read models for persisted ordinary rounds.

This module deliberately composes the existing calculation/review, official
result lifecycle, identity, and ladder boundaries.  It contains no scoring or
ladder rules and never serializes their internal/audit objects wholesale.
"""

from decimal import Decimal

from app.lineups import POSITIONS
from app.round_review import build_round_review


def _number(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def _player_names(database, season_player_ids):
    ids = sorted({item for item in season_player_ids if item})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = database.execute(
        f"SELECT season_player_id, display_name FROM season_player_pool WHERE season_player_id IN ({placeholders})",
        tuple(ids),
    ).fetchall()
    return {row["season_player_id"]: row["display_name"] for row in rows}


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


def _effective_submissions(database, round_):
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
    submissions = _effective_submissions(database, round_)
    player_ids = [slot["season_player_id"] for slots in submissions.values() for slot in slots]
    names = _player_names(database, player_ids)

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
