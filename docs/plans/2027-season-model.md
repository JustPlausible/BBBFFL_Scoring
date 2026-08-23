# BBBFFL 2027 season model and product requirements

## Purpose

This document captures the human-led operating model of the Big Bad Barry Fantasy Football League (BBBFFL) as understood after the 2026 Grand Final prototype and requirements interview.

It is intended to be the primary domain reference for rebuilding `BBBFFL_Scoring` for a complete season. AI coding tools should treat this document as a description of league behaviour and product intent, not as permission to invent additional fantasy rules.

The existing 2027 Grand Final prototype remains useful implementation evidence, but the prototype does not define the whole BBBFFL domain.

## Core architecture boundary

BBBFFL is a consumer of the separate `afl-api` service.

- `afl-api` owns factual AFL data, canonical player identity, fixtures, team lists/statuses, injuries where available, match states and player statistics.
- BBBFFL owns fantasy player ownership, drafts, weekly selections, lockouts, scoring formulas, DNP rulings, interchange behaviour, fixtures, ladders, finals, SuperScore, administration and historical fantasy records.
- BBBFFL must consume the versioned consumer API and must not depend on Champion Data/CFS implementation details.
- Consumer-facing fun or league terminology belongs in BBBFFL. Upstream AFL facts should remain source-agnostic.
- The long-term design should allow changes in AFL data collection to be isolated to `afl-api` wherever possible.

## Rules versus conventions

The implementation must distinguish:

1. **Hard league rules/invariants** — deterministic rules the software should enforce.
2. **Season configuration** — values that can change by annual league decision.
3. **Recommendations requiring scorer confirmation** — decisions that can usually be calculated but remain subject to scorer authority.
4. **League convention/scorer discretion** — social or exceptional decisions that should not be unnecessarily hard-coded.

BBBFFL is a small social competition. The software should support the league rather than turn every historical convention into an inflexible rule.

## Competition identity

- BBBFFL history dates to 2004.
- The competition has historically contained 10 coaches/teams.
- The persistent historical identity is the **coach**, not the team name.
- A coach may change team name between seasons while retaining their coaching history.
- A departing coach may be replaced by a new coach, who begins their own history even if they temporarily reuse a previous team name.
- Conceptually, each season has 10 competition licences occupied by coaches for that season.
- Public spectator views primarily identify entries by team name; coach identity does not need to be public.

A useful conceptual relationship is:

`Coach -> Season Entry -> Team Name -> Squad -> Weekly Selections -> Results`

## Annual season lifecycle

A BBBFFL season aligns to an AFL season/calendar year.

The normal lifecycle is:

1. Previous BBBFFL Grand Final concludes.
2. Next season's draft order is randomly drawn.
3. Coaches hold the annual rules/constitution discussion and vote on proposed changes.
4. Returning participants are confirmed/replacements invited if necessary.
5. Preseason AFL player pool becomes available through `afl-api`.
6. Live preseason snake draft is held, normally 1–2 weeks before the AFL season.
7. Preseason player trades may occur after the draft and before the first official AFL match.
8. Opening squads become final at the first official AFL home-and-away match.
9. BBBFFL home-and-away rounds are played.
10. Mid-season delisting/draft period occurs, normally after the first nine-round round robin.
11. Remaining home-and-away rounds are played.
12. Top five qualify for four weeks of BBBFFL finals.
13. SuperScore runs concurrently across all four finals weeks and keeps all 10 coaches involved.
14. Grand Final determines the premier; last place receives the wooden spoon.
15. Cycle begins again with the next season's draft-order draw and rules discussion.

## Annual governance and season configuration

Rule changes are normally discussed and agreed on Grand Final day for the following season.

From the 2027 season onward, BBBFFL should retain a versioned record of season rules rather than assuming today's rules apply historically.

Season-level configuration may include:

