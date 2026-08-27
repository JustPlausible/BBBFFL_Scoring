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

from app.scoring import FORWARD_POSITIONS

Number = int | float


def _is_whole(n: Number) -> bool:
    return float(n).is_integer()


def _fmt(n: Number) -> Number:
    return int(n) if _is_whole(n) else n


def format_football_line(goals: Number, behinds: Number) -> str:
    """The compact "G.B" scorer-sheet notation for a goals/behinds pair.

    Both values are ordinarily whole numbers, so this reads as the familiar
    "4.5" AFL notation. A half-point scorer override (the admin override
    input allows 0.5 steps) can in principle leave a fractional behind --
    still internally consistent (see football_score_for_position below),
    just rendered with an explicit separator instead of risking a second
    decimal point being misread as part of the goals/behinds pair.
    """
    if _is_whole(goals) and _is_whole(behinds):
        return f"{int(goals)}.{int(behinds)}"
    return f"{_fmt(goals)} · {_fmt(behinds)}"


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
    if effective_score is None:
        return FootballScore(goals=None, behinds=None, is_actual_afl=False)
    if (
        position in FORWARD_POSITIONS
        and stat_goals is not None
        and stat_behinds is not None
        and 6 * stat_goals + stat_behinds == effective_score
    ):
        return FootballScore(goals=stat_goals, behinds=stat_behinds, is_actual_afl=True)

    goals, behinds = divmod(effective_score, 6)
    return FootballScore(goals=_fmt(goals), behinds=_fmt(behinds), is_actual_afl=False)
