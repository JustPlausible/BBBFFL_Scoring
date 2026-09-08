# BBBFFL mid-season draft planning

## Status and purpose

This document records the agreed planning for the BBBFFL mid-season draft workflow. It is a design and competition-process specification, not a statement that all described functionality is currently implemented.

The immediate use case is continuation of the 2026 historical replay. The design should also provide a sound basis for normal live operation from 2027 onward.

For 2026, the confirmed sequence is:

1. complete BBBFFL Round 10 using the existing pre-draft squads;
2. conduct the mid-season draft process after Round 10 is final;
3. complete post-draft trading before the first Round 11 lockout trigger; and
4. resume normal weekly operation for Round 11.

The 2026 replay may use Scorer/replay-operator controls where coach-facing functionality is not yet required. Live seasons should progressively move routine coach decisions into authenticated coach-facing workflows.

## Core principles

- The season setup must explicitly identify the BBBFFL round after which the mid-season draft occurs.
- The configured required squad size remains authoritative throughout the process. For 2026 this is expected to be 22 players, subject to the season configuration rather than hard-coding.
- The mathematical ladder remains authoritative and must not be silently rewritten to obtain a preferred draft order.
- Draft-order exceptions and genuine unresolved ties may be determined outside the app by the competition and recorded by the Scorer as audited draft-order decisions.
- Corrections to an actually incorrect ladder should instead be made through the existing audited match/result/player-stat correction pathways so the ladder recalculates from corrected facts.
- Private coach planning must remain distinct from public competition declarations.
- Coaches may propose or socially agree trades, but no ownership-changing trade becomes authoritative until an authorised Scorer/Admin records or approves it and the transaction is auditable.
- Delistings remain reversible until the Scorer explicitly locks them.
- The final numbered selection table cannot be generated until delistings are locked and all relevant pending trades have been resolved.
- Where practical, the actual player-selection phase should reuse the pre-season draft machinery rather than create a separate selection engine.

## Expected application workflow

### 1. Configure the mid-season draft during season setup

The Scorer or season administrator records the round after which the mid-season draft occurs, together with the season's required squad size and other relevant competition configuration.

For the 2026 replay, the mid-season draft occurs after Round 10, not after Round 9. Recording this at season setup should allow the application to surface the correct phase transition without relying on operator memory.

### 2. Complete the nominated round normally

The designated round proceeds through normal team submission, lockout, AFL evidence/scoring, review, publication and ladder calculation.

For 2026, Round 10 is a normal round played using the squads that existed before the mid-season draft.

### 3. Surface that the mid-season process is ready

Once the designated BBBFFL round is fully final, the Scorer dashboard should indicate that the competition is ready to begin the mid-season process.

The transition should remain an explicit Scorer action rather than occurring merely because the underlying AFL round appears complete.

### 4. Confirm the ladder snapshot

The application presents the mathematically calculated ladder after the nominated round. The Scorer confirms the ladder snapshot that will form the basis of the draft order.

If the mathematical ladder is wrong because an earlier result or statistic is wrong, the source result/statistic should be corrected through the existing audited correction mechanisms and the ladder allowed to recalculate.

The ladder itself must not be manually rearranged merely to produce a historical or competition-agreed draft order.

### 5. Resolve any draft-order exception or genuine tie

The normal draft-order basis is reverse ladder order: last place selects first, progressing toward first place.

If the normal mathematical ladder criteria do not resolve a genuine tie, or the historical replay requires an exceptional ordering that cannot legitimately be reconstructed from the available data, the competition may determine the required order offline. The Scorer records that manual determination with a mandatory audit explanation before the draft order is published.

The system should preserve the calculated ladder and the resulting draft-order decision separately.

The Scorer should also retain an audited ability to alter team selection ordering where a legitimate competition decision requires it. This flexibility is preferable to requiring emergency code changes during a live draft.

### 6. Establish provisional round-based draft entitlements

Before final delistings are known, draft assets are expressed by team and draft round rather than by final numbered pick.

Examples include:

- Team A's first-round mid-season pick;
- Team B's second-round mid-season pick;
- Team A and Team B agreeing to swap their first-round picks; or
- Team A agreeing to trade Player 101 to Team B for Team B's second-round pick.

These examples describe proposed or socially agreed trades only. Pick ownership or player ownership does not change until an authorised Scorer/Admin records or approves the trade.

A specific overall Pick 1, Pick 2, Pick 3, etc. cannot be reliably assigned yet because the number of selections required by each team remains unknown until delistings are locked.

### 7. Allow private coach planning

For live operation, coaches should be able to maintain private planning against their own squad, potentially throughout the first half of the season.

A player may be privately classified as, for example:

- Keep;
- Potential delist; or
- Potential trade.

These are planning states only. They must not be visible to other coaches on public competition pages and must not themselves alter squad ownership or the draft pool.

This private planning facility is not required for the 2026 replay.

### 8. Open formal delisting and trading

Once the Scorer opens the mid-season process, coaches may formally submit delistings and may negotiate or propose permitted trades.

