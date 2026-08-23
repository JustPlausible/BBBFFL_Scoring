# BBBFFL 2026 Grand Final — coach discussion outcomes for 2027

## Purpose

This document records the outcomes of the 2026 Grand Final-day coach discussion for the planned 2027 BBBFFL rebuild.

The discussion was intentionally practical rather than constitutional. The goal was to confirm the few league behaviours that software cannot safely guess and to test whether coaches supported moving from the legacy spreadsheet/WhatsApp workflow toward a full-season BBBFFL website.

The 2026 scorer workbook has also been audited, with a light consistency check against the 2021–2025 workbooks. Workbook-derived evidence is tracked separately in `2026-workbook-findings.md`.

## Overall product decision

The coaches were strongly supportive of moving forward with a full-season BBBFFL website.

The likely direction is now **web-first**, rather than extending the previous Google Sheets/Google Forms model. Legacy spreadsheet/GAS approaches remain useful historical and migration evidence, but they should move down the development priority list rather than defining the 2027 product architecture.

The coach-facing implementation can be refined later through prototypes and replay testing, but the core expectation is now:

- each coach has a simple authenticated account;
- team selection is performed directly on a mobile-friendly BBBFFL web page;
- live scores/results are followed through the website;
- the scorer retains a powerful administration/review interface;
- WhatsApp becomes primarily a notification/social channel rather than the authoritative team-submission mechanism;
- email remains useful for direct coach communication and result/league notices;
- historical and spectator competition data can largely be public, while personal contact details and write/admin functions remain authenticated.

## Confirmed decisions

### 1. 2027 scoring rules — unchanged

No rule changes were agreed for 2027.

Unless the league explicitly revisits this before the 2027 draft, scoring remains:

- Forward: goal = 6 points, behind = 1 point.
- Midfielder: 1 point per disposal.
- Tackler: 6 points per tackle.
- Ruck: hit-outs + marks.
- Interchange: uses the scoring formula of the position it ultimately fills.
- No bonuses, fractional scores or AFL-position eligibility restrictions.

Annual rules should still be versioned so any later change does not rewrite historical seasons.

### 2. Interchange loophole priority — highest score wins

The edge case discussed was:

- a coach intentionally leaves a position available for an early-game Interchange player;
- later in the round another selected player is ruled DNP;
- the Interchange would score more in the later DNP position.

The confirmed rule is that the **highest-scoring available replacement position takes priority**, regardless of whether an earlier vacancy was intentionally created as a loophole.

The system should calculate the potential scores for all eligible replacement positions and recommend the highest-scoring outcome. The scorer still confirms the final Interchange assignment and retains normal exceptional-ruling authority.

An intentional loophole therefore creates an opportunity for the Interchange to score; it does not permanently reserve that position if a better legitimate replacement opportunity later arises.

### 3. Ordinary AFL byes — unavailable, not DNP

A player whose AFL club has a scheduled bye is simply unavailable for that BBBFFL round.

- An AFL bye is **not** a DNP.
- A bye therefore does **not** activate the Interchange.
- Coaches are expected to select around known AFL byes using their available squad.
- The future team-selection page should clearly warn/show that a player is on a bye, but the current preference is **not** to prevent the coach selecting that player.
- If a coach deliberately or accidentally selects a player on a bye, the consequences remain the coach's responsibility rather than the system silently repairing the lineup.
- Notifications/warnings before lockout may be useful.

Any special Opening Round/deferred-stat rules remain separate from the ordinary AFL-bye rule.

### 4. All-time record scope

Confirmed record rules:

- Normal home-and-away scores count toward BBBFFL all-time records.
- Premiership-finals scores also count toward the same normal BBBFFL all-time records.
- SuperScore performances **do not** count toward the normal BBBFFL record book.
- SuperScore should be capable of maintaining its **own separate records/history**.
- Ordinary bye-affected BBBFFL rounds remain eligible for all-time records.
- Bye-affected record entries should be capable of carrying an annotation/marker, consistent with the legacy workbook's historical asterisk convention.

