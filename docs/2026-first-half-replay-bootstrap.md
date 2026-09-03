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

4. Fill in the config's `opening_round` section (issue #126): the ten accepted 2026 Opening Round rules, one per participating AFL club, plus the local acquired replay evidence file used to validate them. This is the same evidence bundle acquired in the playbook's acquisition phase (`replay/2026-first-half/evidence/2026-first-half.json`); no separate file or live AFL-api reconnection is needed. The template already contains the genuine 2026 facts (Opening Round ID `1343`; compensating byes `1345`/`1346`/`1347` for the R2/R3/R4 target rounds) transcribed from `docs/opening-round-deferred-selection.md`'s evidence table -- do not invent or renumber club IDs, round IDs, or the R2/R3/R4 target split. Two fields still require operator replacement beyond the template's own genuine facts: adjust `evidence_file`'s path to the operator's actual directory layout, and **replace the template's deliberately invalid `afl_season_id: 0` placeholder** with the real AFL-api season identifier the acquired `evidence_file` declares for year 2026 -- read it from that file's `seasons` list; do not guess `2026`, since AFL-api's `season_id` is an opaque identifier that need not equal the calendar year.

## Opening Round rule activation (issue #126)

Bootstrap establishes the ten accepted 2026 Opening Round rules -- one per participating club (`BL`, `CARL`, `COLL`, `GCFC`, `GEEL`, `GWS`, `HAW`, `STK`, `SYD`, `WB`) -- through the ordinary `app.opening_round.OpeningRoundRuleRepository.accept()` domain semantics, inside the same transaction as the rest of bootstrap. It never inserts `opening_round_rule` rows with ad-hoc SQL and never creates a parallel rule table: the persisted rows are ordinary accepted rules, immediately consumable by `list_accepted_for_season()`, delegated Opening Round Operations, and the nomination/preload/scoring/lockout logic issue #69 already implements.

Acceptance is validated against the **local acquired replay evidence** (`opening_round.evidence_file`), never a live AFL-api client: `ReplayOpeningRoundEvidenceValidator` reads only that evidence file's `seasons`/`rounds` identity facts to confirm the configured AFL season and Opening Round/bye round IDs genuinely exist, exactly the same round-existence contract `OpeningRoundRuleRepository.accept()` already requires of `app.round_mapping.AflReferenceValidator`. Once evidence acquisition has produced that file, AFL-api can be disconnected before bootstrap runs at all.

`opening_round.afl_season_id` is AFL-api's own opaque season identifier for 2026, not necessarily the calendar year itself -- bootstrap cross-checks it against whichever season the evidence's own `seasons` list declares for year 2026 and fails closed on any mismatch, so read the genuine value from the acquired `evidence_file` rather than assuming it equals `2026`.

