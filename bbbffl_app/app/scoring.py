"""Canonical BBBFFL scoring engine.

Rules recovered from the legacy Google Apps Script implementation
(`legacy/gas/BBBFFL_Results/fetchBBBFFLResults.js::calculateFantasyPoints` and
`legacy/gas/BBBFFL_Results/generateLiveBBBFFLMatches.js::getPositionScore`, both
confirmed identical on the `audit/google-apps-script-2026` branch and in
`docs/reviews/2025-system-forensic-review.md` section 8). These are the
league's established rules and are not being changed for this prototype.

Interchange is never scored as "Interchange" itself -- the legacy code
explicitly refuses to do so. Callers must resolve which starting position
an interchange player is replacing and pass that position in here instead.
"""

from dataclasses import dataclass

FORWARD_POSITIONS = ("Forward1", "Forward2", "Forward3")
MIDFIELD_POSITIONS = ("Midfield1", "Midfield2", "Midfield3")
SCORABLE_POSITIONS = FORWARD_POSITIONS + MIDFIELD_POSITIONS + ("Ruck", "Tackler")

# All nine roster slots, in BBBFFL position order.
ROSTER_SLOTS = SCORABLE_POSITIONS + ("Interchange",)


@dataclass(frozen=True)
class PlayerStats:
    """A single player's raw AFL statistics for one match, as sourced from afl-api."""

    goals: int = 0
    behinds: int = 0
    disposals: int = 0
    marks: int = 0
    hitouts: int = 0
    tackles: int = 0


def score_position(position: str, stats: PlayerStats) -> float:
    """Compute the BBBFFL score for a starting position given a player's stats.

    `position` must be one of SCORABLE_POSITIONS. "Interchange" is deliberately
    not accepted here -- resolve it to the position it is replacing first.
    """
    if position in FORWARD_POSITIONS:
        return 6 * stats.goals + stats.behinds
    if position in MIDFIELD_POSITIONS:
        return stats.disposals
    if position == "Ruck":
        return stats.marks + stats.hitouts
    if position == "Tackler":
        return 6 * stats.tackles
    raise ValueError(
        f"'{position}' cannot be scored directly. Interchange must be scored "
        "as the starting position it replaces."
    )
