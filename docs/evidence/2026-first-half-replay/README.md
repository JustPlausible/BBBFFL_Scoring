# 2026 first-half replay evidence

This directory contains the sanitised project record for the completed 2026
BBBFFL first-half replay. The replay exercised the database-native season
workflow against a fixed AFL evidence package, with the operator reconstructing
the historical league process through the same role and lifecycle boundaries
intended for normal use.

The evidence here supports design, regression and handoff decisions. It is not
a copy of private league communications, authentication material, host
configuration, database backups, screenshots or the complete chronological
operator journal.

## Completion state

BBBFFL Rounds 1–9 were completed and published. Every round finished in the
`final` lifecycle state with ten authoritative team submissions. The replay
covered AFL rounds 1344–1352; AFL Opening Round 1343 remained the source of
deferred nominations and compensating-bye evidence.

The closing runtime baseline was application commit `3abc503` with migration
`0026_lineup_adjudication`. The replay checkpoint was at final-results after
AFL round 1352.

## Scope exercised

- season and competition bootstrap;
- the 220-selection draft and opening-squad freeze;
- Opening Round deferred nominations and compensating-bye scores;
- BBBFFL-to-AFL round mapping and staged selective/main lockout;
- Coach, carry-forward and delegated weekly-lineup submission;
- audited locked-lineup correction and missed-submission adjudication;
- scorer calculation, DNP/interchange review, publication and ladder updates;
- Scorer and Administrator operational dashboards.

## Documents

- [Round results](round-results.md) records the round-by-round verification
  outcome.
- [Workflow findings](workflow-findings.md) records durable behavioural
  findings and their disposition.
- [UX findings](ux-findings.md) groups presentation and role-navigation work
  supported by replay observations.
- [Evidence manifest](evidence-manifest.md) identifies the fixed evidence and
  relevant implementation baselines without exposing deployment secrets.
- [Phase 1 closeout](phase-one-closeout.md) records the completion criteria,
  retained evidence and recovery posture.
- [Second-half handoff](second-half-handoff.md) provides the starting context
  and validation priorities for the next replay phase.

## Evidence policy

The repository record may retain public commit, PR and issue references;
non-personal AFL provider identifiers; schema/migration names; package hashes;
and sanitised behavioural observations. It deliberately excludes:

- passwords, tokens, session values, email addresses and private config;
- private network addresses and host filesystem paths;
- raw coach communications and screenshots;
- database dumps and checkpoint files;
- backup filenames and their private integrity hashes;
- personal or team UUIDs that are unnecessary to understand a finding.

Validated database/checkpoint archives, integrity records and page captures are
retained privately by the operator. These documents are curated derivatives
intended for collaborative review and long-term maintenance.
