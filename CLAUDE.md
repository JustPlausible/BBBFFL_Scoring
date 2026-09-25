# CLAUDE.md

Guidance for Claude Code sessions in this repository. Sessions usually start fresh for one GitHub issue. This file holds durable knowledge only. For current status, read [`docs/2027-live-season-readiness.md`](docs/2027-live-season-readiness.md), not this file.

## What this is

BBBFFL Scoring is the application for the Big Bad Bustling Fantasy Football League: ten coaches, AFL player squads, weekly nine-position lineups, head-to-head ordinary rounds, a top-five Finals series and a four-round SuperScore competition. It is a FastAPI + Jinja (server-rendered, vanilla JS) service over PostgreSQL (production) or SQLite (dev/tests/replay). All AFL data comes from the separate `afl-api` service's public `/api/v1` contract.

- `bbbffl_app/` is the only active application. New work goes here.
- `legacy/gas/` is the archived Google Apps Script system. It is historical and replay evidence only. Never extend it or port it as part of unrelated work.
- `docs/` holds one design doc per domain, plus replay evidence (`docs/evidence/`) and planning (`docs/plans/`, `docs/roadmap/`). Before changing a domain, read its doc. Module docstrings often point to the doc section that is authoritative.

## Two application surfaces (do not mix them)

1. **Season model**, the primary target. Persisted seasons, coaches/entries, drafts, ownership, fixtures, lineups, lockouts, calculations, scorer review, official results, ladder, Finals and SuperScore, all in the database.
2. **Legacy Grand Final / SuperScore prototype**: `app/service.py`, `app/teams.py`, `app/superscore.py`, `app/db.py`'s `DecisionsRepository`, `app/scorer_decisions.py`, and the routes `/legacy/grand-final`, `/admin`, `/superscore` (the last is opt-in via `BBBFFL_SUPERSCORE_CONFIG_PATH`). It is kept working as a separate prototype and is not the place for new season features. Note that `/admin` is the legacy panel, while `/admin/dashboard` is the Administrator Dashboard.

Both share `app/scoring.py` and the AFL client boundary and nothing else. `tests/test_architecture.py` enforces this.

## Where to look

All paths below are under `bbbffl_app/app/`. Routes live in `routes/` and templates in `templates/`.

| Area | Modules |
|---|---|
| Scoring formulas (pure leaf, no internal imports) | `scoring.py` |
| AFL API integration | `afl_client.py` (the only adapter for response shapes), `afl_resilience.py` (retry/cache/freshness), `afl_diagnostics.py`, `round_mapping.py` (BBBFFL round → AFL round), `player_stats_context.py`, `participation.py` |
| Season, identity, ownership | `season.py`, `identity.py`, `player_pool.py`, `season_centre.py` |
| Drafts and trades | `draft.py`, `draft_board.py`, `preseason.py`, `midseason_draft.py`, `shortlist.py` |
| Fixtures and ordinary rounds | `fixtures.py`, `competition_lifecycle.py`, `round_preflight.py`, `calculations.py`, `round_review.py`, `ladder.py` |
| Coach lineups | `lineups.py`, `lineup_validation.py`, `coach_lineup.py`, `carry_forward.py`, `lineup_proxy.py`, `lineup_correction.py`, `lineup_adjudication.py`, `opening_round.py`, `lockouts.py` |
| Scorer / Admin surfaces | `scorer_dashboard.py`, `admin_dashboard.py`, `routes/round_review.py` (Round Centre), `routes/delegated_operations.py` |
| Finals | `finals.py` (bracket), `finals_seeding.py`, `finals_preflight.py`, `finals_review.py`, `finals_participation.py`, `public_finals.py` |
| SuperScore (season model) | `superscore_round.py`, `superscore_participation.py`, `superscore_review.py`, `superscore_results.py` |
| Finals + SuperScore together | `finals_superscore_open.py`, `finals_superscore_dashboard.py`, `stream_presentation.py` |
| Season end | `season_completion.py`, `season_awards.py`, `season_archival.py` |
| Public site | `public_rounds.py`, `public_finals.py`, `presentation.py`, `routes/public_rounds.py` |
| Auth and permissions | `auth.py`, `authorization.py` (the single HTTP policy boundary), `routes/context.py` (acting role / represented entry), `csrf.py` |
| Audit | `audit.py` (`append_event`, `ActorContext`) |
| Replay (never used in live operation) | `replay*.py`, `scripts/*_2026*.py`, `scripts/*_replay.py` |
| Settings | `config.py` (`get_settings()` is the single validated settings boundary) |

