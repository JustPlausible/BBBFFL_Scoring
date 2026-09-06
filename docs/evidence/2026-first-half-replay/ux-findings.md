# UX and role-surface findings

The core domain protections held during Rounds 1–4. Most remaining findings
concern discoverability, human-readable identity and clear separation of draft,
submission, lockout and review state.

## Priority groups

### 1. Human-readable identity projection

Several Scorer/Admin surfaces expose stable internal identifiers even though
the application already holds human-readable identities:

- scorer-attention messages prefix warnings with a season-entry UUID instead
  of the current BBBFFL team name;
- the Scorer ladder shows entry UUIDs rather than team names;
- scorer player-evidence cards lead with a provider/internal player ID rather
  than the player's name;
- missed-submission adjudication previews show season-player UUIDs for both
  evidenced-draft and carry-forward outcomes;
- some correction and carry-forward errors expose player UUIDs.

These should be solved through shared server-side read models. Team and player
names should be primary; stable identifiers should remain available in
expandable diagnostics and audit payloads. Browser-side joins should not become
an alternative identity authority.

### 2. Scorer operational dashboard

Issue [#147](https://github.com/JustPlausible/BBBFFL_Scoring/issues/147)
should give the Scorer one role-aware home surface containing:

- current season/round and lifecycle/evidence state;
- the next safe action and blocking reason;
- lineup readiness and staged-lockout status;
- links to delegated entry, missed-submission adjudication, locked correction,
  scorer review and publication;
- attention grouped as blocking, decision required, waiting for evidence,
  advisory and completed;
- brief explanations of why each exceptional authority exists and when it must
  not be used.

The dashboard should aggregate existing authoritative services rather than
implementing new mutation logic.

### 3. Administrator governance dashboard

Issue [#148](https://github.com/JustPlausible/BBBFFL_Scoring/issues/148)
should focus on season setup, identity/role governance, preseason readiness,
competition health, exceptional administrative attention and audit integrity.
It should link into Scorer operations without duplicating ordinary round tasks.

### 4. Mapping and lockout configuration

Round preflight currently requires opaque AFL season, round and match IDs.
Improve it by:

- offering evidence-backed AFL season and round choices;
- recommending the likely corresponding round while retaining explicit
  confirmation because BBBFFL and AFL numbering can diverge;
- listing relevant AFL matches chronologically;
- using human match labels as trigger options;
- displaying operator-local time alongside retained UTC provenance;
- recommending a safe post-final checkpoint after the latest relevant match.

### 5. Navigation and acting context

Role-specific tasks often require manually constructed UUID routes or returning
to Season Centre solely to change represented team. Improve discoverability by:

- providing direct links for Coach lineup, delegated lineup, preflight,
  correction, adjudication, scorer review and public round views;
- keeping the signed-in person, active role and represented team conspicuous;
- preventing shared represented-entry state across browser tabs from causing
  accidental cross-team work, or clearly warning when it changes;
- replacing stuck loading states with recoverable instructions;
- returning operators to a role-appropriate landing page after login.

### 6. Draft and lock presentation

Delegated and Coach views should consistently distinguish:

- private draft versus effective authoritative submission;
- editable, selective-locked, main-locked and deferred positions;
- saved versus submitted state;
- stale-draft conflict and the required reload/rebase action.

PR [#143](https://github.com/JustPlausible/BBBFFL_Scoring/pull/143)
improved delegated staged-lock presentation. Continue testing this shared read
model rather than reconstructing lock state in templates.

### 7. Smaller presentation items

- Correct the finalized Draft Board readiness title that can still say “Needs
  attention” while its explanatory text correctly says the draft is finalized.
- Sort transaction-window squads by draft selection order by default, with
  optional player name, AFL club and draft-order sorting.
- Make lineup position ordering consistent across nomination, Coach, delegated
  and correction views to reduce transcription mistakes.
- Refresh round selectors after publication so they do not retain stale `open`
  labels until a hard refresh.
- Add Points Per Game to the public ladder if confirmed as the final accepted
  ranking criterion.
- Keep mobile selection controls fully within the viewport.

## Suggested issue boundaries

Before implementing the dashboards, triage these as shared prerequisites:

1. one human-readable team/player identity issue across Scorer/Admin surfaces;
2. one round-preflight evidence-picker and chronological lockout-planning issue;
3. one role/task navigation issue, coordinated with dashboard Issues #147/#148;
4. a small presentation batch for stale labels, position ordering and mobile
   layout where changes do not affect sporting authority.
