"""Allow-listed spectator read models for persisted ordinary rounds.

This module deliberately composes the existing calculation/review, official
result lifecycle, identity, and ladder boundaries.  It contains no scoring or
ladder rules and never serializes their internal/audit objects wholesale.
"""

from decimal import Decimal

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


def _slot(slot, names, interchange):
    replaced_by_interchange = bool(slot.dnp_ruling and interchange.target_position == slot.slot)
    deferred = slot.scoring_source == "opening_round_deferred"
    if replaced_by_interchange:
        outcome = "replaced_by_interchange"
    elif slot.dnp_ruling:
        outcome = "confirmed_dnp_zero"
    else:
        outcome = "scored" if slot.season_player_id else "vacant_zero"
    return {
        "position": slot.slot,
        "player_name": names.get(slot.season_player_id),
        "participation": "deferred_source" if deferred else slot.participation_state,
        "effective_score": slot.effective_score,
        "outcome": outcome,
        "confirmed_dnp": slot.dnp_ruling is True,
        "deferred_source": (
            {
                "kind": "opening_round_deferred",
                "label": "Score carried from the deferred Opening Round match",
                "afl_round_id": slot.source_afl_round_id,
            }
            if deferred
            else None
        ),
    }


def _side(side, names, official_score):
    interchange = side.interchange
    return {
        "team": {"name": side.team_name or "Team"},
        "lineup": {
            "submission_version": side.lineup_version,
            "players": [_slot(slot, names, interchange) for slot in side.slots],
            "interchange": {
                "player_name": names.get(interchange.season_player_id),
                "confirmed_dnp": interchange.dnp_ruling is True,
                "replaces_position": interchange.target_position,
            },
        }
        if side.lineup_version is not None
        else None,
        "calculated_score": side.effective_score if side.lineup_version is not None else None,
        "official_score": _number(official_score) if official_score is not None else None,
    }


def build_public_round(database, lifecycle, review_repo, identities, round_id):
    """Return the dedicated public DTO for one persisted ordinary round."""
    round_ = lifecycle.get_round(round_id)
    if round_ is None:
        raise KeyError(round_id)
    review = build_round_review(lifecycle, review_repo, identities, round_id)
    player_ids = []
    for matchup in review.matchups:
        for side in (matchup.home, matchup.away):
            player_ids.extend(slot.season_player_id for slot in side.slots)
            player_ids.append(side.interchange.season_player_id)
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
                "home": _side(matchup.home, names, official.home_score if official else None),
                "away": _side(matchup.away, names, official.away_score if official else None),
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
