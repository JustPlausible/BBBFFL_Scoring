# 2026 first-half replay evidence

This directory contains the sanitised project record for the 2026 BBBFFL
first-half replay. The replay exercises the database-native season workflow
against a fixed AFL evidence package, with the operator reconstructing the
historical league process through the same role and lifecycle boundaries
intended for normal use.

The evidence here supports design and regression decisions. It is not a copy
of private league communications, authentication material, host configuration,
database backups, or the complete chronological operator journal.

## Scope

The replay covers:

- season and competition bootstrap;
- the 220-selection draft and opening-squad freeze;
- Opening Round deferred nominations and their compensating-bye scores;
- BBBFFL-to-AFL round mapping and staged lockout;
- Coach and delegated weekly-lineup submission;
- audited exceptional correction/adjudication workflows;
- scorer calculation, DNP/interchange review, publication, and ladder updates.

## Documents

- [Round results](round-results.md) records the round-by-round verification
  outcome.
- [Workflow findings](workflow-findings.md) records durable behavioural
  findings and their disposition.
- [UX findings](ux-findings.md) groups presentation and role-navigation work
  supported by replay observations.
- [Evidence manifest](evidence-manifest.md) identifies the fixed evidence and
  relevant implementation baselines without exposing deployment secrets.

## Evidence policy

The repository record may retain public commit, PR and issue references;
non-personal AFL provider identifiers; schema/migration names; package hashes;
and sanitised behavioural observations. It deliberately excludes:

- passwords, tokens, session values, email addresses and private config;
- private network addresses and host filesystem paths;
- raw coach communications and screenshots;
- database dumps and checkpoint files;
- personal or team UUIDs that are unnecessary to understand a finding.

The detailed working journal remains outside the repository. These documents
are curated derivatives intended for collaborative review and long-term
maintenance.