- entry fee;
- squad size;
- scoring formulas/weights;
- fixture mapping;
- lockout configuration;
- mid-season draft timing;
- finals/SuperScore configuration;
- exceptional AFL-round mappings;
- prize allocations;
- notes describing agreed rule changes.

Historical rule reconstruction before 2027 is optional and should use available evidence rather than invented assumptions.

## Fees and prizes

- Current entry fee is $100 per coach; historically it began at $50 and was later increased by league vote.
- The scorer tracks paid/unpaid status.
- League fees fund monetary Grand Final and SuperScore prizes.
- The wooden spoon, trophy engraving and draft-day incidental costs are not currently league-fee expenses.
- The system only needs lightweight fee administration unless requirements expand: season fee, coach payment status and prize configuration are sufficient.

## Preseason draft

### Draft order

- Next season's draft order is randomly drawn after the previous Grand Final.
- Draft positions may be traded before draft day by league agreement, although this is uncommon.

### Draft format

- The preseason draft is a snake draft.
- Order proceeds `1 -> 10`, then `10 -> 1`, repeating until squads are complete.
- Pick 10 and 11 therefore belong consecutively to the coach at draft position 10; picks 20 and 21 belong consecutively to draft position 1.
- There are no positional drafting requirements.
- A coach may draft any mixture of eligible AFL players.
- One AFL player may belong to only one BBBFFL club at a time.

### Squad size

- Squad size is agreed each year, usually 22–24 players.
- 2026 used 22 players.
- The league may even reduce the planned size during the live draft by agreement if the draft is taking too long.
- Software should therefore permit an authorised scorer/admin to finalise/change the draft target rather than treating it as an immutable compile-time value.

### Attendance and proxies

The draft is normally held live with as many coaches physically present as possible. Remote participation by Teams/Zoom/WhatsApp and proxy selections are accepted by league convention. The scorer may enter selections on behalf of a coach.

### Draft history

Every selection should retain historical draft metadata, including season, overall pick, draft round, coach/team and player identity. Historical draft information is culturally useful on draft night and should eventually support previous-pick/previous-owner displays.

## AFL player identity and provisional players

The normal draft pool should come from the current AFL season player population exposed by `afl-api`.

Rookie status, lack of AFL games, injury or suspension does not inherently make an AFL-listed player ineligible for BBBFFL drafting.

BBBFFL must also support a **provisional player** when a legitimate AFL player is not yet represented by `afl-api` at draft time. An authorised scorer/admin may create the provisional identity and later reconcile it with a canonical `afl-api` player without altering the historical draft transaction.

This also allows legitimate AFL mid-season recruits to be represented if upstream identity arrives late.

## Player ownership and transaction windows

Player ownership is completely reset each season. No players or keeper rights persist into the following season.

Player movement is restricted to two periods:

### Preseason transaction window

After the live draft and before the first official AFL match:

- coaches may trade players;
- trades may involve two or multiple clubs;
- one-for-one and balanced multi-player trades are permitted;
- every completed transaction must leave every participating club at the required squad size;
- the opening squads become final at the first official AFL match.

Draft-pick trading during the live preseason draft is not a commonly established practice and should be treated as league discretion rather than prohibited by an invented rule.

### Mid-season transaction/draft window

Normally after Round 9:

1. The official post-round ladder is established.
2. Mid-season draft priority is frozen in reverse ladder order: 10th to 1st.
3. Normal percentage tiebreaks determine ladder order where premiership points are equal.
4. Coaches publicly delist any number of players before the scorer closes the delisting window.
5. Coaches may trade players and/or mid-season draft selections during the permitted window.
6. The draft occurs asynchronously, normally through WhatsApp/email rather than a live meeting.
7. Unlike preseason, the order does **not** snake. The same 10th-to-1st order repeats.
8. A club is skipped in later passes once it has filled all vacancies.
9. Drafting continues until every club has returned to the required season squad size.

A coach may theoretically delist their entire squad, although normal delisting counts are much smaller.