Trading may include players and round-based draft selections. The intended commissioner/scorer workflow is:

1. coaches agree the trade socially;
2. the trade is proposed or communicated in the app or to the league/Scorer;
3. an authorised Scorer/Admin records or approves it;
4. only then do player or draft-pick ownership changes become authoritative; and
5. the transaction remains auditable.

The application should therefore distinguish pending/proposed trades from approved trades. A pending trade must not affect authoritative squad ownership, draft-pick ownership or the final draft table.

The application should not assume that every approved trade leaves each team's visible player count temporarily equal to the final required squad size.

The relevant end-state invariant is that each team's retained players and approved draft entitlements must allow it to return to the configured required squad size. Conceptually:

`retained players - locked delistings - approved outgoing players + approved incoming players + approved draft selections to be exercised = required squad size`

A player-for-pick trade can therefore create a temporary visible imbalance after approval while still producing a valid final list structure.

### 9. Publish formal delistings progressively

When a coach formally submits a delisting, that delisting becomes visible to the competition. A master delisting view should progressively show the formal submissions from all teams.

Only formal delistings are public. Private Keep, Potential delist and Potential trade classifications remain private.

A coach may submit zero delistings.

### 10. Permit changes and resolve trades before the delisting lock

Published delistings remain reversible until the Scorer locks the phase. A coach may withdraw or amend a delisting, including withdrawing a player because a trade opportunity has arisen.

Trade negotiation and proposal may continue during this period. Proposed trades may be approved or rejected by an authorised Scorer/Admin. Only approved trades affect ownership and downstream squad/draft calculations.

Before the Scorer can lock delistings and generate the final selection table, all pending trades that could affect players, vacancies or draft-pick ownership for the mid-season draft must be resolved. The Scorer may approve them, reject them, or require the coaches to withdraw/correct the proposal.

The Scorer should also be able to act as an audited proxy for a coach who cannot use the application, including submitting or amending delistings or recording an agreed trade on that coach's behalf.

If a coach does not participate and the competition determines offline that it can wait no longer, the Scorer may proceed with that team effectively having zero delistings. The application does not need to automate the competition's quorum decision; it needs to support and audit the resulting operational action.

### 11. Scorer locks delistings

The Scorer explicitly moves the competition to **Delistings Locked**. This is the decisive boundary.

The application should not allow this transition while a relevant trade remains pending approval/rejection.

After this action:

- formal delistings cannot be withdrawn or amended through normal coach actions;
- the final vacancies for each squad are known;
- delisted players can enter the available-player pool;
- all approved trades and ownership of round-based draft selections are known; and
- the application can generate the final selection table.

There should not be an automatic 12- or 24-hour expiry. The league may use such expectations socially, but the Scorer decides when the phase is closed.

Exceptional changes after this boundary require an audited Scorer correction with a reason.

### 12. Generate the final numbered selection table

The draft proceeds in rounds through the reverse-ladder team order, with teams skipped once they no longer require a selection.

For example, if one team has three vacancies and another has one, both may participate in the first pass, but only the team still requiring players participates in later passes.

Approved round-based pick trades are applied to ownership when the table is generated. Only at this point do the abstract assets become concrete overall Pick 1, Pick 2, Pick 3, and so on.

The number of selections ultimately available to a team must reconcile its list to the configured required squad size.

### 13. Build the available-player pool

The mid-season draft pool contains:

- eligible AFL players who were not currently held on a BBBFFL squad; and
- players formally delisted and locked during the mid-season process.

A player selected in the pre-season and subsequently delisted becomes available again. A player still held on another BBBFFL squad remains unavailable.

### 14. Conduct the draft

The selection phase should reuse the pre-season draft selection engine and interaction patterns where practical, including:

- identifying the current pick and team entitled to select;
- presenting eligible available players;
- preventing selection of unavailable players;
- identifying already selected/owned players appropriately;
- updating availability immediately after a selection; and
- advancing to the next valid selection.

The pre-season and mid-season setup processes differ, but once an ordered selection table exists the underlying player-selection mechanism should ideally be shared.

### 15. Complete automatically after the final required selection

When the final required selection is successfully made, the draft can automatically transition to complete. A routine additional Scorer lock should not be necessary.

The application must validate that every team has reconciled to the season's configured required squad size rather than merely assuming that the final pick guarantees this.

### 16. Permit audited post-draft corrections

The Scorer retains an exceptional audited correction capability after completion of the draft. This supports competition decisions such as correcting an agreed erroneous selection without pausing the system for a code change.

The audit record should preserve what originally occurred, what was changed, who performed the change, when it occurred and the supplied reason.

### 17. Continue post-draft trading

Draft completion does not itself freeze the new squads. Coaches may continue to negotiate and propose trades during the remaining break before the next BBBFFL round, but the same Scorer/Admin approval requirement applies before any ownership change becomes authoritative.

For the 2026 example, proposed post-draft trades may be submitted until the first Round 11 lockout trigger. Any trade intended to affect the Round 11 squad must also have been approved before that trigger. A merely pending proposal does not take effect simply because it was lodged before the deadline.

