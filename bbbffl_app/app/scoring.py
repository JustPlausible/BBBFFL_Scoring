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

    goals: int | None = 0
    behinds: int | None = 0
    disposals: int | None = 0
    marks: int | None = 0
    hitouts: int | None = 0
    tackles: int | None = 0


@dataclass(frozen=True)
class ScoringRules:
    """Version-resolved coefficients consumed by the one scoring core."""

    forward_goal: int = 6
    forward_behind: int = 1
    midfield_disposal: int = 1
    ruck_mark: int = 1
    ruck_hitout: int = 1
    tackler_tackle: int = 6

    @classmethod
    def from_dict(cls, value: dict | None) -> "ScoringRules":
        return cls(**(value or {}))


def score_position(position: str, stats: PlayerStats, rules: ScoringRules | None = None) -> float | None:
    """Compute the BBBFFL score for a starting position given a player's stats.

    `position` must be one of SCORABLE_POSITIONS. "Interchange" is deliberately
    not accepted here -- resolve it to the position it is replacing first.
    """
    rules = rules or ScoringRules()
    if position in FORWARD_POSITIONS:
        if stats.goals is None or stats.behinds is None:
            return None
        return rules.forward_goal * stats.goals + rules.forward_behind * stats.behinds
    if position in MIDFIELD_POSITIONS:
        if stats.disposals is None:
            return None
        return rules.midfield_disposal * stats.disposals
    if position == "Ruck":
        if stats.marks is None or stats.hitouts is None:
            return None
        return rules.ruck_mark * stats.marks + rules.ruck_hitout * stats.hitouts
    if position == "Tackler":
        if stats.tackles is None:
            return None
        return rules.tackler_tackle * stats.tackles
    raise ValueError(
        f"'{position}' cannot be scored directly. Interchange must be scored as the starting position it replaces."
    )