### Closed periods

Outside the preseason and mid-season transaction windows there is no waiver wire, free agency, injury replacement, delisting or ordinary player trading. Injured, retired or otherwise unavailable players remain on the squad until an allowed transaction window or season end.

## Weekly BBBFFL team

From the season squad, each coach selects exactly nine BBBFFL positions:

- 3 Forwards;
- 3 Midfielders;
- 1 Tackler;
- 1 Ruck;
- 1 Interchange.

All remaining squad players are simply **not selected** for that round. They are not a fantasy bench or reserve list.

Forward 1/2/3 and Midfielder 1/2/3 may exist as internal implementation slots, but the ordering has no sporting significance and should not be presented as though coaches nominate ranked forwards/midfielders.

### Positional eligibility

There are no AFL-position restrictions in BBBFFL.

Any owned player may be named as Forward, Midfielder, Tackler, Ruck or Interchange. Choosing the fantasy role that best exploits a player's AFL statistical profile is part of the coaching strategy.

A player may occupy only one BBBFFL position in a given competition lineup for a round.

## Scoring

Current scoring rules are:

| BBBFFL position | AFL statistics | BBBFFL score |
| --- | --- | --- |
| Forward | Goals, Behinds | `(goals * 6) + behinds` |
| Midfielder | Disposals | `disposals` |
| Tackler | Tackles | `tackles * 6` |
| Ruck | Hit-outs, Marks | `hit_outs + marks` |
| Interchange | Replacement position | Uses the formula of the position ultimately occupied |

Examples:

- Forward with 3 goals 1 behind = 19.
- Midfielder with 27 disposals = 27.
- Tackler with 6 tackles = 36.
- Ruck with 31 hit-outs and 5 marks = 36.

Scores are non-negative integers. There are no bonuses, thresholds or fractional scoring rules.

Scoring must be season-configurable so a future league vote can change a formula without changing historical seasons.

## Lockouts and staged team submission

BBBFFL should not hard-code Thursday and Friday as universal rules. AFL scheduling can contain Thursday double-headers, special midweek matches and late fixture confirmation.

Each BBBFFL round should support scorer-confirmed:

- an **early lockout match set** (zero, one or more AFL matches); and
- a **main lockout trigger match**.

The system may recommend these from the AFL fixture, but BBBFFL/scorer configuration is authoritative for fantasy lockout purposes.

### Early lockout behaviour

A coach only needs to submit players participating in early-lockout AFL matches before those matches begin. Other BBBFFL positions may remain undecided until main lockout.

Once an early AFL match starts:

- any selected player from either participating AFL club is locked in their nominated BBBFFL position;
- the coach cannot move that player to another fantasy position;
- no additional player from those AFL clubs may subsequently be added to that coach's lineup for the round.

### Main lockout

At the configured main lockout trigger, all remaining selections are frozen for coaches.

Authorised scorer/admin intervention remains possible after lockout and is treated as a ruling/override rather than an ordinary coach edit.

## Submission and resubmission

- Coaches may edit and resubmit their own unlocked selections until the applicable lockout.
- Submitted selections become visible to the league when submitted; draft/in-progress edits do not need to be visible.
- The scorer/admin may enter or edit a selection on behalf of a coach, including selections received through WhatsApp, phone or another trusted coach.
- Pre-lockout administrative edits should be auditable.
- Coaches cannot self-edit a locked selection.
- Exceptional post-lockout corrections require scorer/admin authority and should record who changed what, when and why.
- BBBFFL historically permits social/gentleman's-agreement resolutions; the software should record the final ruling rather than attempt to codify every social negotiation.

## Failure to submit

If a coach fails to submit a team by the deadline, the default rule is to carry forward **the same team selection from the previous BBBFFL round**.

The system should not optimise or repair that team for current availability. Players who are injured, omitted or otherwise unavailable remain selected and normal DNP/Interchange rules apply.

