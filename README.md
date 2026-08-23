# BBBFFL Scoring

Application source for the **Big Bad Bustling Fantasy Football League
(BBBFFL)**.

## Repository structure

- **`bbbffl_app/`** is the active, core application source and the primary
  implementation target for the 2027 rebuild. Start with
  [`bbbffl_app/README.md`](bbbffl_app/README.md) for local development,
  architecture, configuration, and tests.
- **`docs/plans/`** records the agreed 2027 direction and the evidence that
  informs it.
- **`legacy/gas/`** archives the former Google Apps Script, Google Forms, and
  Google Sheets implementation. It is retained as migration and replay
  evidence, not as the target for new feature development.

New BBBFFL features should normally be implemented in `bbbffl_app/`. The
archive must not be treated as a second active application or ported as part
of unrelated work.

## Active development

The application CI runs the Python test suite from `bbbffl_app/` and builds
that directory's Docker image. See the application README for the exact
commands and current prototype scope.

The legacy `clasp` pull/push helpers moved with the GAS projects to
`legacy/gas/`. They are intentionally retired from the normal repository
workflow and are preserved only for maintainers who explicitly need to
inspect or recover the historical Apps Script projects. See
[`legacy/gas/README.md`](legacy/gas/README.md) before using them.
