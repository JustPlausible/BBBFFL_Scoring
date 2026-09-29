"""Pure, dependency-free BBBFFL score display formatting (issue #261).

Extracted from `app.presentation` (the Grand Final/SuperScore prototype's
own football-score presentation, which keeps importing these exact
functions unchanged) so the season model's public read models
(`app.public_rounds`, `app.public_finals`) can reuse the identical
formatting rules without depending on the Grand Final/SuperScore vertical
-- see `tests/test_architecture.py`'s `GRAND_FINAL_VERTICAL` group and this
repository's "two application surfaces" boundary (root `CLAUDE.md`). This
module never recomputes or overrides an official BBBFFL score; it only
formats an already-computed numeric total for display.

Rules (unchanged from `app.presentation`'s original docstring):

- A whole-number total displays without a trailing ".0" (`format_number`).
- Two whole numbers display as the traditional "G.B" scorer-sheet notation
  (`format_football_line`); a fractional value (a half-point scorer
  override) uses an explicit " . " separator instead of risking a second
  decimal point being misread as part of the pair.
- A point total with no literal AFL goals/behinds line available converts
  via divmod(total, 6) -- the traditional "4 goals worth 24 is really 4.0"
  reading of a point total (`football_score_from_total`).
"""

Number = int | float


def is_whole(n: Number) -> bool:
    return float(n).is_integer()


def format_number(n: Number) -> Number:
    """`n` unchanged if genuinely fractional, else as a plain `int` -- so an
    integral score never displays a trailing ".0" (issue #261)."""
    return int(n) if is_whole(n) else n


def format_football_line(goals: Number, behinds: Number) -> str:
    """The compact "G.B" scorer-sheet notation for a goals/behinds pair."""
    if is_whole(goals) and is_whole(behinds):
        return f"{int(goals)}.{int(behinds)}"
    return f"{format_number(goals)} · {format_number(behinds)}"


def football_score_from_total(total: Number | None) -> tuple[Number, Number] | None:
    """The goals/behinds pair for a point total with no literal AFL stat
    line of its own -- `divmod(total, 6)`. Returns ``None`` when `total`
    itself is unknown, so a caller never has to special-case that
    separately from the arithmetic."""
    if total is None:
        return None
    goals, behinds = divmod(total, 6)
    return format_number(goals), format_number(behinds)
