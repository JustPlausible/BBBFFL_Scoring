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

# Validate the archive *before* touching the target database at all
# (issue #243 review): pg_restore --list parses the custom-format
# archive's table of contents without connecting to any database, so a
# truncated, corrupted, or wrong-format dump file is rejected right here.
# Without this check, a bad dump would only be discovered after the drop
# below had already destroyed the target -- turning a bad backup file
# into a second, self-inflicted outage on top of whatever this restore
# was meant to recover from.
if ! pg_restore --list "$DUMP_FILE" >/dev/null 2>&1; then
    bbbffl_log "ERROR" "dump file '${DUMP_FILE}' failed pg_restore --list validation (truncated, corrupted, or not a pg_dump custom-format archive) -- refusing to touch '${TARGET_DB}'"
    exit 1
fi

# Restore into a fresh, uniquely-named temporary database first, and only
# ever touch TARGET_DB once that restore has fully succeeded (issue #243
# review, follow-up to the pg_restore --list check above): --list only
# reads the archive's table of contents, not its data blocks, so a dump
# truncated or corrupted past the header could still pass that check and
# then fail partway through the real restore -- if that restore were
# running directly against TARGET_DB (as an in-place drop-and-recreate
# does), the failure would leave TARGET_DB empty or missing instead of
# preserving its last usable state. Restoring into a disposable name
# means a failed pg_restore here never touches TARGET_DB at all.
TMP_DB="${TARGET_DB}__bbbffl_restore_tmp"

_drop_if_exists() {
    if psql -lqtA | cut -d '|' -f1 | grep -qxF "$1"; then
        dropdb "$1"
    fi
}

# A prior run that crashed or was killed between creating TMP_DB and
# renaming it away could leave a stale one behind; clear it before
# reusing the name so this script is safe to simply re-run.
if psql -lqtA | cut -d '|' -f1 | grep -qxF "$TMP_DB"; then
    bbbffl_log "INFO" "dropping stale temporary database '${TMP_DB}' left over from a previous attempt"
    dropdb "$TMP_DB"
fi
bbbffl_log "INFO" "creating temporary database '${TMP_DB}' for the restore"
createdb "$TMP_DB"

# --no-owner: tolerates a target cluster whose roles do not exactly match
# the dump's origin cluster (e.g. a differently-provisioned staging host).
if ! pg_restore --no-owner --dbname="$TMP_DB" "$DUMP_FILE"; then
    bbbffl_alert "PostgreSQL restore of ${DUMP_FILE} into temporary database '${TMP_DB}' FAILED -- '${TARGET_DB}' was never touched"
    _drop_if_exists "$TMP_DB"
    exit 1
fi
bbbffl_log "INFO" "restore into temporary database '${TMP_DB}' completed -- swapping it in as '${TARGET_DB}'"

# Only now, with a fully-restored and verified-complete database sitting
# under TMP_DB, replace TARGET_DB: drop it if it already exists (dropdb
# connects to PostgreSQL's own "postgres" maintenance database to do this,
# never to TARGET_DB itself, so this is safe even when TARGET_DB is the
# live database this script's own env is otherwise configured to talk to),
# then rename TMP_DB to TARGET_DB -- a fast, atomic catalog-only operation,
# not a second data copy. Dropping first (rather than renaming TARGET_DB
# aside as a backup) matches this script's existing "drop and recreate the
# target" contract from before this fix: a schema object a later migration
# introduced, absent from an older pre-release archive, must not survive
# the restore alongside alembic_version going back to that older revision.
_drop_if_exists "$TARGET_DB"
psql -d postgres -c "ALTER DATABASE \"${TMP_DB}\" RENAME TO \"${TARGET_DB}\""
bbbffl_log "INFO" "restore into '${TARGET_DB}' completed"
