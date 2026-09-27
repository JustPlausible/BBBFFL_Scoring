#!/bin/sh
# Shared logging/alerting helper for BBBFFL production scripts (issue #243).
# POSIX sh -- sourced unmodified by both the backup container's Alpine
# busybox `sh` and readiness_watch.sh running directly on the host.
#
# Never pass a secret as $1/$message: it is logged verbatim and, when
# BBBFFL_ALERT_WEBHOOK_URL is configured, included verbatim in the webhook
# payload BBBFFL POSTs out. Every caller in this directory only ever passes
# a fixed, secret-free description of what failed (see
# docs/production-operations.md#alerting).

bbbffl_log() {
    level="$1"
    shift
    printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$level" "$*"
}

bbbffl_alert() {
    message="$1"
    bbbffl_log "CRITICAL" "$message"
    if [ -n "${BBBFFL_ALERT_WEBHOOK_URL:-}" ]; then
        # A caller (readiness_watch.sh) may embed the readiness endpoint's
        # own JSON response body in $message, which itself contains double
        # quotes -- escaping backslashes and quotes (and collapsing any
        # newline, which JSON strings cannot contain literally) here keeps
        # the outer payload valid JSON regardless of what the message
        # contains, rather than only for the fixed, simple messages this
        # directory's other scripts happen to pass today.
        escaped=$(printf '%s' "$message" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr '\n' ' ')
        payload=$(printf '{"text":"BBBFFL: %s"}' "$escaped")
        if ! wget -q -T 10 -O /dev/null \
            --header="Content-Type: application/json" \
            --post-data="$payload" \
            "$BBBFFL_ALERT_WEBHOOK_URL"; then
            bbbffl_log "WARNING" "alert webhook delivery failed (BBBFFL_ALERT_WEBHOOK_URL unreachable)"
        fi
    fi
}