This distinction is important for automated record calculation: normal competition/finals and SuperScore must retain enough competition-context metadata to derive the correct record sets.

### 5. Team drafts versus submitted teams

The future coach team-naming page should distinguish **Save Draft** from **Submit**.

- A coach may build and save a private working lineup during the week.
- Saved drafts are not visible to other coaches.
- A coach may return to and edit a saved draft while relevant positions remain unlocked.
- Once the coach selects **Submit**, the submitted lineup becomes visible to the league immediately, matching the existing WhatsApp convention.
- The team does not remain hidden until lockout.
- A submitted lineup can still be edited/resubmitted by the coach while the relevant players/positions remain unlocked.

A remaining implementation edge case is what to do when a coach has a saved draft but never presses Submit by main lockout; see **Open edge cases** below.

### 6. Coach submission method — website becomes primary

All coaches were supportive of moving toward direct web-based team submission.

The preferred direction is therefore:

- authenticated coach login;
- mobile-friendly weekly team-selection page;
- coach's owned squad shown automatically;
- useful AFL fixture/team-selection/injury/form information shown where practical;
- direct nine-position submission through BBBFFL;
- no requirement to retain Google Forms/Google Sheets as the primary coach workflow.

WhatsApp team posting does not need to remain the normal submission mechanism.

However, the scorer must retain the ability to enter or modify a lineup on behalf of a coach when a coach cannot use the site and supplies the team through WhatsApp, phone, another coach, email or another accepted channel. Such administrative entry should retain an audit trail.

### 7. Notifications — WhatsApp and email remain useful

Coaches were supportive of automated league notifications.

WhatsApp is a strong candidate for the shared/social notification channel rather than the authoritative database. Email should also be retained in coach profiles for direct or grouped communication.

Useful events include:

- lockout approaching;
- coaches still missing a submission;
- team submitted;
- selected player late withdrawal/availability warning;
- suspected DNP or Interchange action requiring scorer attention;
- round ready for scorer review;
- results and updated ladder published;
- mid-season delisting/draft events;
- finals/SuperScore results and progression.

Replay/development notifications must use test destinations rather than the production league chat/email list.

### 8. Coach identity and team identity are separate

The future model must store persistent **coach/user identity** separately from season-specific **team name**.

This supports:

- coaches changing team names between seasons;
- coaches entering/leaving the competition;
- historical coach records independent of team-name changes;
- authenticated account/contact details remaining attached to the person rather than the display team name.

Coach email/contact information is private. Public views may identify teams primarily by team name.

### 9. Replacement coach after draft-order draw — league discretion

No hard rule is required.

If a coach withdraws after the next season's draft order has already been drawn, the league/scorer may decide whether a replacement inherits that position or whether another arrangement/redraw is appropriate.

The system should support an authorised administrative adjustment and audit it, rather than imposing a fixed automatic rule.

### 10. Draft-pick trading — explicitly supported

Individual draft selections can be traded in both preseason and mid-season drafting contexts if coaches agree.

This should be treated as a commissioner/scorer-approved transaction:

- two or more coaches agree the trade socially;
- the transaction is communicated to the league/scorer;
- an authorised scorer/admin records/approves it;
- ownership/order of the relevant draft selection changes before it is exercised;
- the transaction remains auditable.

The live draft interface must therefore not assume each original draft-order owner permanently owns every pick generated from that position.

### 11. Public spectator scope — broad competition visibility is acceptable

Coaches were comfortable with most competition information being publicly viewable.

Public pages can reasonably include:

- team names;
- live Round Centre and detailed matchups;
- current ladder;
- finals bracket;
- SuperScore views;
- historical results/records/draft history where eventually implemented.

Private/authenticated information includes:

- email/contact details;
- saved draft lineups before submission;
- administrative controls;
- scorer notes/rulings/audit detail where not appropriate for spectators;
- any capability to modify league data.

