# 2026 first-half replay bootstrap

This is the operator runbook section intended for inclusion in the full 2026 replay playbook. The command creates prerequisites only: it never selects, reserves, or auto-drafts a player.

## Prerequisites and explicit replay facts

1. Obtain the genuine historical coach names, login emails, BBBFFL team names, durable licence keys, accepted draft positions, authoritative squad limit, and identify one of those coaches as `operator_email`. That one account receives the Administrator role needed for this browser checkpoint; the other nine coaches do not need passwords merely to inspect Pick 1 readiness.
2. Copy `config/replay/2026-first-half.template.json` to an untracked operator file and replace **every** `REPLACE_`/`.invalid` value. The repository intentionally contains no invented historical identities.
3. Export/capture the project-supported `afl-api-v1` 2026 player data into the configured `player_pool_file`. Its format is:

```json
{
  "source": {"provider": "afl-api-v1", "season_year": 2026},
  "players": [
    {"canonical_player_id": 396, "display_name": "name from provider", "afl_team_id": 1001,
     "afl_team_name": "club from provider", "eligible": true, "source_updated_at": "provider timestamp or null"}
  ]
}
```

Canonical IDs, club IDs and names are mandatory. Duplicate/missing identities, a non-AFL API provider, a year mismatch, or a pool too small for ten complete squads fails before writes. The capture is operator evidence and should not be committed if its redistribution or personal-data status is unclear.

This command is intentionally restricted to `season.year: 2026` and a player source with `season_year: 2026`. It is not a generic season seeder; another year is rejected before any persistent write.

## Clean database through Pick 1

From `bbbffl_app`:

```bash
# Point this at the clean replay database used by the web process.
export BBBFFL_DATABASE_URL=sqlite:///$(pwd)/data/2026-first-half-replay.db
alembic upgrade head
python -m scripts.bootstrap_2026_first_half --config /secure/replay/2026-first-half.json
# The first command intentionally reports NOT READY until authentication is provisioned.
read -rsp 'Replay operator password: ' BBBFFL_REPLAY_OPERATOR_PASSWORD; echo
export BBBFFL_REPLAY_OPERATOR_PASSWORD
python -m scripts.bootstrap_2026_first_half --config /secure/replay/2026-first-half.json --provision-operator
unset BBBFFL_REPLAY_OPERATOR_PASSWORD
python -m scripts.bootstrap_2026_first_half --config /secure/replay/2026-first-half.json --readiness-only --json
BBBFFL_DATABASE_URL="$BBBFFL_DATABASE_URL" uvicorn app.main:app --reload
```

A successful final report says `READY` and shows rounds `[1..9]`, 10 entries, 10 accepted-order positions, the captured eligible-player count, the configured squad limit, `operator_authentication_provisioned: true`, `completed_draft_picks_exist: false`, and `next_human_action: Pick 1`. The password is read from a transient environment variable, passed only to the existing credential hashing service, and must never be placed in replay JSON, shell history, logs, or source control. The accepted order necessarily materialises the Draft Board's future snake-draft slots; those are allocations, not completed/history picks.

Sign in normally as `operator_email` (no admin token is introduced), activate the Administrator role in the existing context picker, open Season Centre, select the 2026 season, then open its existing Draft Board. Confirm all ten genuine team/coach labels are in accepted order, eligible players are searchable, the current action is Pick 1, and completed history is empty. A human may then make Pick 1.

## Reruns, conflicts, and safe reset

An identical rerun is a validated no-op and returns `READY` after credentials are provisioned. Any differing label, extra or changed rules version, stream, round, entry/coach/team identity, squad limit, canonical pool, provider fact, accepted position, or paused/finalized draft fails closed and the transaction rolls back; the bootstrap never guesses which operator data to repair or silently resumes a paused draft.

There is deliberately no wipe command. For a rehearsal database that has never become authoritative, stop the app, verify the exact URL/path above, delete only `data/2026-first-half-replay.db`, rerun `alembic upgrade head`, and bootstrap again. Never delete a production/shared database. Once humans have made picks, reset means provisioning another isolated replay database, not erasing history.

## Troubleshooting

* `no such table`: run `alembic upgrade head` against the exact same `BBBFFL_DATABASE_URL`.
* Missing/ambiguous canonical or club identity: correct the upstream capture; never manufacture an ID.
* Too few eligible players: capture the complete supported 2026 AFL season pool or correct authoritative eligibility.
* Conflict on rerun: use `--readiness-only --json`, compare the named prerequisite with the operator config, and investigate the existing state. Do not edit SQL.
* `NOT READY` after a successful transaction: use the listed failed checks; the command exits non-zero and must not be followed by drafting.

The older `scripts/replay_2026_draft.py` remains synthetic software/demo scaffolding and is not a supported source or bootstrap for this historical replay.