The scorer should be able to confirm the carry-forward, and the lineup should retain a visible/auditable source such as `carried forward from Round N` rather than pretending the coach submitted it.

The precise handling of a partially submitted early-lockout team followed by failure to complete the main submission should be retained as an edge case for explicit testing/ruling.

## DNP rulings

DNP is a BBBFFL ruling informed by AFL facts, not inferred solely from zero statistics.

- `afl-api` should provide available team-list, late-withdrawal, participation and match evidence.
- A selected player receiving zero statistics is not by itself proof that they did not play.
- BBBFFL may recommend that a player appears to be DNP.
- The scorer confirms the official fantasy DNP ruling.
- Unusual cases may be discussed socially before the scorer records the decision.

## Interchange and loophole

The Interchange is a specifically selected ninth player. It is not a generic bench containing all unselected squad players.

When a selected player is officially ruled DNP, the Interchange can replace that position and is scored using the formula for the position occupied.

If multiple genuine DNP positions are available, the normal rule is to assign the Interchange to the position that yields the highest BBBFFL score for that Interchange player's AFL statistics. The system should calculate and recommend this outcome; the scorer confirms the ruling.

### Intentional loophole

An early-game Interchange can be used strategically. After observing that player's early AFL performance, the coach may intentionally leave a position vacant at main lockout so the Interchange fills it. Historically the vacancy may be communicated by `?`, an intentionally unavailable squad player, humour/context in WhatsApp, or another understood mechanism.

The software should preserve scorer flexibility rather than require an unnecessarily rigid declaration unless the league later chooses to formalise one.

### Unresolved loophole priority rule

The league should explicitly confirm what happens when:

1. a coach has intentionally created a loophole vacancy for the Interchange; and
2. a later genuine DNP occurs in another position where that Interchange would score even more.

Current practice suggests the intentional vacancy is expected to receive the player, but this should be confirmed rather than hard-coded from assumption.

## Regular-season fixture

After the preseason player draft, a second independent random draw assigns the 10 teams fixture numbers 1–10.

A predetermined fixture formula then creates the season draw.

- Five head-to-head matches occur each BBBFFL round.
- Rounds 1–9 form a complete round robin: every club plays every other club once.
- Rounds 10–18 repeat the same pairings with nominal home/away reversed.
- With a 24-round AFL home-and-away season, BBBFFL Rounds 19–20 begin the cycle again, corresponding to the Round 1 and Round 2 pairing patterns.
- The final four AFL home-and-away rounds are reserved for BBBFFL finals.
- Home/away has no scoring effect but is retained for traditional presentation.

The fixture algorithm should be derived/verified against historical scorer spreadsheets before implementation is considered final.

## Ladder

For each head-to-head match:

- Win = 4 premiership points.
- Draw = 2 premiership points.
- Loss = 0 premiership points.

The ladder retains:

- wins/draws/losses as appropriate;
- premiership points;
- Points For;
- Points Against;
- percentage = `(Points For / Points Against) * 100`;
- points-per-game average for informational/fun presentation.

When premiership points are equal, percentage determines ladder order.

## Finals

The top five teams qualify. With a 24-round AFL season, BBBFFL finals occupy AFL Rounds 21–24.

### Finals Week 1

- 1st: bye.
- Qualifying Final: 2nd vs 3rd.
- Elimination Final: 4th vs 5th.
- Elimination Final loser is eliminated.

### Finals Week 2

- Second Semi-Final: 1st vs Qualifying Final winner.
- First Semi-Final: Qualifying Final loser vs Elimination Final winner.
- First Semi-Final loser is eliminated.
- Second Semi-Final winner advances directly to the Grand Final.

### Finals Week 3

- Preliminary Final: Second Semi-Final loser vs First Semi-Final winner.
- Winner advances to the Grand Final.

### Finals Week 4

