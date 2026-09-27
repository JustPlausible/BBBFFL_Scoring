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

# POSTs a JSON payload, trying curl first (present on typical Linux hosts --
# readiness_watch.sh already requires it for its own readiness check) and
# falling back to wget (present in the Alpine-based backup container,
# which has no curl). Neither this file nor its callers can assume only one
# of these is installed, since this same file is sourced in both places.
_bbbffl_http_post_json() {
    url="$1"
    payload="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fsS -m 10 -H 'Content-Type: application/json' -d "$payload" -o /dev/null "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 10 -O /dev/null --header='Content-Type: application/json' --post-data="$payload" "$url"
    else
        bbbffl_log "WARNING" "neither curl nor wget is available; cannot deliver webhook"
        return 1
    fi
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
        if ! _bbbffl_http_post_json "$BBBFFL_ALERT_WEBHOOK_URL" "$payload"; then
            bbbffl_log "WARNING" "alert webhook delivery failed (BBBFFL_ALERT_WEBHOOK_URL unreachable)"
        fi
    fi
}