### 12. Team colours/logos/branding — deferred

No team-identity visual scheme was chosen.

Coaches may be interested in future team colours/logos/branding, but this should be explored through later UI drafts and is not a requirement for the first full-season model.

## Open edge cases

### Partial early submission followed by no final submission

This remains the main unresolved weekly-selection edge case.

Likely intended behaviour is:

- any properly submitted early-game selections remain locked and valid;
- for still-empty positions at main lockout, the previous round's corresponding selections may be carried forward where possible;
- normal player-lock/duplicate/availability rules must still be respected;
- the scorer confirms the resulting fallback lineup.

This was not sufficiently explicit to treat as a hard rule yet. It should be tested in the 2026 replay and, if needed, confirmed with coaches before the 2027 live season.

### Saved draft but never submitted

The new web workflow introduces a related question that did not exist cleanly in WhatsApp:

- if a coach has saved a complete or partial private draft but never presses **Submit**, does that draft count for fallback purposes, or is the coach considered to have failed to submit and therefore receives the normal previous-lineup carry-forward?

Current safe interpretation is that **Save Draft is not Submit**, so the normal failure-to-submit rule should apply unless the league later agrees otherwise. This should be made explicit during replay/usability testing before implementation is locked.

## Already confirmed league fundamentals

- 10 coaches/teams.
- Complete redraft every season; no keepers.
- One AFL player can belong to only one BBBFFL club.
- Preseason snake draft.
- No drafting positional restrictions.
- Weekly side: 3 Forwards, 3 Midfielders, Tackler, Ruck, Interchange.
- Unselected squad players are simply not selected; they are not a bench.
- Any owned player can be used in any fantasy position.
- Early-game players must be named before their AFL match starts and then remain locked in that position.
- Main lockout freezes the remaining lineup for coaches.
- Failure to submit defaults to the previous relevant lineup, with Round 1/SS1 exceptions handled through scorer-confirmed fallback rules.
- DNP is confirmed by the scorer and is not inferred merely from zero statistics.
- Interchange uses the scoring formula of the position it fills.
- With multiple eligible DNP/vacant positions, Interchange goes to the highest-scoring available position, subject to scorer confirmation.
- Five regular-season head-to-head matches per round.
- Win 4, draw 2, loss 0; ladder ordering uses premiership points, percentage, then Points For.
- Tied finals are won for progression purposes by the team that finished higher on the home-and-away ladder, subject to scorer confirmation.
- Top-five, four-week finals structure.
- Four independent SuperScore rounds run during finals for all 10 coaches.
- Mid-season draft uses reverse post-Round-9 ladder order and does not snake.
- No normal player movement outside preseason and mid-season windows.
- Scorer remains the final fantasy authority; software should recommend rather than overrule.
- The 2026 workbook confirms the exact legacy nine-round fixture-number rotation.

## Product direction after the discussion

The intended full-season BBBFFL experience is now reasonably well endorsed by the coaches:

1. Coach signs in using a simple authenticated account.
2. Dashboard shows opponent, ladder context, AFL/BBBFFL lockouts and submission status.
3. Coach can privately save a draft lineup during the week.
4. Coach selects nine players from their owned squad with useful AFL information alongside them.
5. Pressing **Submit** publishes the team to the league.
6. Everyone follows all five fantasy matches live through a Round Centre.
7. Scorer receives DNP/Interchange/fallback recommendations and reviews exceptions.
8. Scorer publishes the official round, updating results, ladder and applicable records.
9. Finals and SuperScore use the same core scoring/ruling engine but retain their separate competition contexts.
10. Historical draft/results/record information can progressively migrate from the legacy workbooks.

The current Grand Final/SuperScore trial interface is considered a successful starting point, not a locked final design.

Before the 2027 season, the plan remains to replay the real 2026 season through the rebuilt application using the real draft squads, weekly submissions and historical AFL data. That replay should be used both as an acceptance test and as the main mechanism for resolving the remaining edge cases before live operation.