#!/bin/sh
set -eu

# Docker HEALTHCHECK for the "backup" service (issue #243 review). A cron
# child job failing does not, by itself, change the container's own status
# -- PID 1 stays the foreground `crond` daemon regardless of a scheduled
# job's exit code, so `docker compose ps` alone never reflects a backup
# failure. This gives the container a real, `docker compose ps`-visible
# "unhealthy" signal instead: it fails whenever no successful backup
# exists newer than BBBFFL_BACKUP_MAX_AGE_HOURS, which is exactly what "the
# scheduled backup silently stopped succeeding" looks like from the
# filesystem, whether or not BBBFFL_ALERT_WEBHOOK_URL is configured. See
# docs/production-operations.md#scheduled-backups.

MAX_AGE_HOURS="${BBBFFL_BACKUP_MAX_AGE_HOURS:-26}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"

newest=$(find "$BACKUP_DIR" -maxdepth 1 -name 'bbbffl-*.dump' -mmin "-$((MAX_AGE_HOURS * 60))" -print -quit)
if [ -n "$newest" ]; then
    exit 0
fi
echo "no successful backup in ${BACKUP_DIR} newer than ${MAX_AGE_HOURS}h" >&2
exit 1