### 18. Resume normal weekly operation

At the first lockout trigger for the following round, squad-changing activity closes and the normal weekly competition lifecycle resumes.

Any still-pending trade proposal remains non-authoritative and cannot alter the locked Round 11 squad unless a later exceptional Scorer correction is made under the established audited correction process.

For 2026, Round 11 onward then proceeds using the approved post-mid-season-draft squads.

## Suggested competition states

The process is best represented as explicit competition states rather than a single large wizard. A provisional state progression is:

`Normal season -> Mid-season draft pending -> Ladder/draft-order confirmation -> Delisting and trading open -> Resolve pending trades -> Delistings locked -> Mid-season draft open -> Draft complete/post-draft trading -> Normal season`

The exact implementation names may differ. `Resolve pending trades` may be implemented as a gating condition rather than a separately persisted state. The important requirement is that permissions and valid actions are clear at each boundary and that unresolved ownership-changing trades cannot silently pass into the final draft table.

## Information and privacy layers

The design should keep three kinds of information separate:

1. **Private coach planning** - Keep, Potential delist, Potential trade and similar preparatory notes.
2. **Official competition declarations** - submitted delistings plus proposed/agreed trades awaiting approval and Scorer/Admin-approved trades.
3. **Competition state** - locked delistings, authoritative approved trade effects, draft-order decisions, final numbered selections, completed picks and resulting squads.

A coach's private planning must never become public merely because the formal delisting window has opened.

## Coach and Scorer responsibilities

### Coach

Subject to the current competition state, a coach may:

- maintain private list-planning information;
- formally submit zero or more delistings;
- amend or withdraw submitted delistings before the lock;
- negotiate, agree socially and propose permitted trades;
- propose trades involving round-based draft selections where permitted;
- make draft selections when entitled; and
- continue proposing permitted post-draft trades until the next round's first lockout trigger.

A coach action alone must not make an ownership-changing trade authoritative.

### Scorer

The Scorer may:

- configure the mid-season draft round as part of season setup;
- confirm that the nominated round is final and commence the process;
- confirm the ladder snapshot;
- record audited draft-order tie-break or exceptional determinations without altering the mathematical ladder;
- act as an audited proxy for a coach where required;
- record, approve or reject proposed trades, with the completed transaction remaining auditable;
- ensure relevant pending trades are resolved before locking delistings and generating the selection table;
- explicitly lock delistings;
- make exceptional audited corrections to draft ordering or selections; and
- allow the normal next-round lockout mechanism to end the post-draft trading period.

## 2026 replay scope

The historical replay should prioritise the minimum functionality needed to reconstruct what actually occurred while exercising the intended lifecycle boundaries.

It does not require all future coach-facing conveniences. In particular, the replay operator/Scorer may perform actions that a live 2027 season should expose directly to authenticated coaches.

The 2026 replay should establish and validate at least:

- Round 10 completion before the draft;
- the post-Round-10 ladder snapshot;
- any necessary audited historical draft-order determination;
- historical trades, if evidence shows they occurred, recorded/approved through the Scorer with audit provenance;
- formal delistings and their lock boundary;
- resolution of any relevant pending trade before that lock;
- generation of the correct selection table;
- the available-player pool including delisted and previously undrafted players;
- the historical mid-season selections;
- resulting valid squads; and
- the transition into Round 11.

## Future live-season enhancement: private preference lists and timed auto-pick

This feature is explicitly out of scope for the 2026 replay but should be retained as a candidate shared drafting capability for 2027 and later.

A coach could maintain a private ordered list of preferred players. Five entries may be a useful interface default, but the underlying design need not impose that specific limit.

When the coach's pick becomes active:

1. the coach receives the normal opportunity to select manually;
2. a predetermined/configured selection period begins;
3. a manual selection immediately resolves the pick normally;
4. if the period expires, the application examines the coach's private preference list in order;
5. already selected or otherwise ineligible players are skipped; and
6. the highest-ranked still-available preference is automatically selected and recorded as an automatic preference-list selection.

The list should update effectively without coach intervention: if earlier preferences are selected by other teams, the next available preference becomes the candidate auto-pick.

The preference list remains private.

The fallback when the timer expires and no preferred player remains available is deliberately unresolved. Options such as a grace period, deferred pick or other competition rule should be decided before implementing timed auto-pick.

This capability should belong to a shared draft engine so that it can potentially serve both pre-season and mid-season drafts.

## Implementation questions that do not block this plan

The following can be decided during implementation without reopening the core competition workflow:

- exact dashboard and page layouts;
- labels and button wording;
- whether private planning is presented as tags, dropdowns or another control;
- whether both coaches must explicitly confirm a proposed trade in-app before Scorer/Admin approval;
- notification and reminder mechanisms;
- visual treatment of withdrawn delistings and audit history;
- exact internal names for competition states; and
- the no-available-preference fallback for any future timed auto-pick feature.

Any implementation that discovers a conflict with the competition behaviours recorded above should surface that conflict explicitly rather than silently choosing a different rule.