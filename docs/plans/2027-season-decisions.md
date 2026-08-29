# BBBFFL 2027 — confirmed season decisions

## Status and authority

This document records decisions confirmed by the coaches at the 2026 BBBFFL Grand Final gathering on 23 August 2026.

It is an authoritative addendum to `2027-season-model.md`. Where the main season model still labels one of the topics below as requiring confirmation, this document supersedes that older unresolved wording until the next full consolidation edit.

Detailed discussion context is retained in `2026-grand-final-coach-discussion.md`.

## Confirmed for 2027

### Scoring rules

No 2027 scoring changes were agreed.

- Forward: `(goals * 6) + behinds`.
- Midfielder: disposals.
- Tackler: `tackles * 6`.
- Ruck: hit-outs + marks.
- Interchange: uses the formula of the position ultimately filled.
- No bonuses, fractions or AFL-position eligibility restrictions.

These remain subject only to an explicit later league decision before the 2027 season.

### Interchange priority

If more than one vacant/DNP position is available to the Interchange, the Interchange goes to the position producing the **highest BBBFFL score**.

This remains true when one vacancy was intentionally created through the early-game loophole and a later genuine DNP creates a higher-scoring opportunity elsewhere.

The system should calculate/recommend the highest-scoring assignment and the scorer confirms it.

### Ordinary AFL byes

A player whose AFL club has a scheduled bye is unavailable but is **not DNP**.

- A bye does not activate the Interchange.
- Coaches are responsible for selecting around byes.
- The web UI should clearly warn/show bye status but should not necessarily prohibit selecting the player.
- Warning/notification behaviour is desirable.

Opening Round/deferred-stat exceptions remain separate season-specific rules
-- see [`opening-round-deferred-selection.md`](../opening-round-deferred-selection.md)
(issue #69) for the implemented 2024/2025/2026 capability.

### Record-book scope

Normal BBBFFL all-time records include:

- home-and-away rounds; and
- premiership finals.

SuperScore performances are excluded from the normal record book and should have a separate SuperScore record/history context.

Ordinary bye-affected rounds remain record-eligible, with an annotation/marker available to preserve the legacy `* denotes bye round` context.

### Team drafts and submission visibility

The coach-facing web workflow should distinguish **Save Draft** from **Submit**.

- Saved drafts are private.
- Submitted lineups become visible to the league immediately.
- Submission does not wait for lockout to become visible.
- Coaches may edit/resubmit while applicable selections remain unlocked.

`Save Draft` should not be treated as equivalent to a formal submission unless a future rule explicitly says otherwise.

### Primary team-submission channel

Direct web submission is the preferred 2027 direction.

Google Forms/Google Sheets and WhatsApp team posts do not need to remain primary workflow dependencies.

The scorer must still be able to enter/modify a team on behalf of a coach when the team was communicated through an accepted fallback channel. Such changes should be auditable.

### Notifications

WhatsApp and email are both useful notification channels.

WhatsApp should be treated primarily as a league/social notification channel rather than the authoritative source of team submissions.

Coach profiles should retain private email/contact details suitable for individual or grouped notifications.

### Coach versus team identity

Persistent coach/user identity and season-specific team name are separate entities.

This supports historical coach continuity, team-name changes, coach replacements and authenticated contact details without making the team name the permanent identity.

### Replacement coach after draft-order draw

No hard automatic rule is required.

The league/scorer decides whether a replacement inherits the departed coach's draft position or another arrangement/redraw is appropriate. The system should allow an authorised, auditable administrative adjustment.

### Draft-pick trading

Individual draft picks can be traded in preseason and mid-season draft contexts.

The intended workflow is commissioner/scorer style:

1. coaches agree the trade socially;
2. it is communicated to the league/scorer;
3. an authorised scorer/admin records/approves it;
4. the draft pick's ownership/order changes before the pick is exercised;
5. the transaction remains auditable.

The draft engine must therefore represent pick ownership separately from the original draft-order position.

### Public spectator scope

Broad competition information may be public, including:

- team names;
- live Round Centre/matchups;
- current ladder;
- finals;
- SuperScore;
- historical results, records and draft history when implemented.

Private/authenticated data includes coach contact information, saved private drafts, administrative/write controls and non-public audit/ruling detail.

### Web-first product direction

The coaches supported moving toward a full-season BBBFFL website.

The first full-season model should prioritise:

- simple authenticated coach access;
- mobile-friendly team selection;
- live round/match viewing;
- scorer administration and review;
- direct website submissions;
- notifications through WhatsApp/email where useful;
- progressively imported historical competition data.

Legacy Google Sheets/Forms/GAS implementations remain migration/history references rather than target architecture.

## Still unresolved / intentionally discretionary

### Partial early submission followed by no main submission

Likely intended behaviour is that valid early selections remain locked and the previous round fills still-empty positions where possible, subject to scorer confirmation and normal duplicate/lock rules.

This was not confirmed strongly enough to encode as a hard rule yet. Test it during the 2026 replay and confirm before live 2027 if necessary.

### Saved draft never submitted

A private saved draft currently should be treated as **not submitted**. Normal failure-to-submit fallback should therefore apply unless later coach testing results in a different explicit rule.

### Team colours/logos

Potentially desirable but deliberately deferred until later coach-facing UI work.

### Exact ladder equality after existing tiebreaks

If clubs remain exactly equal after premiership points, percentage and Points For, the scorer still requires an explicit ruling unless another formal tiebreak is agreed.

## Implementation note

These decisions should be incorporated into the next full revision of `2027-season-model.md` and exercised during the 2026 historical replay. Implementation tools must not restore previously unresolved alternatives where this document records a confirmed coach decision.