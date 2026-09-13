"""Shared fail-closed eligibility rule for SuperScore (issue #192).

Mirrors `app.finals_participation`'s shape deliberately: a small, dependency-
light sibling reused by `app.coach_lineup`/`app.lineup_adjudication`/
`app.lineup_correction` (see `app.finals_participation`'s own docstring/
this file's placement in `COACH_LINEUP` in tests/test_architecture.py) so
none of those callers needs to import the heavier `app.superscore_round`/
`app.superscore_review` modules just to ask "is this entry eligible".

Unlike finals (top-5/bracket-participant only), SuperScore's confirmed rule
is simple: **all ten season entries are always eligible** for every
SuperScore round (docs/2026-finals-superscore-design.md's "SuperScore
design" -- "All ten coaches participate in every SuperScore round, including
the five teams eliminated from or never qualified for the finals"). This
module still enforces it explicitly, at every entry point (ordinary
submission, adjudication, correction) rather than leaving it implicit,
exactly as issue #192 requires -- the check is simply "this entry belongs to
the same season as this SuperScore competition", which is what actually
distinguishes a genuine cross-season mistake from a legitimate request.
"""

from app.finals_participation import resolve_cross_stream_fallback_source, stream_type  # noqa: F401 (re-exported)

__all__ = ["SuperScoreParticipantError", "require_superscore_entry_eligible", "resolve_cross_stream_fallback_source"]


class SuperScoreParticipantError(ValueError):
    pass


def require_superscore_entry_eligible(database, competition_id, season_entry_id) -> None:
    """Require `season_entry_id` to belong to the same season as the
    `superscore`-typed `competition_id`. A no-op for any other stream type --
    ordinary/finals eligibility is enforced by their own adapters."""
    if stream_type(database, competition_id) != "superscore":
        return
    row = database.execute(
        "SELECT 1 FROM season_entry e JOIN competition_stream c ON c.season_id=e.season_id "
        "WHERE c.competition_id=? AND e.season_entry_id=?",
        (competition_id, season_entry_id),
    ).fetchone()
    if row is None:
        raise SuperScoreParticipantError(
            f"season_entry_id {season_entry_id} is not an entry in the season this SuperScore competition belongs to"
        )