Useful docs: [`docs/architecture.md`](docs/architecture.md) (layering and transaction ownership), [`docs/2026-finals-superscore-design.md`](docs/2026-finals-superscore-design.md) (see its "Shared versus distinct behaviour" table), [`docs/authorization-and-privacy.md`](docs/authorization-and-privacy.md), [`docs/acting-context.md`](docs/acting-context.md), [`docs/audit-events.md`](docs/audit-events.md), [`docs/afl-api-v1-contract.md`](docs/afl-api-v1-contract.md).

## Commands

Run everything from `bbbffl_app/` using Python **3.11** in a venv (`pip install -r requirements-dev.txt`). On Windows, use `.venv\Scripts\` instead of `.venv/bin/`. Setup is described in [`bbbffl_app/README.md`](bbbffl_app/README.md#source-development-workflow).

Focused checks while developing:

```bash
python -m pytest tests/test_<area>.py -q          # the module(s) you touched plus their _api / _client_requests siblings
python -m pytest tests/test_<area>.py -k <name>   # one scenario
python -m pytest tests/test_architecture.py       # always, if you add or move imports between app modules
python -m pytest tests/test_db_migration.py       # always, if you touch migrations
```

Checks that match CI; use the local pre-push and full-suite risk guidance below. Each is a separate required CI job; see [`docs/ci-quality-gates.md`](docs/ci-quality-gates.md) for details.

```bash
ruff format --check .      # `ruff format .` to fix; line length is 120
ruff check .               # narrow rule set: E4/E7/E9/F/I
mypy                       # incremental scope only: files listed in pyproject [tool.mypy]
python -m pytest           # full suite: ~2,100 tests, roughly 25–40 min (mostly per-test SQLite migrations)
python -m scripts.dependency_audit
docker build -t bbbffl-prototype .
```

- The CI `postgres-migrations` job also runs the `*_postgresql.py` and `*_concurrency.py` suites against real PostgreSQL. Locally these skip unless `BBBFFL_DATABASE_URL` points at PostgreSQL.
- `*_client_requests.py` tests execute JS extracted from served templates under Node, and skip if Node is missing.
- Because the full suite is slow, run focused tests plus lint, format and mypy locally, then rely on CI for the full run. Exception: run the full suite locally first when the change touches migrations, `db.py`, `audit.py`, `scoring.py`, or other shared services.
- Never make live `afl-api` calls in tests. Use `tests/conftest.py`'s `FakeAflClient` or the offline evidence under `tests/fixtures/afl_evidence/` (loaded via `tests/afl_evidence.py`). `scripts/afl_contract_diagnostic.py` and the `integration-diagnostics` workflow are manual only.

## Architecture and persistence rules

- **Layering:** routes → application services → domain repositories → `db`/`audit`. Routes translate HTTP into a service call and must not decide whether a mutation is legal. Services raise plain domain exceptions, not `HTTPException`. `app/main.py` is the composition root and maps exceptions to status codes. `tests/test_architecture.py` enforces the import graph, and every new app module must be classified there.
- **SQL:** explicit SQLAlchemy Core SQL through `app.db`. No ORM.
- **Transactions:** each repository method owns its transaction (`app.db.transaction()`), and commits its domain write and its `append_event` audit row together. An operation spanning two repositories gets its own service function that follows `scorer_decisions.finalize`'s shape. Never open transactions in route handlers, and never nest write transactions (SQLite has a single writer).
- **Row locks:** use the existing `_for_update_suffix` / CAS-revision patterns for concurrency. PostgreSQL behaviour is covered by the `*_concurrency.py` tests.
- **Migrations:** Alembic in `migrations/versions/` is the only schema authority. Never add startup DDL or `create_all`.
  - Every schema change is a new ordered revision `NNNN_<slug>.py`, and must work on both SQLite and PostgreSQL. Use `op.batch_alter_table` for constraint changes, and re-create any hand-written triggers that a SQLite batch rebuild drops.
  - A downgrade must refuse, with a clear error, rather than lose data it cannot represent.
  - Add coverage to `tests/test_db_migration.py`, and record notable revisions in [`docs/database-migrations.md`](docs/database-migrations.md).
- **Audit:** every privileged or state-changing operation records an `audit_event` via `app.audit.append_event` with an `ActorContext`. `audit_event` is append-only (DB triggers enforce this) and is never read back to compute current state. Domain tables are the source of truth.
- **Immutability and history:** submitted lineup versions, lock evidence, frozen fixture draws, official results, draft corrections and finals seed snapshots are protected by DB triggers or refusal logic. To correct something, append a new version or record, audited and with a reason. Never update history in place.
- **Completed seasons are read-only:** result-changing writes call `SeasonRepository.guard_writable` and raise `SeasonCompletedError`. Keep new write paths inside this fence.
- **Authorization:** decide permissions in `app.authorization`, not ad hoc in routes. Coach sessions, scorer/admin roles, the acting context and the legacy `X-Admin-Token` are distinct. See the auth docs before changing who can do what. Public read models must never expose private coach data.

## Domain invariants (supported by code and docs)

- **Scoring:**
  - Forward: `6×goals + behinds`. Midfield: disposals. Ruck: `marks + hitouts`. Tackler: `6×tackles`.
  - The Interchange player scores only as the position they are assigned to.
  - All streams use `app/scoring.py` unchanged. Never add stream-specific scoring formulas.
- **Calculated vs official:**
  - `calculations.py` writes replaceable calculated snapshots. Only publication and correction write official results.
  - A live estimate must never become an official result implicitly.
- **Scorer decisions:**
  - Scorer overrides change only the effective score; the calculated score is always kept alongside.
  - DNP/Interchange recommendations are advisory; only an explicit scorer ruling changes a score.
- **AFL match states:**
  - `POSTGAME` is not complete. Only `CONCLUDED` (`completed`) counts toward sign-off readiness.
  - Nothing is finalised automatically; finalisation needs an explicit scorer action.
  - Legacy prototype finalisation (`scorer_decisions.finalize`) fails closed with a 503 if the AFL evidence behind it is not confirmed fresh.
- **Ordinary round lifecycle:**
  - The round lifecycle moves forward only: `upcoming → open → live → review → final`.
  - `review → final` happens only through atomic five-result publication.
  - `live` is not the same as "locked": which positions are locked is decided per position by the staged lockout plan (`lockouts.py`).
- **Carry-forward:** a lineup carries forward only within the same competition stream. The only cross-stream exceptions are the confirmed ones: Finals Week 1, seed 1's Week 2, and SS1.

## Ordinary season vs Finals vs SuperScore

Competition streams are typed `ordinary`, `finals` or `superscore`. Much of the ordinary-round code assumes exactly five matchups and a fixed fixture draw. Check which stream a code path serves before reusing it.

- **Ordinary:**
  - Ten entries play five pre-drawn head-to-head matchups per round.
  - Scorer review and sign-off go through `round_review.py`.
  - Tied matchups escalate to a scorer ruling.
- **Finals:**
  - Only the top five by frozen finals seed take part.
  - The bracket (`finals.py`) generates each week's pairings from the previous week's results.
  - A tie goes to the higher frozen seed.
  - Finals has its own review adapter (`finals_review.py`) and its own eliminations.
- **SuperScore:**
  - All ten entries, in rounds SS1–SS4, run at the same time as Finals.
  - There are no matchups: results are an entry-scoped leaderboard (`superscore_results.py`), with rulings and review state keyed by entry (`superscore_review.py`), and tied top scores stand as joint winners.
  - SuperScore does not count toward all-time BBBFFL records.
- **Shared by all three:** the nine-position lineup, validation, ownership, lockouts, participation evidence and the scoring engine.
- **Distinct per stream:** eligibility, pairing, results shape, tie rules and review/sign-off. Where Finals and SuperScore must coordinate (for example the paired "Open week" action and a shared lockout plan), use the composition modules (`finals_superscore_*`) rather than making either stream depend on the other.

## Historical and replay behaviour

The 2026 season was fully replayed through this application as historical evidence, and 2026 and 2027 data coexist in one database, scoped by season and competition.

- Preserve historical and replay behaviour, including 2026-specific paths such as the finals seeding snapshot and replay scripts, unless the issue explicitly changes it.
- Model or document historical replay anomalies explicitly rather than weakening a general live-season invariant, unless the issue explicitly requires that change.
- Replay replaces only the AFL boundary and the clock. It must keep going through the normal services, and domain modules must not import replay modules.
- Replay checkpoints and clocks are never part of live operation.
- Never edit committed evidence (`tests/fixtures/afl_evidence/`, `docs/evidence/`) to make a test pass. A correction is a new file that records where it came from.

## Coding and testing conventions

- **Extend existing patterns.** Before building anything, search for an existing service, repository, read model, helper or template doing something similar, and extend it. Do not add a parallel workflow, a second store, a second lineup model or a speculative abstraction layer. Module docstrings usually say "builds on, rather than duplicating" and name what to reuse.
- **Put rules in the lowest layer that owns them**, and keep read models (dashboards, the public site) free of their own scoring or ladder logic: they read authoritative state.
- **Match the local style:**
  - Explicit dataclasses and small domain exception classes.
  - Module docstrings that explain the issue context and invariants.
  - Ruff formatting at 120 columns.
  - Don't enable new lint rule families or widen mypy scope as a side effect.
- **Regression tests:**
  - Write regression and boundary tests next to the existing tests for the area (`tests/test_<module>.py`, `_api.py` for HTTP, `_client_requests.py` for template JS, `_concurrency.py` / `_postgresql.py` for PostgreSQL semantics).
  - Reuse the helper modules (`db_helpers.migrated_connection`, `lineup_helpers`, `finals_helpers`, `superscore_helpers`, `round_review_helpers` and others) rather than building fixtures from scratch.
  - Test refusals as well as successes, and check that a rejected mutation leaves domain tables and the audit trail unchanged.
- **Keep the implementation scoped to the issue.** No unrelated refactoring, reformatting, renames or dependency upgrades. If you find an unrelated problem, mention it in the PR or report it rather than fixing it inline.
- **Update the relevant `docs/*.md`** when behaviour, routes or workflows change.

## Working interactively vs autonomously

- **Interactive local sessions:** inspect `git status` and the current diff before editing. Use the current local environment and state where useful for investigation and validation. Never discard, overwrite or revert user changes without explicit permission.
- **Autonomous GitHub issue work:** use an issue-specific branch and handle routine Git and PR operations according to the workflow below.

## GitHub issue workflow

1. Read the issue and its comments. Inspect the affected implementation, its docs and its tests before changing anything.
2. Look for related existing behaviour and tests, so the change extends established patterns.
3. Implement the smallest complete solution that satisfies the issue.
4. Add or update regression and boundary tests appropriate to the change.
5. Run focused tests while developing and the appropriate pre-push checks above before treating the work as ready. Run the full local suite when the risk guidance calls for it; otherwise rely on CI for the full suite.
6. Handle the routine Git work without asking the user to:
   - Work on an issue-specific branch (for example `claude/issue-<n>-<short-slug>`).
   - Make clear, imperative commits that reference the issue, such as `Fix X when Y (#<n>)`, or `(#<n> review)` for review fixes.
   - Push the branch, then create or update the PR, linking the issue.
7. Monitor CI. Investigate and fix any failures caused by the work. If a CI run is slow, use the triage in [`docs/ci-quality-gates.md`](docs/ci-quality-gates.md#python-test-runtime-and-slow-ci-runs) before cancelling it.
8. Monitor Codex review findings on the PR.
9. **P1 findings are stopping conditions.** Resolve them before completion.
10. **P2 findings:** investigate each one, and resolve genuine defects and meaningful regression risks. If a fix would need substantial architectural work, significant scope expansion or a risky refactor outside the issue, pause and explain the trade-off to the user instead of doing it automatically.
11. After fixing P1/P2 findings, reply to or resolve the corresponding review threads where appropriate. Rerun the relevant tests, push, and keep checking CI and review status.
12. Do not describe an issue as complete until the implementation, the relevant tests, CI and the required review findings are all in an acceptable state.
