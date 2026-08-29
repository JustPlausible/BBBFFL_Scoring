# DNP evidence and Interchange recommendations

Issue #57 adds an advisory layer around the existing scorer-owned scoring and
decision boundaries. It does not add a second scoring engine and does not write
decisions while calculating recommendations.

## Public afl-api evidence

`app.participation` classifies only public `afl-api` v1 facts:

| State | Meaning | DNP advice |
|---|---|---|
| `played_with_stats` | A player row contains a non-zero or incomplete stat line. | Not DNP. |
| `participated_zero_stats` | A player row exists and every reported statistic is zero. | Not DNP. |
| `club_bye` | The round's `byes` collection identifies the player's AFL club. | Unavailable, explicitly not DNP. |
| `unknown` | The match, bye information, or player row is insufficient. | Scorer review required; no DNP inference. |

The current public contract has no independent selected-team, substitute, or
participation feed. Consequently, absence from `/matches/{id}/player-stats`
cannot distinguish a genuine DNP from incomplete evidence. The domain includes
`recommend_dnp` for future authoritative public evidence, but current v1 facts
never manufacture that recommendation. Adding named-player evidence is a
potential upstream `AFL-api` follow-up; BBBFFL must not bypass the contract.

## Scorer authority and provenance

Evidence and `dnp_recommendation` are recalculated observations. An official
ruling is a separate persisted `slot_dnp` row: `true` confirms DNP, `false`
records an explicit rejection, and no row means unresolved scorer input.
Confirmation/rejection accepts a reason and uses the existing actor, immutable
audit-event, competition-scope, and finalisation-lock boundaries. Later evidence
does not update or delete any ruling or audit event.

## Interchange candidates

Candidates are generated only for scorer-confirmed DNP positions and deliberate
unnamed vacancies. For each target, the Interchange stat line is passed to the
canonical `score_position()` formula for that target. The candidate exposes its
replacement score and resulting team score. One clear maximum is `clear_best`;
all equal maxima are returned as `equal_best`; no vacancy is
`no_eligible_replacement`; and legal vacancies without enough Interchange
evidence are `awaiting_evidence`.

Candidate generation is advisory only. The official result continues to read
only the persisted Interchange assignment, and the coach's original selected
player remains alongside the effective replacement. A scorer must persist a
target (with audit reason/provenance) before it contributes.

## Replay coverage

`tests/test_interchange_recommendation.py` uses clearly labelled synthetic,
2026-shaped fixtures because no authoritative historical scorer rulings are
stored in this repository. Repeating the calculation over identical AFL facts
and persisted decisions produces the same result and does not rewrite the
decision.