- Grand Final: Second Semi-Final winner vs Preliminary Final winner.

The system should automatically derive bracket progression from confirmed results while preserving scorer authority to correct results.

## SuperScore

SuperScore runs across the same four AFL rounds as BBBFFL finals and keeps all 10 coaches engaged, including teams eliminated from or not qualified for the premiership finals.

- Four independent rounds: SS1–SS4.
- All 10 coaches compete together in each round.
- Highest score wins that SuperScore round.
- Equal highest scores result in joint winners.
- Coaches may select only players owned by their club.
- The same nine-position BBBFFL scoring model applies.
- A finalist's SuperScore lineup is independent of their premiership-finals lineup; they may legitimately submit two different teams for the same AFL round.

## Opening Round and unusual AFL scheduling

Recent AFL Opening Round formats have required special BBBFFL treatment. Historically, players participating in Opening Round could be selected and their performance later applied to the BBBFFL round corresponding to that AFL club's subsequent bye.

This should be documented for historical replay but **must not be baked into the normal round model**.

A BBBFFL scoring period should therefore be capable of mapping flexibly to AFL fixture data rather than assuming `BBBFFL Round N == AFL Round N` in every season.

If the AFL returns to a conventional Round 1 in 2027, no Opening Round exception needs to be enabled.

## Roles and permissions

### Coach

- Has an individual login linked to persistent coach identity.
- Manages their own weekly selections before applicable lockouts.
- Cannot self-edit locked selections.
- Can view appropriate competition data, statistics and other submitted teams.

### Scorer

- Operates competition scoring and round review.
- May enter selections received through other trusted channels.
- Confirms DNPs, Interchange assignments, carry-forwards and exceptional rulings.
- Can make authorised post-lockout corrections.
- Publishes official round results.

### Delegated scorer

Scorer authority may be temporarily assigned to another coach for a round or period when the normal scorer is unavailable. This is best represented as delegated/temporary permissions rather than a completely different permanent role.

### System administrator

Manages software/season configuration and may have elevated intervention rights. The administrator and scorer may be the same person but are conceptually different responsibilities.

## Authentication and privacy

- Each coach should have an individual account.
- Authentication should be deliberately low-friction for a trusted 10-person league; email magic-link or equivalent simple sign-in is preferred over unnecessary credential complexity.
- Public spectator views may expose team names, live Round Centre/matchups, finals and current ladder.
- Public views do not need coach names.
- Email addresses, contact information, administrative notes, audit logs and private configuration must not be public.

## Coach-facing product vision

BBBFFL should become the place coaches can select and follow their fantasy teams, not merely a replacement spreadsheet.

### Coach dashboard

A round-aware dashboard should surface useful information such as:

- current BBBFFL round/opponent;
- early lockout match/time;
- main lockout match/time;
- own submission status (`2/9 selected`, submitted, carried forward, etc.);
- opponent submission status, without revealing draft edits;
- ladder context;
- relevant alerts.

### Team selection

The coach should be able to see their whole owned squad and useful `afl-api` information such as:

- AFL club;
- next match/time;
- AFL team-selection/list status where available;
- injury information where available;
- season averages;
- optional rolling form (last 3/5/etc.);
- calculated potential BBBFFL scores by position.

Coaches may select via an appropriate mobile-friendly interface. The existing dropdown model is acceptable evidence but not a mandatory final UX.

### Live Round Centre

The normal regular-season view should show all five head-to-head matchups and live scores. Selecting a matchup opens a detailed view similar to the successful 2026 Grand Final prototype, including individual player scores/states and relevant Interchange/DNP information.

Finals should provide the corresponding bracket/match presentation, while SuperScore should continue to provide an all-team ranked view.

## Potential/best possible score

A useful analytical feature is a coach's retrospective **best possible team score** from their complete owned squad for a round.

The calculation should optimise the normal nine positions using each player at most once and the normal scoring formulas. This is an analytical/coaching metric, not the official result and should remain distinct from submitted score.

