# BBBFFL 2026 Grand Final — coach discussion for 2027

## Purpose

Short discussion guide for the 2026 Grand Final gathering. Most BBBFFL behaviour is already well understood; the items below are the decisions or confirmations that are most useful before the 2027 rebuild proceeds.

This is not intended to turn BBBFFL into a formal rules-heavy competition. The aim is to remove the few ambiguities that software cannot safely guess.

The 2026 scorer workbook has now also been audited, with a light consistency check against the 2021–2025 workbooks. Workbook-derived findings are tracked separately in `2026-workbook-findings.md`; only questions that genuinely need human confirmation are repeated here.

## Must decide or confirm today

### 1. 2027 rule changes

Confirm whether the existing scoring remains unchanged for 2027:

- Forward: goal = 6, behind = 1.
- Midfielder: 1 point per disposal.
- Tackler: 6 points per tackle.
- Ruck: hit-outs + marks.
- No bonuses, fractions or positional eligibility restrictions.

Record any other rule changes agreed for 2027.

### 2. Interchange loophole priority

Confirm the edge case:

- A coach intentionally leaves a position available for an early-game Interchange player.
- Later in the round, a genuinely selected player is ruled DNP in another position.
- The Interchange would score more in the later DNP position.

Does the Interchange remain committed to the intentional loophole position, or move to the highest-scoring available DNP position?

### 3. Ordinary AFL byes

The scorer workbooks explicitly annotate AFL bye rounds and historical record entries include `* denotes bye round`, so BBBFFL clearly continues to play during ordinary AFL bye periods.

Confirm the intended rule wording:

- a player whose AFL club has a scheduled bye is simply unavailable for that BBBFFL round;
- an AFL bye is **not** a DNP and therefore does not activate the Interchange;
- coaches are expected to select around byes using the players available from their squad;
- there are no other ordinary-bye exceptions beyond any separately agreed Opening Round/deferred-stat rules.

This is important to make explicit before software starts interpreting AFL availability states.

### 4. What counts toward all-time records?

The workbook maintains all-time BBBFFL records such as highest team score, positional records, largest winning margin, lowest winning score and highest losing score.

Confirm the scope for the future automatic record book:

- Do normal premiership finals scores count in the same all-time BBBFFL records as home-and-away rounds?
- Do SuperScore performances count toward those same all-time team/positional records, or should SuperScore records be separate?
- Should ordinary bye-affected rounds remain eligible for records, with a note/annotation if useful, as the legacy workbook currently suggests?

### 5. Team submission visibility

Today, WhatsApp means a team becomes visible to everyone as soon as the coach posts it.

Confirm whether the new system should preserve that behaviour:

- selections being edited remain private;
- once the coach presses Submit, the submitted team becomes visible to the league immediately;
- it does not wait for lockout.

### 6. Coach submission method

Ask whether coaches would use a simple BBBFFL mobile/web team-selection page if it provided:

- their owned squad;
- AFL match/start time;
- AFL team-list/selection status where available;
- injury information where available;
- season and recent-form statistics;
- calculated Forward/Midfield/Tackle/Ruck potential scores;
- simple nine-player submission.

WhatsApp can remain supported during transition, with the scorer able to enter a WhatsApp submission into BBBFFL.

### 7. Notifications

Gauge support for BBBFFL sending useful messages to the existing WhatsApp group and/or email, such as:

- lockout approaching and coaches still missing;
- team submitted;
- selected player late withdrawal;
- round ready/results published;
- updated ladder;
- mid-season draft reminders;
- finals/SuperScore results.

Development/replay notifications should go to a separate test WhatsApp group rather than the real league chat.

## Useful discussion if time permits

### Partial submission then no final team

If a coach correctly names one or more early-game players but then completely fails to submit the remainder before main lockout, what exactly carries forward from the previous round into the still-empty positions?

This is a genuine software edge case worth agreeing explicitly.

### Replacement coach and pre-drawn draft order

If next year's draft order has already been drawn and a coach later withdraws, should the replacement normally inherit that draft position, or should the league redraw?

This can remain scorer/league discretion if coaches do not want a hard rule.

### Draft-pick trading

Mid-season draft picks can be traded. Preseason draft-order trading has occurred. Ask whether coaches want to explicitly allow individual live preseason draft picks to be traded, or simply leave this as an exceptional agreement if it ever occurs.

### Public spectator pages

Current proposal:

- public team names;
- live Round Centre and detailed matchups;
- current ladder;
- finals/SuperScore views;
- no email/contact details;
- coach names not required publicly;
- deeper history can remain logged-in or be decided later.

### Team identity

No decision is required for 2027, but ask whether coaches would enjoy choosing team colours/logos for the future Round Centre.

## Already understood — no need to re-litigate unless coaches disagree

- 10 coaches/teams.
- Complete redraft every season; no keepers.
- One AFL player can belong to only one BBBFFL club.
- Preseason snake draft.
- No drafting positional restrictions.
- Weekly side: 3 Forwards, 3 Midfielders, Tackler, Ruck, Interchange.
- Unselected squad players are simply not selected; they are not a bench.
- Any owned player can be used in any fantasy position.
- Early-game players must be named before their AFL match starts and then remain locked in that position.
- Main lockout freezes the remaining lineup.
- Failure to submit defaults to the previous relevant lineup, with Round 1/SS1 exceptions handled through scorer-confirmed fallback rules.
- DNP is confirmed by the scorer and is not inferred merely from zero statistics.
- Interchange uses the scoring formula of the position it fills.
- With multiple genuine DNP positions, Interchange normally goes to the highest-scoring available position, subject to scorer confirmation.
- Five regular-season head-to-head matches per round.
- Win 4, draw 2, loss 0; ladder ordering uses premiership points, percentage, then Points For.
- Tied finals are won for progression purposes by the team that finished higher on the home-and-away ladder, subject to scorer confirmation.
- Top-five, four-week finals structure.
- Four independent SuperScore rounds run during finals for all 10 coaches.
- Mid-season draft uses reverse post-Round-9 ladder order and does not snake.
- No normal player movement outside preseason and mid-season windows.
- Scorer remains the final fantasy authority; software should recommend rather than overrule.
- The 2026 workbook confirms the exact legacy nine-round fixture-number rotation; this does not need to be debated unless coaches want to change the draw system.

## Product idea to show interested coaches

The intended full-season BBBFFL experience is broader than today's spreadsheet:

1. Coach logs in with a simple account/magic link.
2. Home screen shows opponent, ladder context, lockouts and submission status.
3. Coach selects nine players from their owned squad with useful AFL statistics alongside them.
4. Submitted teams become visible to the league.
5. Everyone follows all five fantasy matches live through a Round Centre.
6. Scorer receives DNP/Interchange recommendations and reviews exceptions.
7. Scorer publishes the official round, updating results and ladder.
8. Finals and SuperScore use the same scoring engine.
9. Historical draft/results/record information can grow around the system over time.

Before the 2027 season, the plan is to replay the real 2026 season through the rebuilt application using the real draft squads, weekly submissions and historical AFL data. This will exercise the full season from draft through Grand Final before the system is trusted live.