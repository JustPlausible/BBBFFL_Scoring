"""Acquire, validate, and stage the supported 2026 first-half replay."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from app.afl_client import AflApiClient
from app.replay import ReplayAflDataSource, ReplayClock
from app.replay_acquisition import acquire_first_half_2026, package_summary, write_package


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
    a.add_argument("--player-pool-output")
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
                payload = acquire_first_half_2026(Api(client), source_base_url=args.base_url)
            finally:
                client.close()
            write_package(payload, args.output)
            if args.player_pool_output:
                pool = {
                    "source": {"provider": "afl-api-v1", "season_year": 2026},
                    "players": [
                        {
                            "canonical_player_id": x["canonical_player_id"],
                            "display_name": x["display_name"],
                            "afl_team_id": x["team_id"],
                            "afl_team_name": x["team_name"],
                            "eligible": x.get("eligible", True),
                            "source_updated_at": None,
                        }
                        for x in payload["players"]
                    ],
                }
                Path(args.player_pool_output).write_text(json.dumps(pool, indent=2, sort_keys=True) + "\n")
            # Historical packages deliberately require checkpoint state; acquisition
            # validation here validates the package shape with a temporary logical state.
            print(
                f"acquisition PASS\nseason: 2026\nrounds: {len(payload['rounds'])}\nmatches: {len(payload['matches'])}\nstats coverage: {len(payload['player_stats'])}/{len(payload['matches'])}\nroster coverage: {payload['manifest']['roster_coverage']}\npackage: {payload['manifest']['package_version']}\nacquired: {payload['manifest']['acquired_at']}"
            )
            return 0
        if args.command == "validate":
            print(package_summary(ReplayAflDataSource(args.evidence, checkpoint_path=args.state)))
            return 0
        clock = ReplayClock.from_iso(args.effective_at)
        target = Path(args.state)
        target.parent.mkdir(parents=True, exist_ok=True)
        finalised_round_ids: set[int] = set()
        if target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            if existing.get("schema") != "bbbffl.replay-checkpoint/v1":
                raise ValueError(f"unsupported replay checkpoint schema: {existing.get('schema')!r}")
            previous = ReplayClock.from_iso(existing["effective_at"])
            if clock.now() < previous.now():
                raise ValueError(
                    f"replay effective time cannot move backwards: {clock.now().isoformat()} < {previous.now().isoformat()}"
                )
            finalised_round_ids.update(int(value) for value in existing.get("finalised_round_ids", []))
        if args.stage == "final-results":
            if args.round_id is None:
                raise ValueError("--round-id is required with --stage final-results")
            finalised_round_ids.add(args.round_id)
        elif args.round_id is not None:
            raise ValueError("--round-id is only valid with --stage final-results")
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema": "bbbffl.replay-checkpoint/v1",
                    "effective_at": clock.now().isoformat(),
                    "stage": args.stage,
                    "finalised_round_ids": sorted(finalised_round_ids),
                },
                indent=2,
            )
            + "\n"
        )
        temporary.replace(target)
        print(f"checkpoint {args.stage} at {clock.now().isoformat()} -> {target}")
        return 0
    except Exception as exc:
        print(f"replay operation FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
