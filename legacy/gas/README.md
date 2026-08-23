# Archived Google Apps Script implementation

This directory contains the former Google Apps Script (GAS) and Google
Sheets/Forms implementation of BBBFFL scoring. It automated three parts of the
league's workflow:

- `AFL_Stats/` collected AFL fixtures, players, injuries, and match statistics
  into Google Sheets;
- `BBBFFL_Weekly_Teams/` processed Google Form team submissions and produced
  validated weekly team sheets; and
- `BBBFFL_Results/` combined selections and AFL statistics to generate live and
  completed BBBFFL results, fixtures, ladder views, and scorer review tools.

`AFL_Stats.zip` is retained alongside the source as a history-relevant snapshot.
No legacy functionality has been deleted during archival.

## Status and intended use

The GAS implementation is retained for **historical reference, migration work,
and 2026 replay evidence**. It is no longer the primary implementation target.
The active/core source for the 2027 rebuild is [`../../bbbffl_app/`](../../bbbffl_app/),
and new BBBFFL features should normally be implemented there.

Do not port or extend this code merely because it remains available. Any future
migration should be explicit, evidence-driven work with its own scope.

## Retired `clasp` workflow

`pull-all.sh` and `push-all.sh` are archived operational helpers. They resolve
their project paths relative to their own location, so invoking them from the
repository root or another working directory does not redirect `clasp` to an
unrelated directory. They are **intentionally retired from normal development
and CI**. They require:

1. Node.js and `clasp`;
2. maintainer access to the historical Google Apps Script projects; and
3. a `.clasp.json` in each project directory.

The `.clasp.json` files contain deployment-specific project identifiers and are
ignored by Git, so they may only exist in an authorised maintainer's local
checkout. The committed `appsscript.json` manifests remain archived with each
project. The helpers must be invoked deliberately, for example from the
repository root:

```bash
./legacy/gas/pull-all.sh  # fetch historical remote GAS source
./legacy/gas/push-all.sh  # update historical remote GAS source; use only with approval
```

Neither helper is called by GitHub Actions. Moving them into this archive
prevents the legacy deployment workflow from appearing to be a supported
root-level application workflow while preserving it for authorised recovery.
