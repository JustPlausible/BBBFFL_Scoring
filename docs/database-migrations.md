# Relational database and migration policy

## Supported databases and approach

PostgreSQL is the supported production database. SQLite remains supported for
local development, hermetic tests, replay databases, and safe prototype-file
upgrades. Configure either through one boundary:

```bash
export BBBFFL_DATABASE_URL=postgresql+psycopg://user:password@host/bbbffl
export BBBFFL_DATABASE_URL=sqlite:////absolute/path/scorer_decisions.db
```

`BBBFFL_DB_PATH` remains a compatibility/local convenience when the URL is
unset. New deployments should use the URL. Alembic and SQLAlchemy Core provide
ordered migrations and a portable connection boundary. The existing
explicit-SQL repository is deliberately not converted to an ORM: it remains
the narrow boundary for DNP, Interchange, overrides, finalisation, frozen
snapshots, and competition isolation. `migrations/versions` is the sole schema
authority. Startup invokes the same migrator as a convenience but owns no DDL.
The shared SQLAlchemy engine boundary enables `PRAGMA foreign_keys = ON` on
every SQLite DB-API connection (including Alembic, replay and test engines);
PostgreSQL connections are unaffected. Repositories therefore complement,
rather than replace, database-enforced referential integrity.

## Operator commands

Run commands from `bbbffl_app` after setting `BBBFFL_DATABASE_URL` (or pass
`--database-url` to the Python commands):

```bash
python -m app.migrations upgrade
python -m app.migrations current
python -m app.migrations downgrade 0001_prototype
alembic revision -m "short description"
```

Back up before upgrading. Production should normally migrate as a release step
before new application code starts; startup also migrates idempotently for the
current single-service deployment.

## Existing prototype databases

Bootstrap is deliberately conservative. Empty databases run the full history.
The known singleton prototype, including the older variant without
`finalized_snapshot`, is validated table-by-table and column-by-column,
stamped at `0001_prototype`, then migrated normally; rows become `grand_final`
rows. The already competition-scoped prototype is also completely validated
before stamping at head and is not rewritten. Partial, additional, or
unexpected schemas fail clearly and are never guessed or arbitrarily stamped.

Upgrades retain timestamps, DNP flags, Interchange targets, override values and
reasons, finalisation note/time, frozen JSON snapshots, and all competition
keys unchanged.

Revision `0004_season` adds the season-aware parent identity tables without
backfilling ambiguous prototype `competition_key` values. See
[`season-competition-schema.md`](season-competition-schema.md).

Revision `0005_identity` adds private coach and season-entry identity/history.
Revision `0006_players` adds the season player cache, season squad limit and
exclusive time-bounded ownership ledger. It refuses downgrade after player
pool data exists because the previous schema cannot represent that state. See
[`player-pool-ownership.md`](player-pool-ownership.md).

## Audit events (revision `0003_audit`)

`0003_audit` adds one domain-neutral `audit_event` table -- the append-only
history of every DNP, Interchange, override and finalisation change (and the
boundary future privileged workflows should reuse). See
[`audit-events.md`](audit-events.md) for the full design; `app/audit.py` is
the implementation.

`slot_dnp` / `interchange_assignment` / `score_override` / `matchup_state`
remain the sole source of truth for current state, exactly as before --
`audit_event` only explains how that state was reached and is never read
back to compute it.

Downgrading below `0003_audit` drops `audit_event` entirely, which is lossy,
so it is refused (matching the `0002_competition` precedent above) once any
row exists there. Downgrade only succeeds while `audit_event` is empty --
i.e. before the first DNP/Interchange/override/finalize mutation on that
database.

## Migration authoring and rollback

Every relational change must be a new ordered revision. Do not add startup DDL
or a parallel `create_all` schema. Test revisions on PostgreSQL and SQLite.
Data migrations must validate inputs and favour transactional data safety.

Downgrade exists only where data is unambiguously representable. Revision
`0002_competition` can downgrade while all rows belong to `grand_final`; it
refuses before schema changes if another competition key exists because the
singleton predecessor cannot represent it. Future irreversible revisions must
raise clearly and document recovery (normally backup restore plus prior code),
not manufacture lossy reversibility.

CI must cover fresh install, realistic legacy upgrade, idempotence, supported
downgrade, refusal boundaries, and repository semantics on SQLite. It must also
run the history and a representative repository write against PostgreSQL.
