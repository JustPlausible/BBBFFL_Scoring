#!/bin/sh
set -eu

# Restores a BBBFFL production-format pg_dump backup into a target
# database (issue #243). Usage:
#
#   restore_postgres.sh <dump-file> <target-database>
#
# The target database is a required, explicit argument -- there is no
# default -- so this can never silently restore over PGDATABASE (the
# running production database) just because that environment variable
# happens to be set in the shell it runs in. Restoring over the database
# PGDATABASE names is refused outright unless
# BBBFFL_ALLOW_RESTORE_OVER_LIVE_DATABASE=yes is set, which exists only for
# the deliberate database-affecting rollback path
# (docs/production-operations.md#rollback-strategy) -- day-to-day restore
# rehearsal always targets a differently-named clean/staging database and
# never needs that flag. See
# docs/production-operations.md#restore-procedure for the full rehearsed
# procedure (clean staging database, migration-head + representative-data
# verification) this script is one step of.
#
# Run inside the "backup" service container, which has the exact
# pg_dump/pg_restore/psql/createdb build matching the "database" service:
#   docker compose -f compose.production.yaml exec -T backup \
#     /scripts/restore_postgres.sh /backups/bbbffl-bbbffl-20270101T020000Z.dump bbbffl_staging_restore

usage() {
    echo "usage: $0 <dump-file> <target-database>" >&2
    exit 2
}

[ $# -eq 2 ] || usage
DUMP_FILE="$1"
TARGET_DB="$2"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=./lib_alert.sh
. "$SCRIPT_DIR/lib_alert.sh"

[ -f "$DUMP_FILE" ] || {
    bbbffl_log "ERROR" "dump file not found: ${DUMP_FILE}"
    exit 1
}

# pg_restore/psql/createdb (libpq) read PG*, not POSTGRES_* -- see
# backup_postgres.sh's identical mapping. Defaulted, never overridden, so
# an operator restoring onto a different host can still export PGHOST/etc.
# explicitly before running this script.
: "${PGHOST:=database}"
: "${PGDATABASE:=${POSTGRES_DB:-}}"
: "${PGUSER:=${POSTGRES_USER:-}}"
: "${PGPASSWORD:=${POSTGRES_PASSWORD:-}}"
export PGHOST PGDATABASE PGUSER PGPASSWORD

if [ "$TARGET_DB" = "${PGDATABASE:-}" ] && [ "${BBBFFL_ALLOW_RESTORE_OVER_LIVE_DATABASE:-}" != "yes" ]; then
    bbbffl_log "ERROR" "refusing: target '${TARGET_DB}' matches PGDATABASE (the live database)."
    bbbffl_log "ERROR" "set BBBFFL_ALLOW_RESTORE_OVER_LIVE_DATABASE=yes only for a deliberate rollback recovery -- see docs/production-operations.md#rollback-strategy"
    exit 3
fi

bbbffl_log "INFO" "restoring ${DUMP_FILE} into database '${TARGET_DB}' on host '${PGHOST:-database}'"

if ! psql -lqtA | cut -d '|' -f1 | grep -qxF "$TARGET_DB"; then
    bbbffl_log "INFO" "target database '${TARGET_DB}' does not exist yet -- creating it"
    createdb "$TARGET_DB"
fi

# --clean --if-exists: drops conflicting objects first, so a retried
# restore into an already-partially-restored database is idempotent --
# same convention the existing replay playbooks use (e.g.
# docs/2026-second-half-replay-playbook.md). --no-owner: tolerates a
# target cluster whose roles do not exactly match the dump's origin
# cluster (e.g. a differently-provisioned staging host).
if pg_restore --clean --if-exists --no-owner --dbname="$TARGET_DB" "$DUMP_FILE"; then
    bbbffl_log "INFO" "restore into '${TARGET_DB}' completed"
else
    bbbffl_alert "PostgreSQL restore of ${DUMP_FILE} into '${TARGET_DB}' FAILED"
    exit 1
fi
