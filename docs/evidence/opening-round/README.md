# Opening Round AFL-side evidence (issue #69)

Three raw, unmodified captures supplied for issue #69, preserved verbatim
(pretty-printed only) rather than reconstructed from assumptions:

| File | AFL season | Source request |
|---|---|---|
| `rounds-2024.json` | 2024 | `GET https://aflapi.afl.com.au/afl/v2/compseasons/{{compSeasonId}}/rounds?pageSize=30` |
| `rounds-2025.json` | 2025 | same endpoint, 2025 `compSeasonId` |
| `rounds-2026.json` | 2026 | same endpoint, 2026 `compSeasonId` |

## What this is (and is not)

This is a capture of the **upstream AFL data source** (`aflapi.afl.com.au`,
via Bruno/CFS), not a response from BBBFFL's own normalised `afl-api` v1
consumer contract (`app.afl_client.AflApiClient`, documented in
[`../../afl-api-v1-contract.md`](../../afl-api-v1-contract.md)). The field
names differ (e.g. `roundNumber`/`utcStartTime`/`byes[].id` here vs.
`round_number`/`start_time`/`byes[].team_id` in `afl-api` v1), and this
corpus is deliberately **not** loaded through `tests.afl_evidence`
(see [`../../afl-evidence-fixtures.md`](../../afl-evidence-fixtures.md)),
whose `captured` classification is reserved for genuine `afl-api` v1
response bodies. Reshaping these into that contract's shape would risk
silently fabricating an `afl-api` v1 "capture" that was never actually
served by that API.

Instead, these files are the **documentary evidence** the historical
mapping table in
[`../../opening-round-deferred-selection.md`](../../opening-round-deferred-selection.md)
is built from, and `tests/opening_round_evidence.py` distils the specific
facts (Opening Round identity, participating clubs, compensating bye
rounds) they establish into plain Python constants cited by
`tests/test_opening_round.py` -- never re-derived from assumptions, and
never silently reshaped into a different contract's fixture format.

## What each file establishes

For each season, round `roundNumber: 0` (`"name": "Opening Round"`) lists,
in its own `byes` array, every AFL club **not** participating in Opening
Round that year. The participating clubs are therefore every club *not*
listed there. Later ordinary rounds' own `byes` arrays show which of those
participating clubs received their compensating bye in which round --
cross-referenced against BBBFFL's own historical record (where available)
in the mapping table linked above.

These files establish AFL-side facts only: which clubs played in Opening
Round, and which AFL round carried each club's later bye. They do **not**
establish any BBBFFL-side fact (a specific historical nomination, the
coach who made it, the BBBFFL round/slot it targeted) -- see
`docs/opening-round-deferred-selection.md`'s evidence-boundary section.
