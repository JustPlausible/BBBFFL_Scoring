#!/bin/sh
set -eu

# Entrypoint for compose.production.yaml's "backup" service (issue #243).
# Installs a crontab entry from BBBFFL_BACKUP_SCHEDULE, then runs Alpine's
# busybox `crond` in the foreground as this container's PID 1 -- `exec` so
# it receives the container's stop signal directly rather than a wrapper
# shell swallowing it.
#
# Why this captures the environment into a file: busybox crond does not
# inherit the environment a cron job's *container* was started with (a
# well-known Alpine/busybox behaviour, not a BBBFFL choice) -- a job it
# runs otherwise sees none of docker-compose's env_file:-injected
# POSTGRES_*/BBBFFL_* variables at all. This captures them once per
# container start into a root-only (chmod 600), never-volume-persisted
# file the cron job sources immediately before running the backup script,
# so a rotated/regenerated secret is only ever picked up on the next
# container start/restart -- exactly the same lifecycle every other
# env_file:-sourced secret in this deployment already has.

ENV_FILE=/etc/bbbffl-backup.env
umask 077
: >"$ENV_FILE"
# Each value is single-quoted (embedded single quotes escaped) before being
# written, not copied verbatim from `env` -- BBBFFL_BACKUP_SCHEDULE's value
# ("15 2 * * *") contains spaces, and an unquoted `env`-format line breaks
# when later sourced with `.` (the shell would try to run "2", "*", "*",
# "*" as separate commands after the first space).
#
# Written as `export NAME='value'`, not bare `NAME='value'` (issue #243
# review): the crontab line below sources this file with `.` and then runs
# backup_postgres.sh as a *separate* process on the same line -- `.`
# alone only sets plain shell variables in the sourcing shell, which are
# never inherited by a subsequently exec'd child process, only genuinely
# exported ones are. Without `export`, that child would see none of these
# (its own PGDATABASE/PGUSER/PGPASSWORD defaulting would then all resolve
# empty), and only PGHOST -- set inline on the crontab line itself -- would
# actually reach it. A rehearsal that instead sources this file in an
# interactive shell before invoking the script directly (as this issue's
# own rehearsal did) would not catch this: that shell's env is already the
# container's full env regardless of what sourcing exports.
env | grep -E '^(POSTGRES_|PG|BBBFFL_)' | cut -d= -f1 | while IFS= read -r name; do
    value=$(eval "printf '%s' \"\$$name\"")
    escaped=$(printf '%s' "$value" | sed "s/'/'\\\\''/g")
    printf "export %s='%s'\n" "$name" "$escaped" >>"$ENV_FILE"
done
chmod 600 "$ENV_FILE"

SCHEDULE="${BBBFFL_BACKUP_SCHEDULE:-15 2 * * *}"
RETENTION_DAYS="${BBBFFL_BACKUP_RETENTION_DAYS:-14}"

# Cron job output is redirected to this container's own stdout/stderr
# (/proc/1/fd/*) so `docker compose logs backup` shows every backup run --
# busybox crond otherwise only logs to syslog, which nothing in this
# deployment collects.
cat >/etc/crontabs/root <<EOF
$SCHEDULE . $ENV_FILE; PGHOST=database /scripts/backup_postgres.sh >>/proc/1/fd/1 2>>/proc/1/fd/2
EOF

echo "[backup] ready: schedule='${SCHEDULE}' retention_days='${RETENTION_DAYS}' backup_dir=/backups"
exec crond -f -l 2
