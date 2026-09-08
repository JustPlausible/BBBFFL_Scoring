# Second-half replay handoff

## Intended scope

Phase 2 begins with BBBFFL Round 10 and extends through:

1. Round 10 weekly operations;
2. the mid-season draft after Round 10;
3. Rounds 11–20;
4. regular finals;
5. SuperScore finals;
6. season closeout and historical artifact preparation.

The first-half replay remains valuable as a completed weekly-workflow baseline;
it does not need to be recoded merely because the mid-season draft occurs after
Round 10. This ordering reflects the confirmed historical 2026 chronology:
Round 10 must use the pre-draft ownership state, and the resulting mid-season
squads first apply from Round 11. The Round 9 boundary in the Phase 1 playbook
is a replay-scope boundary, not a draft-timing rule.

## Start checklist

Before opening Round 10:

- restore or clone the validated Phase 1 database/checkpoint pair;
- confirm application commit and migration head;
- confirm Rounds 1–9 remain `final`;
- confirm the Round 9 public results and ladder are readable;
- confirm the fixed AFL evidence package required for Round 10 onward;
- create a new pre-Round-10 database/checkpoint recovery pair;
- review open issues and merged changes since `3abc503`;
- record the application image used for the second-half baseline;
- after each checkpoint change and application restart, perform an authoritative
  lineup/lockout read (or the intended submission operation), then verify the
  persisted trigger activation; restart alone does not materialise a trigger.

## Phase 2 validation priorities

### Mid-season draft

- establish eligibility, ordering and timing from the rules;
- preserve existing ownership periods and audit actors;
- verify delist/add transactions and squad limits atomically;
- publish the resulting squads before Round 11;
- retain an auditable relationship between pre-draft and post-draft ownership.

### Weekly rounds

Continue the proven mapping, staged lockout, submission, correction,
adjudication, scoring, review and publication cycle. Confirm that dashboard
next-actions move to the current round and that a completed final round is not
shown as a blocker.

### Historical ladder variance

Before the mid-season draft or finals uses ladder position, compare the replay
ladder with the historical decision point. If provider-stat revisions or
historical manual-entry errors materially change eligibility or seeding, record
the evidence and use an explicit audited administrative ruling. Do not silently
alter weekly results or database rows.

### Finals and SuperScore

Treat fixture generation, qualification, seeding, elimination/progression,
publication and SuperScore as distinct workflows. Take paired database and
checkpoint snapshots before the mid-season draft, before finals generation and
before SuperScore.

## UX observations to re-test

- Scorer final-state panels should show completion rather than a blocker.
- Dashboard actions should establish an already-authorised role/represented
  team context where appropriate.
- Workflow guidance should link to the owning operational page.
- Preflight recommendations should expose populated values and default the next
  available trigger sequence.
- Local dates should use Australian day/month/year presentation with timezone.
- Public/scorer ladders may display points per game for interest, but must
  order by competition points, percentage and PF only; exact equality requires
  an audited Scorer decision.
- Public round browsing remains a low-priority roadmap enhancement.

## Evidence additions during Phase 2

Extend the evidence record with milestone summaries rather than raw private
artifacts. Record:

- mapped AFL round and trigger match IDs;
- lifecycle and authoritative-lineup counts;
- exceptional workflow type and sanitised outcome;
- calculation/publication revision outcomes;
- ladder or seeding decisions affected by evidence variance;
- implementation PRs and tested runtime baselines;
- recovery-pair creation and validation status.

Keep private communications, deployment paths, credentials, raw backups,
checkpoints and page captures outside the repository.
