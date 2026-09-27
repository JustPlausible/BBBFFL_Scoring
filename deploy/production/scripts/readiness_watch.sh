#!/bin/sh
set -eu

# Host-run readiness watchdog for BBBFFL production (issue #243). Polls
# GET /health/ready and alerts (see lib_alert.sh) when it does not return
# HTTP 200 -- the practical, reproducible alerting path for "a critical
# dependency (database or afl-api) is down" that a log nobody tails would
# otherwise miss. See docs/production-operations.md#alerting for the exact
# host cron/systemd-timer entry that runs this on a schedule.
#
# Deliberately runs on the host, not inside a BBBFFL container -- it keeps
# working even if the app container itself is unhealthy or restarting, and
# it exercises the same public HTTPS path (through the reverse proxy) real
# traffic uses, not an internal shortcut.
#
# Requires curl. Usage:
#   BBBFFL_READY_URL=https://bbbffl.example.com/health/ready ./readiness_watch.sh

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=./lib_alert.sh
. "$SCRIPT_DIR/lib_alert.sh"

READY_URL="${BBBFFL_READY_URL:?BBBFFL_READY_URL must be set, e.g. https://bbbffl.example.com/health/ready}"

BODY=$(mktemp)
trap 'rm -f "$BODY"' EXIT

STATUS=$(curl -sS -m 10 -o "$BODY" -w '%{http_code}' "$READY_URL" 2>/dev/null) || STATUS="000"

if [ "$STATUS" = "200" ]; then
    bbbffl_log "INFO" "readiness OK (${READY_URL})"
else
    DETAIL=$(tr -d '\n' <"$BODY" 2>/dev/null | cut -c1-500)
    bbbffl_alert "readiness check FAILED (${READY_URL}, HTTP ${STATUS}) -- body: ${DETAIL}"
    exit 1
fi
