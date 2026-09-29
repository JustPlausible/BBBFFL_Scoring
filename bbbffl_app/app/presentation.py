"""Display-only conversion of BBBFFL effective scores into the traditional
football "Goals.Behinds (Total)" scorer-sheet format.

This module never feeds back into scoring, lifecycle, persistence, or
ranking -- it only interprets an already-computed effective_score (see
scoring.py / service.py) for presentation. Shared by the Grand Final and
SuperScore public pages and by both Admin screens, so the G/B conversion
rules live in exactly one place.

Rules (see the task brief this implements):

- A Forward position (Forward1/2/3) shows the player's literal AFL goals
  and behinds whenever those are known *and* still add up to the displayed
  effective total (6*G + B == effective_score). That covers the normal
  case (no override) and an override that merely corrects the total to
  match the player's real goals/behinds (e.g. a late stats correction).
- Midfield/Ruck/Tackler positions have no literal AFL goals/behinds of
  their own -- their BBBFFL point total is always converted via
  divmod(total, 6), the traditional "4 goals worth 24 is really 4.0"
  reading of a point total.
- A Forward override that leaves the actual AFL goals/behinds inconsistent
  with the new effective total (or a Forward with no AFL stat line at all)
  falls back to the same divmod conversion, so it is never possible for an
  official row to show G*6 + B != the displayed effective total.
"""

from dataclasses import dataclass

from app.score_presentation import Number, football_score_from_evidence, format_football_line
from app.scoring import FORWARD_POSITIONS

__all__ = ["FootballScore", "Number", "football_score_for_position", "format_football_line"]


@dataclass(frozen=True)
class FootballScore:
    goals: Number | None
    behinds: Number | None
    # True only when goals/behinds are the player's literal AFL statistics
    # for a Forward position; False for every Midfield/Ruck/Tackler
    # conversion, and for a Forward whose override no longer matches their
    # actual AFL goals/behinds (see module docstring).
    is_actual_afl: bool

    @property
    def line(self) -> str:
        if self.goals is None or self.behinds is None:
            return "—"
        return format_football_line(self.goals, self.behinds)


def football_score_for_position(
    position: str,
    effective_score: Number | None,
    stat_goals: int | None = None,
    stat_behinds: int | None = None,
) -> FootballScore:
    """The display Goals/Behinds for one scored position row.

    `effective_score` is the already-computed official score (calculated,
    or scorer-overridden) -- never recomputed here. `stat_goals` /
    `stat_behinds` are the player's real AFL statistics for this match, if
    known; pass None when there is no stat line (e.g. an unnamed/vacant/DNP
    position, or a match that hasn't produced stats yet).
    """
    result = football_score_from_evidence(
        effective_score,
        is_actual_stat_capable=position in FORWARD_POSITIONS,
        stat_goals=stat_goals,
        stat_behinds=stat_behinds,
    )
    if result is None:
        return FootballScore(goals=None, behinds=None, is_actual_afl=False)
    goals, behinds, is_actual_afl = result
    return FootballScore(goals=goals, behinds=behinds, is_actual_afl=is_actual_afl)
