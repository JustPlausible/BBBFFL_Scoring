"""Operator CLI for the deterministic 2026 first-half replay bootstrap."""

from __future__ import annotations

import argparse
import json
import os
import sys

from app.config import BASE_DIR
from app.db import connect
from app.replay_bootstrap import bootstrap_first_half, load_replay_config, replay_readiness


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="explicit replay JSON configuration")
    parser.add_argument(
        "--database-url", default=os.getenv("BBBFFL_DATABASE_URL", f"sqlite:///{BASE_DIR / 'data/scorer_decisions.db'}")
    )
    parser.add_argument("--readiness-only", action="store_true", help="make no writes; report current state")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()
    database = connect(args.database_url)
    try:
        config = load_replay_config(args.config)
        report = (
            replay_readiness(database, config.year) if args.readiness_only else bootstrap_first_half(database, config)
        )
    except Exception as exc:
        # A single operator boundary owns error presentation; transactions have
        # already rolled back. Never include credentials or player payloads.
        print(f"NOT READY: {exc}", file=sys.stderr)
        return 1
    finally:
        database.close()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{report['overall']}: {config.year} first-half replay")
        for key in (
            "logical_rounds",
            "season_entry_count",
            "accepted_draft_order_count",
            "player_pool_count",
            "squad_size_limit",
            "completed_draft_picks_exist",
            "next_human_action",
        ):
            print(f"  {key}: {report.get(key)}")
        for message in report.get("messages", []):
            print(f"  missing/conflicting: {message}")
    return 0 if report["overall"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
