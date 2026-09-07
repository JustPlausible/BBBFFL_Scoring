# Workflow findings

## Verified behaviour through Round 9

The replay verified the intended end-to-end path:

1. bootstrap the season, competition, player pool and draft;
2. freeze the opening squads and record Opening Round deferred nominations;
3. map each BBBFFL round to authoritative AFL evidence;
4. configure selective and main lockout triggers;
5. open the round and collect Coach, delegated or carry-forward submissions;
6. activate lockouts from scheduled match-time evidence;
7. advance the round from open to live, review and final;
8. calculate scores, resolve DNP/interchange decisions, sign off and publish;
9. accumulate the public ladder across final rounds.

All nine ordinary rounds completed with ten authoritative lineups. Opening
Round deferred evidence was fully consumed by Round 4.

## Durable findings

### Lifecycle and evidence are separate authorities

The BBBFFL lifecycle controls which workflow is available. AFL evidence controls
match scheduling, lockout activation and whether final statistics are
available. A match may still report provider status `UPCOMING` while its
scheduled time has legitimately activated a BBBFFL lockout.

### Trigger activation must be materialised

Moving the replay checkpoint and restarting the application evaluates triggers
against authoritative replay time. The persisted activation record, not a
browser label, proves that a selective or main lockout occurred.

### Main lockout includes vacancies

After main lockout, an authoritative vacant position is locked even though no
fabricated player-level lock row can exist for it. PR #157 aligned the shared
read model with the server-side enforcement already rejecting a late fill.

### Corrections preserve history

Ordinary submission never bypasses a lock. An authorised Scorer correction
creates a new audited submission version with a substantive reason. The
original submission and actor history remain intact.

### Missed first submissions are adjudicated, not silently repaired

A private draft is not an authoritative submission. Acceptance of an evidenced
draft is limited to its latest snapshot and per-position evidence that predates
the relevant lock. Values without that proof become vacant. The alternative
carry-forward resolution sources only the previous round's effective submitted
lineup and never merges rejected draft content. Any league discussion occurs
outside the application; the Scorer records its outcome in the required reason.

### Carry-forward is deliberately conservative

Round 9 demonstrated that carry-forward may disagree with an informal
historical reconstruction. The system correctly preferred the previous
authoritative lineup. Historical convenience is not a reason to invent or merge
a selection.

### Final statistics can differ from historical worksheets

The replay uses the available authoritative provider evidence. Later provider
corrections or historical manual-entry errors can therefore alter PF, PA,
percentage or points-per-game without changing a matchup winner. Any manual
alignment required for draft or finals seeding needs a distinct audited
administrative workflow.

### Replay recovery requires a paired restore point

A useful replay restore point consists of a validated database archive and its
matching replay checkpoint. Restoring only one can place lifecycle data and AFL
evidence time on different sides of a lockout boundary.

## Implementation outcomes during the replay

- Human-readable team, player and rules labels: PR #154.
- Authoritative browser refresh after mutations: PR #156.
- Main-lock treatment of vacant positions: PR #157.
- Guided round mapping and lockout preflight: PR #158.
- Scorer Operations Dashboard: PR #159.
- Administrator governance dashboard: PR #160.
- Closing tested baseline: `3abc503`, migration
  `0026_lineup_adjudication`.

## Follow-up boundaries

- Reconcile material historical-stat variance before it affects draft or finals
  qualification.
- Keep replay checkpoint controls outside ordinary 2027 user workflows.
- Treat multiple prepared future rounds as configuration, not simultaneous
  current rounds; each lifecycle and trigger remains independently scoped.
- Build the second-half replay around Round 10, the mid-season draft,
  Rounds 11–20, regular finals and SuperScore.