## Scorer round-review workflow

Live calculated scores may update throughout the AFL weekend, but official BBBFFL results require scorer review/publication.

A Round Review should summarise all five matches and highlight unresolved items such as:

- suspected DNPs requiring confirmation;
- recommended Interchange assignments;
- carried-forward teams;
- player identity/mapping problems;
- exceptional selections/rulings;
- AFL match completion state.

The scorer resolves/acknowledges outstanding items, confirms the matches and selects **Publish Round Results**.

Publication updates official results/ladder/records and triggers configured league communication.

Publication is not permanently immutable. If authoritative AFL statistics or a recognised scoring/ruling error later requires correction, the scorer can amend and republish the round with an audit trail.

## Auditability

The system should retain enough history to explain significant state changes without turning routine usage into bureaucracy.

Important auditable actions include:

- submissions/resubmissions;
- who entered a team on behalf of another coach;
- carry-forward source;
- post-lockout changes;
- scorer DNP rulings;
- Interchange assignment/override;
- trade transactions;
- draft/delist activity;
- result publication and republication;
- rule/configuration changes.

Exceptional administrative changes should support a free-text reason/comment.

## Communication and notifications

The existing shared WhatsApp group remains the preferred league communication channel. Email is also established because weekly spreadsheets/results have historically been distributed that way.

The future system should support event-driven communication while keeping the database/application authoritative rather than WhatsApp.

Potential events include:

- early/main lockout reminders;
- coaches yet to submit;
- submitted-team publication;
- selected player late-withdrawal warning to affected coach/scorer;
- suspected DNP/Interchange recommendation to scorer;
- round ready for review;
- results/ladder publication;
- mid-season delisting window opening/closing;
- mid-season coach-on-the-clock notification;
- SuperScore results;
- finals advancement.

Shared competition events naturally suit the league channel. Coach-specific risks should preferentially reach the affected coach and scorer via supported direct channel/email.

WhatsApp integration is a technical investigation, not an architectural dependency. Manual/scorer-entered WhatsApp submissions must remain viable during migration.

## Replay/test season requirement

Before the 2027 live season, the rebuilt system should support a complete replay of the real 2026 BBBFFL season using historical AFL data in `afl-api`.

The intended replay is **not** a new fictional 2026 draft. It should use, where recoverable:

- real 2026 draft squads;
- real weekly submissions from WhatsApp/scorer records;
- real lockout patterns;
- real trades and mid-season draft;
- real finals and SuperScore selections/results.

The operator may act as all 10 coaches plus scorer and progressively reproduce the season.

### Replay controls

Replay/test mode should allow controlled progression through meaningful BBBFFL states, for example:

- round opened;
- early-selection window;
- early lockout triggered;
- main lockout triggered;
- selected historical AFL matches made available/processed;
- round completed/reviewed/published;
- mid-season draft window;
- finals progression.

There is no requirement to hide future AFL statistics from the replay operator. The purpose is workflow validation, not competitive secrecy.

### Replay isolation

Replay/test competitions must be permanently distinguishable from official seasons and must not contaminate official:

- ladders;
- records;
- awards;
- historical statistics;
- live season state;
- production notifications.

A replay/test notification sandbox should direct WhatsApp/email only to test destinations unless explicitly overridden.

The same BBBFFL rules engine should be used for live and replay operation wherever possible; replay should replace/control time/event progression, not introduce a second scoring implementation.

### Replay acceptance scenarios

The replay should deliberately exercise edge cases where possible, including:

- early-game selection and lock;
- pre-lockout edit/resubmission;
- rejected coach edit after lockout;
- failure to submit and previous-team carry-forward;
- genuine DNP;
- intentional Interchange loophole;
- multiple DNP positions;
- scorer override with audit reason;
- multi-team trade;
- mid-season delistings and variable draft lengths;
- traded mid-season pick;
- provisional AFL player reconciliation;
- drawn BBBFFL match;
- tied SuperScore;
- finals progression;
- Grand Final and season closure.

