# UX findings

The replay produced several usability improvements without weakening
authoritative server-side controls.

## Delivered during Phase 1

- Team, player and rules identities are presented human-readably on operational
  surfaces, with stable identifiers retained as diagnostics (PR #154).
- Operational pages reload authoritative state after successful or rejected
  mutations, avoiding stale lifecycle and draft renders (PR #156).
- Vacant positions display as locked after main lockout, matching submission
  enforcement (PR #157).
- Round preflight offers evidence-backed AFL season/round recommendations and
  human-readable match choices (PR #158).
- Scorer and Administrator role dashboards provide central workflow and
  governance entry points (PRs #159 and #160).

## Remaining findings

### Final rounds should not appear as blockers

The Scorer attention queue and review/publication readiness panel currently
treat a published `final` round as “not review” and therefore blocking/not
ready. Once publication is complete, those panels should show a completed state
and direct attention to the next actionable round.

### Acting context should follow authorised dashboard actions

Some Scorer dashboard actions require a separate visit to Season Centre to
activate the appropriate role or represented team. An explicit dashboard action
should establish the permitted acting context before navigation while
preserving the signed-in human as the audit actor. It must never silently grant
authority.

### Workflow guidance should be actionable

The “Which workflow do I need?” content is useful documentation but does not
link to the corresponding operational pages. Context-aware links would reduce
navigation errors, especially for delegated submission, correction and
adjudication.

### Preflight defaults need clearer feedback

- “Use recommended values” can populate a collapsed Advanced section without an
  obvious visible result; it should expand or confirm the populated values.
- Trigger sequence should default to the next unused sequence.
- A recommended main stage may associate every remaining match. This is safe,
  but selecting the intended first main-lock match directly better expresses
  the league rule and simplifies diagnostics.
- Trigger keys are operator labels; guidance should recommend a consistent
  convention such as `early-1` and `main`.

### Local dates should use Australian presentation

Where both UTC and browser-local time are displayed, the local date should use
Australian day/month/year order (for example, `16/4/2026`) and identify the
timezone. UTC should remain visible as authoritative evidence.

### Ladder presentation needs completion

- The public ladder may display points per game as an informative statistic,
  but it must not use PPG as an ordering criterion because it is derived from
  PF and provides no additional tiebreak information.
- Ladder order remains competition points, percentage, then PF. Exact equality
  after those criteria should be escalated for a recorded, audited Scorer
  decision rather than resolved by an invented automatic tiebreaker.
- Scorer ladder presentation should remain aligned with the public ladder's
  human-readable table and columns.

### Public round browsing is a low-priority roadmap item

Public viewers should eventually be able to browse previous and upcoming
rounds, preview matchups, open match detail/evidence and view the ladder as at a
selected round. This can sit on top of the existing published round data and
does not block the replay.

## Capture context

Final dashboard and public-page captures were retained privately as supporting
operator evidence. A “Failed to fetch” message visible in an offline capture
occurred because the application containers had already been stopped while the
page's live refresh was still running; it is not recorded as an application
defect.