Each configured rule states an explicit `evidence_classification`. The AFL-side facts (Opening Round participation, compensating bye placement) are genuine `known_fact` evidence; the BBBFFL-side target-round mapping this replay operationalises for them is explicitly `reconstructable_behaviour` (replay/reconstructed behaviour), not an overstated historical `known_fact` -- this repository holds no historical BBBFFL nomination record (see `docs/opening-round-deferred-selection.md`'s evidence-boundary section).

Bootstrap creates **no** `opening_round_nomination` rows. Ownership does not exist before the draft, so player-level nominations are entered later, after picks are made, through the existing Opening Round Operations workflow (reached from its **Season Centre** link -- see `docs/2026-first-half-replay-playbook.md` section F, steps 6-7, and `docs/opening-round-deferred-selection.md`'s "Replay operator browser workflow") -- never inferred, preloaded, or invented here. Once bootstrap has accepted the ten rules, Season Centre's Opening Round Operations link becomes visible immediately, before any nomination exists. Its nomination-progress indicator (issue #131, `app.opening_round.build_opening_round_readiness`) counts *currently owned, rule-eligible players*, not accepted rules -- with no draft picks made yet, that denominator is genuinely `0/0` (reported ready, since there is nothing yet to complete), not `0/10`. The indicator only starts moving toward the expected `10/10` as the draft assigns ownership of each accepted club's player; do not read a `0/0` immediately after bootstrap as a defect.

Reconciliation follows #116's conservative philosophy: a clean database gets exactly ten accepted rules; an identical rerun is a no-op (no new revision, no duplicate); an existing accepted rule that materially differs from configuration fails closed without being silently corrected; an unexpected extra accepted rule for the replay season fails closed. `--readiness-only --json`'s `opening_round` section reports `expected_rule_count`, `accepted_rule_count`, `complete`, the Opening Round AFL identity, the R2/R3/R4 `targets` distribution, and `nomination_count` (always `0` pre-draft) -- overall `READY` requires `opening_round.complete: true` in addition to every existing prerequisite, but never requires any nomination. Draft Board readiness and `next_human_action: "Pick 1"` are unaffected by Opening Round state.

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

A successful final report says `READY` and shows rounds `[1..9]`, 10 entries, 10 accepted-order positions, the captured eligible-player count, the configured squad limit, `operator_authentication_provisioned: true`, `completed_draft_picks_exist: false`, `next_human_action: Pick 1`, and an `opening_round` section reporting `accepted_rule_count: 10`, `complete: true`, `targets: {"2": 4, "3": 4, "4": 2}`, and `nomination_count: 0`. The password is read from a transient environment variable, passed only to the existing credential hashing service, and must never be placed in replay JSON, shell history, logs, or source control. The accepted order necessarily materialises the Draft Board's future snake-draft slots; those are allocations, not completed/history picks.

Sign in normally as `operator_email` (no admin token is introduced), activate the Administrator role in the existing context picker, open Season Centre, select the 2026 season, then open its existing Draft Board. Confirm all ten genuine team/coach labels are in accepted order, eligible players are searchable, the current action is Pick 1, and completed history is empty. Then open Opening Round Operations for the same season and confirm all ten accepted rules are listed with zero nominations. A human may then make Pick 1; nominations are entered later, after the draft, once actual owned players exist.

## Reruns, conflicts, and safe reset

An identical rerun is a validated no-op and returns `READY` after credentials are provisioned. Any differing label, extra or changed rules version, stream, round, entry/coach/team identity, squad limit, canonical pool, provider fact, accepted position, or paused/finalized draft fails closed and the transaction rolls back; the bootstrap never guesses which operator data to repair or silently resumes a paused draft. The same applies to Opening Round rules: an existing accepted rule that materially conflicts with `opening_round.rules`, or an unexpected extra accepted rule for the season, fails closed in the same all-or-nothing transaction -- ordinary season/draft state and Opening Round rules always commit or roll back together.

There is deliberately no wipe command. For a rehearsal database that has never become authoritative, stop the app, verify the exact URL/path above, delete only `data/2026-first-half-replay.db`, rerun `alembic upgrade head`, and bootstrap again. Never delete a production/shared database. Once humans have made picks, reset means provisioning another isolated replay database, not erasing history.

## Troubleshooting

* `no such table`: run `alembic upgrade head` against the exact same `BBBFFL_DATABASE_URL`.
* Missing/ambiguous canonical or club identity: correct the upstream capture; never manufacture an ID.
* Too few eligible players: capture the complete supported 2026 AFL season pool or correct authoritative eligibility.
* Conflict on rerun: use `--readiness-only --json`, compare the named prerequisite with the operator config, and investigate the existing state. Do not edit SQL.
* `NOT READY` after a successful transaction: use the listed failed checks; the command exits non-zero and must not be followed by drafting.
* `AFL Opening Round reference does not exist` / `AFL compensating bye round reference does not exist`: `opening_round.evidence_file` does not contain the configured season/round identity -- point it at the genuine acquired evidence package (never a live AFL-api reconnection, and never a hand-edited evidence file).
* `opening_round.rules` shape errors (wrong count, duplicate/missing/extra club, wrong Opening Round ID, wrong bye/target pairing, wrong R2/R3/R4 distribution): correct the config against the genuine 2026 facts in `docs/opening-round-deferred-selection.md`; never adjust a club ID, round ID, or target round to make validation pass.

The older `scripts/replay_2026_draft.py` remains synthetic software/demo scaffolding and is not a supported source or bootstrap for this historical replay.
