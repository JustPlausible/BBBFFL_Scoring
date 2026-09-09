"""Acquire, validate, and stage the supported 2026 second-half (AFL R10-20)
replay evidence package.

This mirrors `scripts/first_half_replay.py`'s acquire/validate/checkpoint
triad and reuses its acquisition/domain boundaries end to end
(`app.replay_acquisition`, `app.replay.ReplayAflDataSource`) -- only the
round selection (AFL R10-20 inclusive, no Opening Round) and package
identity differ. See `docs/2026-second-half-replay-playbook.md` section E
for the exact Docker-based operator commands.

Deliberately does not offer a `--player-pool-output` flag. The Phase 2
working installation already carries the verified season-wide
`2026-player-pool.json` from the first-half acquisition/bootstrap; nothing
in the second-half playbook re-bootstraps a season player pool from a file,
so a second output here would only invite an operator to point it at the
existing pool file and silently replace the established source of truth.
A genuine need to refresh season membership is a separate, explicitly
justified action, not a side effect of acquiring round evidence.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from app.afl_client import AflApiClient
from app.replay_acquisition import (
    SECOND_HALF_ROUND_NUMBERS,
    acquire_second_half_2026,
    apply_checkpoint,
    package_summary,
    validate_replay_package,
    write_package,
)

PACKAGE_VERSION = "bbbffl.second-half/v1"
AFL_SEASON = 2026


class Api:
    def __init__(self, client):
        self.client = client

    def get(self, path):
        return self.client._get(path)  # exporter uses the same authenticated transport/contract parsing seam


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("acquire")
    a.add_argument("--output", required=True)
    a.add_argument("--base-url", default=os.getenv("AFL_API_BASE_URL"))
    a.add_argument("--api-key", default=os.getenv("AFL_API_KEY"))
    v = sub.add_parser("validate")
    v.add_argument("--evidence", required=True)
    v.add_argument("--state", required=True)
    c = sub.add_parser("checkpoint")
    c.add_argument("--state", required=True)
    c.add_argument("--effective-at", required=True)
    c.add_argument("--stage", choices=("scheduled", "final-results"), default="scheduled")
    c.add_argument("--round-id", type=int, help="AFL round whose final results are released at final-results")
    args = p.parse_args()
    try:
        if args.command == "acquire":
            if not args.base_url:
                p.error("--base-url or AFL_API_BASE_URL is required")
            client = AflApiClient(args.base_url, args.api_key)
            try:
                payload = acquire_second_half_2026(Api(client), source_base_url=args.base_url)
            finally:
                client.close()
            # A failure here never reports PASS and never writes a partial
            # output -- write_package stages the file (temp file + replace)
            # only after the full payload above has been built and
            # validated, matching first_half_replay.py's acquisition
            # failure-safety guarantee.
            write_package(payload, args.output)
            print(
                "acquisition PASS\n"
                f"season: {AFL_SEASON}\n"
                f"rounds: {len(payload['rounds'])}\n"
                f"matches: {len(payload['matches'])}\n"
                f"stats coverage: {len(payload['player_stats'])}/{len(payload['matches'])}\n"
                f"roster coverage: {payload['manifest']['roster_coverage']}\n"
                f"package: {payload['manifest']['package_version']}\n"
                f"acquired: {payload['manifest']['acquired_at']}"
            )
            return 0
        if args.command == "validate":
            source = validate_replay_package(
                args.evidence,
                args.state,
                expected_package_version=PACKAGE_VERSION,
                expected_afl_season=AFL_SEASON,
                expected_round_numbers=SECOND_HALF_ROUND_NUMBERS,
            )
            print(package_summary(source))
            return 0
        payload = apply_checkpoint(args.state, effective_at=args.effective_at, stage=args.stage, round_id=args.round_id)
        print(f"checkpoint {payload['stage']} at {payload['effective_at']} -> {Path(args.state)}")
        return 0
    except Exception as exc:
        print(f"replay operation FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
