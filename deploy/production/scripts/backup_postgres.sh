#!/bin/sh
set -eu

# Scheduled PostgreSQL backup for BBBFFL production (issue #243). Runs
# inside the compose.production.yaml "backup" service, built from the exact
# same postgres:16-alpine image as the "database" service, so pg_dump's
# version always matches the server it is backing up -- see
# docs/production-operations.md#scheduled-backups for the schedule,
# retention policy and why custom-format pg_dump was chosen.
#
# Naming: bbbffl-<database>-<UTC timestamp>.dump makes each file's origin
# database and exact capture time unambiguous from the filename alone, with
# no separate manifest to keep in sync. Never git-committed (see
# .gitignore) -- these land only on the "backup" service's bind-mounted
# host directory (deploy/production/backups/, outside every container's
# writable layer).

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=./lib_alert.sh
. "$SCRIPT_DIR/lib_alert.sh"

# pg_dump/pg_restore/psql (libpq) read PG*, not POSTGRES_* -- the postgres
# image's own env_file: convention (see bbbffl_app/.env.production.example).
# Only defaulted when unset, so an operator/rehearsal invocation that
# already exports PG* explicitly (e.g. to target a different host) is
# never overridden.
: "${PGDATABASE:=${POSTGRES_DB:-}}"
: "${PGUSER:=${POSTGRES_USER:-}}"
: "${PGPASSWORD:=${POSTGRES_PASSWORD:-}}"
export PGDATABASE PGUSER PGPASSWORD

: "${PGDATABASE:?PGDATABASE (or POSTGRES_DB) must be set}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BBBFFL_BACKUP_RETENTION_DAYS:-14}"

TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$BACKUP_DIR/bbbffl-${PGDATABASE}-${TIMESTAMP}.dump"
TMP_OUT="${OUT}.in-progress"

bbbffl_log "INFO" "starting backup of database '${PGDATABASE}' to ${OUT}"

if pg_dump -Fc -f "$TMP_OUT"; then
    # Atomic rename: a reader (or a concurrent retention sweep) never sees
    # a partially written file at the final name.
    mv "$TMP_OUT" "$OUT"
    SIZE=$(wc -c <"$OUT" 2>/dev/null || echo unknown)
    bbbffl_log "INFO" "backup succeeded: ${OUT} (${SIZE} bytes)"

    find "$BACKUP_DIR" -maxdepth 1 -name 'bbbffl-*.dump' -mtime "+${RETENTION_DAYS}" -print -delete 2>/dev/null |
        while read -r pruned; do
            bbbffl_log "INFO" "pruned expired backup (older than ${RETENTION_DAYS}d): ${pruned}"
        done

    if [ -n "${BBBFFL_BACKUP_SUCCESS_PING_URL:-}" ]; then
        if ! wget -q -T 10 -O /dev/null "$BBBFFL_BACKUP_SUCCESS_PING_URL"; then
            bbbffl_log "WARNING" "backup succeeded but the success-ping (dead man's switch) delivery failed"
        fi
    fi
else
    rm -f "$TMP_OUT"
    bbbffl_alert "PostgreSQL backup of '${PGDATABASE}' FAILED at ${TIMESTAMP} -- see: docker compose -f compose.production.yaml logs backup"
    exit 1
fi
