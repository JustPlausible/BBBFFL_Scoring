"""Shared fail-closed participant and carry-forward rules for finals."""


class FinalsParticipantError(ValueError):
    pass


def stream_type(database, competition_id):
    row = database.execute(
        "SELECT stream_type FROM competition_stream WHERE competition_id=?", (competition_id,)
    ).fetchone()
    return row["stream_type"] if row else None


def require_round_participant(database, competition_id, round_id, season_entry_id):
    """Require an entry to occur in the active, materialised pairing.

    A bye is deliberately not participation: seed 1 cannot acquire a phantom
    Week-1 lineup. Ordinary streams are outside this adapter and unchanged.
    """
    if stream_type(database, competition_id) != "finals":
        return
    row = database.execute(
        "SELECT 1 FROM finals_bracket_pairing p JOIN finals_bracket_week w "
        "ON w.bracket_id=p.bracket_id AND w.week_number=p.week_number "
        "WHERE w.bbbffl_round_id=? AND p.status='active' AND p.matchup_id IS NOT NULL "
        "AND (? IN (p.home_season_entry_id,p.away_season_entry_id))",
        (round_id, season_entry_id),
    ).fetchone()
    if row is None:
        raise FinalsParticipantError("entry is not an active participant in this finals round")


def resolve_cross_stream_fallback_source(database, season_id, ordinary_competition_id, season_entry_id):
    """Return the latest genuinely submitted ordinary lineup for an entry."""
    return database.execute(
        "SELECT l.bbbffl_round_id, l.lineup_id, l.effective_submission_version "
        "FROM weekly_lineup l JOIN bbbffl_round r ON r.bbbffl_round_id=l.bbbffl_round_id "
        "WHERE l.season_id=? AND l.competition_id=? AND l.season_entry_id=? "
        "AND l.effective_submission_version IS NOT NULL ORDER BY r.sequence DESC LIMIT 1",
        (season_id, ordinary_competition_id, season_entry_id),
    ).fetchone()