AFL-stat delta/goblin simulation is desirable only if the upstream `afl-api` replay/data model supports it and should not block the first BBBFFL replay.

## Historical records

Existing scorer spreadsheets contain BBBFFL ladders/history dating to 2004 and records including examples such as:

- highest/lowest team scores;
- highest combined positional scores;
- highest individual positional scores;
- highest winning margins;
- lowest winning score;
- highest losing score;
- team-v-team records/margins;
- premiership history;
- wooden spoons;
- SuperScore winners.

Historical spreadsheet import is valuable but should not block the 2027 season rebuild. The new domain model should nevertheless be designed so historical seasons can be imported later without architectural replacement.

Where underlying historical results can be imported, records should preferably be derived from those results rather than maintained as disconnected manually edited record values.

## Awards

Established awards/results include:

- BBBFFL premiership and Big Bad Barry Fantasy Football League trophy;
- wooden spoon for last place;
- monetary prize for each of the four SuperScore rounds;
- Grand Final monetary prize structure as configured for the season.

Team colours/logos/branding are optional future cosmetic features and are not required for the first full-season model.

## Design principles for implementation tools

AI coding tools should follow these principles:

1. Do not infer BBBFFL rules from AFL rules or AFL-listed positions.
2. Do not make `afl-api` responsible for BBBFFL interpretations.
3. Do not duplicate upstream AFL authority in BBBFFL.
4. Make annual fantasy rules/configuration versionable.
5. Automate deterministic calculations.
6. Recommend decisions where rules/evidence provide a likely answer.
7. Preserve scorer authority for rulings and exceptional cases.
8. Audit exceptional changes rather than silently rewriting history.
9. Treat mobile usability as important for coach workflows.
10. Keep WhatsApp/manual migration paths viable while direct coach submission matures.
11. Keep live and replay/test data safely isolated.
12. Prefer one shared rules engine for live and replay modes.
13. Avoid hard-coding the current AFL fixture structure where BBBFFL has historically needed exceptions.
14. Preserve historical identity using canonical `afl-api` player IDs, with provisional reconciliation where necessary.

## Known decisions still requiring confirmation

The following should not be silently invented by implementation tools:

1. **Intentional loophole versus later genuine DNP:** if the Interchange was intentionally targeted at a vacant position but a later genuine DNP would produce a higher score elsewhere, which position has priority?
2. **2027 rule changes:** confirm scoring weights/formulas and any other Grand Final-day rule amendments.
3. **2027 squad size:** may remain undecided until or even during the draft.
4. **Replacement coach after draft-order draw:** normally likely to inherit the departed coach's position, but league may choose to redraw; scorer/league discretion remains.
5. **Preseason live draft-pick trading:** uncommon and not presently a fully specified rule.
6. **Partial early submission followed by failure to complete main submission:** clarify whether previous-round selections fill remaining positions or another ruling applies.
7. **Opening Round:** retain historical support but determine 2027 behaviour only once the AFL fixture format is known.
8. **Team visibility:** current intent is immediate league visibility once submitted; confirm coaches are happy to preserve this when direct web submission replaces WhatsApp.
9. **Public spectator scope:** current intent is team names, live results/matchups and current ladder; historical/public expansion can be decided later.
10. **WhatsApp automation:** desired, but feasibility and permitted API workflow require technical investigation.

## Status

This is the first consolidated human-led season model. It should be refined using:

- decisions made at the 2026 Grand Final-day coach discussion;
- the 2026 scorer workbook and older historical workbooks where useful;
- real 2026 WhatsApp selections during replay preparation;
- findings from the full 2026 replay;
- implementation discoveries that expose genuine rule ambiguity.

Changes discovered during implementation should be brought back to this domain document rather than resolved only in code.