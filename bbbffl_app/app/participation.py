"""Advisory AFL participation evidence and DNP review states.

Only facts exposed by the public ``afl-api`` v1 contract are interpreted here.
In particular, a missing player-stat row is not proof that a player did not
participate: the current contract has no independent named-team/participation
field, so that case deliberately remains unknown.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.afl_client import Match, PlayerStatLine


class ParticipationState(StrEnum):
    PLAYED_WITH_STATS = "played_with_stats"
    PARTICIPATED_ZERO_STATS = "participated_zero_stats"
    CLUB_BYE = "club_bye"
    UNKNOWN = "unknown"


class DnpRecommendationState(StrEnum):
    NOT_DNP = "not_dnp"
    REVIEW_REQUIRED = "review_required"
    RECOMMEND_DNP = "recommend_dnp"


@dataclass(frozen=True)
class ParticipationEvidence:
    state: ParticipationState
    dnp_recommendation: DnpRecommendationState
    reason: str
    source: str = "afl-api-v1"
    afl_match_id: int | None = None


def assess_participation(
    *,
    afl_team_id: int | None,
    bye_team_ids: frozenset[int] | None,
    match: Match | None,
    stat_line: PlayerStatLine | None,
) -> ParticipationEvidence:
    """Classify public-contract evidence without inventing a DNP fact."""
    if afl_team_id is not None and bye_team_ids is not None and afl_team_id in bye_team_ids:
        return ParticipationEvidence(
            ParticipationState.CLUB_BYE,
            DnpRecommendationState.NOT_DNP,
            "Player's AFL club has an ordinary scheduled bye; league rules say this is unavailable, not DNP.",
        )
    if stat_line is not None:
        values = (
            stat_line.goals,
            stat_line.behinds,
            stat_line.disposals,
            stat_line.marks,
            stat_line.hitouts,
            stat_line.tackles,
        )
        if all(value == 0 for value in values):
            return ParticipationEvidence(
                ParticipationState.PARTICIPATED_ZERO_STATS,
                DnpRecommendationState.NOT_DNP,
                "afl-api returned a player stat row whose reported statistics are all zero.",
                afl_match_id=match.match_id if match else None,
            )
        return ParticipationEvidence(
            ParticipationState.PLAYED_WITH_STATS,
            DnpRecommendationState.NOT_DNP,
            "afl-api returned a player stat row with one or more non-zero (or incomplete) statistics.",
            afl_match_id=match.match_id if match else None,
        )
    if match is None:
        reason = "No AFL match was returned for the player's club and bye information is missing or does not identify the club."
    else:
        reason = "No player stat row was returned; afl-api v1 exposes no independent participation/named-team fact, so DNP cannot be inferred."
    return ParticipationEvidence(
        ParticipationState.UNKNOWN,
        DnpRecommendationState.REVIEW_REQUIRED,
        reason,
        afl_match_id=match.match_id if match else None,
    )
