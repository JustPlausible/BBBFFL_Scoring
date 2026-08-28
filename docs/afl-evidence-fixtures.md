# AFL evidence fixtures

**Issue:** [#40 — Curate deterministic AFL evidence fixtures](https://github.com/JustPlausible/BBBFFL_Scoring/issues/40)
(roadmap work package **08**, `docs/roadmap/2027-season-roadmap.md`)

**Depends on:** [#18 — Validate and pin `afl-api` v1 consumer contract](https://github.com/JustPlausible/BBBFFL_Scoring/issues/18)
(roadmap package 04) — see [`afl-api-v1-contract.md`](afl-api-v1-contract.md).

## Purpose

A live `afl-api` call is unsuitable as the *sole* evidence source for
BBBFFL's tests and for historical replay (roadmap packages 32–36): live
responses can be corrected, coverage can change, and an outage makes a test
non-repeatable. This corpus is a small, curated, provenance-rich set of
`afl-api` v1 evidence — season/round/match/player/player-stat payloads —
that BBBFFL's tests and later replay tooling can load deterministically,
with no network access, while still exercising the real
`app.afl_client.AflApiClient` parsing path.

**This corpus is evidence for testing/replay, never an operational data
source.** Nothing in `app/` reads it; production `AflApiClient` gains no
test-only branch from it. It never becomes a second AFL authority for live
2027 scoring, and it must never be treated as a substitute for a live
`afl-api` deployment when one is reachable.

**Relationship to `tests/fixtures/afl_api_v1/` (issue #18):** that directory
is a separate, smaller corpus that pins BBBFFL's supported subset of the
raw `afl-api` v1 *contract* (proven by `tests/test_afl_contract_v1.py`) and
is out of scope for this document's classification/provenance/versioning
system. Do not add package-08 evidence there, and do not point replay
tooling at it — use `tests.afl_evidence` (this document) instead.

## Directory and manifest convention

```
tests/fixtures/afl_evidence/
  README.md
  v1/                                    <- afl-api contract version
    captured/                            <- classification: captured
      README.md                          <-   (empty; see "Adding a new capture")
    captured_bbbffl_historical/          <- classification: captured_bbbffl_historical
      README.md                          <-   (empty; see below)
    synthetic/                           <- classification: synthetic
      season_85/
        season.json                      <- GET /api/v1/seasons
        rounds.json                      <- GET /api/v1/seasons/{id}/rounds
        round_1500/
          matches.json                   <- GET /api/v1/rounds/1500/matches
          match_9501/
            player_stats.json            <- GET /api/v1/matches/9501/player-stats
          match_9502/
            player_stats.json
          match_9503/
            player_stats.json
        players/
          player_9701.json               <- GET /api/v1/players/9701
    unresolved/                          <- classification: unresolved
      season_85/
        round_1500/
          match_9503/
            participation_9706.json
```

The path alone tells a reader the **contract version** (`v1/`), the
**evidence classification** (the directory directly under it), the
**season** (`season_<id>/`), the **round** (`round_<id>/`, for
round-scoped evidence), the **match** (`match_<id>/`, for match-scoped
evidence), and the **endpoint/payload type** (the file name). A resource
that is not season/round/match-scoped in the real endpoint's URL (e.g.
player detail) lives outside those directories rather than being forced
into a misleading path.

Every fixture is loaded and validated against this convention by
`tests.afl_evidence` (see below) — a fixture filed under the wrong
classification directory, or whose `season_id`/`round_id`/`match_id` don't
match its own path, fails to load rather than being silently trusted.

### The fixture file itself

Every fixture is one JSON file containing a small envelope:

```json
{
  "provenance": { "...": "see below" },
  "response": { "...": "the afl-api v1 response shape, byte-for-byte" }
}
```

or, for an `unresolved` scorer-ruling note that has no `afl-api` response
of its own:

```json
{
  "provenance": { "...": "..." },
  "facts": { "requires_scorer_ruling": true, "...": "..." }
}
```

**Provenance travels embedded in the same file as the payload it
describes.** This is deliberate: issue #40 requires provenance to "remain
attached to the fixture if files are later reorganised or copied", and an
embedded envelope survives a plain file copy/move with no side-car index to
keep in sync.

`response` is the real `afl-api` v1 response shape, unmodified except for
sanitisation (never a simplified, fixture-only representation) — the same
JSON `app.afl_client.AflApiClient` would receive from a live deployment, so
a fixture drifting from the real contract is exactly what
`tests/test_afl_evidence.py`'s validation and `tests/test_afl_contract_v1.py`
(issue #18) are positioned to catch.

### Provenance fields

| Field | Meaning |
| --- | --- |
| `schema_version` | Envelope format version (currently `1`). |
| `fixture_id` | Stable, human-readable identity for this fixture. |
| `classification` | One of the four evidence classifications below. |
| `derivation` | Where the data actually came from — see the compatibility table below. |
| `contract_version` | The `afl-api` contract version this evidence targets (`v1`). |
| `endpoint` | The `afl-api` endpoint this is a response for (`null` for a `facts` note). |
| `endpoint_kind` | One of `seasons`/`rounds`/`matches`/`player_stats`/`player_detail`/`scorer_ruling_note`. |
| `request_params` | The request parameters that produced this response. |
| `season_id` / `round_id` / `match_id` | Relevant `afl-api` identifiers, or `null` when not applicable. |
| `canonical_player_ids` | Canonical `afl-api` player IDs this fixture concerns. |
| `captured_at` | Real capture timestamp — **required** for `classification: "captured"`, **must be null** otherwise. |
| `authored_at` | When this fixture file was written (always required). |
| `source` | Where the data came from: an `afl-api` source/doc citation, a description of the real capture, or (for a BBBFFL-recorded fixture) the BBBFFL record referenced. |
| `notes` | A real explanation of what this fixture demonstrates and why it looks the way it does. Never blank/placeholder. |
| `supersedes` / `superseded_by` | Cross-links to a corrected/prior version of this evidence (see "Recording an upstream correction" below); `null` when not applicable. |

## Evidence classifications

Four classifications, matching issue #40's evidence policy exactly. A
fixture's classification is a **structured field checked by
`tests.afl_evidence`**, never inferred from a filename by a human or a
test:

| Classification | Meaning | Allowed `derivation` |
| --- | --- | --- |
| `captured` | A real, recorded `afl-api` v1 response from a live deployment. | `live_capture` only |
| `captured_bbbffl_historical` | BBBFFL's own historical operational evidence (a past round's persisted scoring/lockout/lineup state), kept distinct from afl-api's own record. | `bbbffl_recorded` only |
| `synthetic` | Not a real recorded response. Either reproduced from the `AFL-api` project's own source/docs (`afl_api_source_derived`) to stay contract-accurate, or a hand-authored edge case (`hand_authored_edge_case`) exercising a specific semantic (e.g. a null-vs-zero stat). | `afl_api_source_derived`, `hand_authored_edge_case` |
| `unresolved` | A genuinely unresolved historical fact (e.g. "did this selected player actually take the field?") that afl-api's own contract does not settle and that replay/scoring code must surface to a **scorer-owned ruling**, never guess. | `hand_authored_edge_case` only |

`tests/afl_evidence.py` enforces the classification ↔ derivation mapping
above as a hard validation rule — a fixture cannot claim `captured` unless
its `derivation` honestly says `live_capture`. **This is the mechanical
guardrail behind "do not fabricate captured AFL evidence":** it is not
merely a naming convention a reviewer might miss.

`captured` and `captured_bbbffl_historical` are **currently empty** in this
repository (each holds only a `README.md` explaining why) — this
environment's outbound network access to the configured `afl-api`
deployment is blocked by organisation egress policy (the same, still-open
restriction recorded in [`afl-api-v1-contract.md`](afl-api-v1-contract.md)'s
"Live validation status" for issue #18), so no genuine live capture was
possible here, and fabricating one would violate the rule above. See
"Adding a new capture" below for the procedure once network access exists.

## Loader usage

Load fixtures **only** through `tests/afl_evidence.py` — never by reading a
path directly from test code, so every read gets the same
validation/provenance guarantees.

```python
from tests import afl_evidence

# Load one fixture by its relative path under tests/fixtures/afl_evidence/.
fixture = afl_evidence.load("v1/synthetic/season_85/round_1500/match_9503/player_stats.json")
fixture.provenance.classification   # "synthetic"
fixture.provenance.notes            # why this fixture looks the way it does
fixture.response                    # the raw afl-api v1 response shape

# Load and validate every committed fixture (used by tests.afl_evidence's
# own corpus-wide test; also handy for future replay tooling).
all_fixtures = afl_evidence.iter_all()

# Build a real AflApiClient wired to curated evidence via httpx.MockTransport
# -- the production adapter's own parsing code runs unchanged, and no
# socket can ever be opened through this client.
client = afl_evidence.build_client({
    "/api/v1/rounds/1500/matches": "v1/synthetic/season_85/round_1500/matches.json",
    "/api/v1/matches/9503/player-stats": "v1/synthetic/season_85/round_1500/match_9503/player_stats.json",
})
matches = client.get_matches(1500)          # real Match dataclasses
stats = client.get_match_player_stats(9503)  # real PlayerStatLine dataclasses
client.close()

# A MatchFactsProvider (app.lockouts) backed by curated evidence, for a
# lockout/replay scenario:
match_facts = afl_evidence.RoundMatchFacts(client, afl_round_id=1500)
```

`load()` raises `EvidenceNotFoundError` for a missing fixture and
`EvidenceValidationError` (with the offending file's path in the message)
for anything malformed or incompatible — never a silently-accepted partial
result. `build_client()` reuses the exact same offline `httpx.MockTransport`
seam `tests/test_afl_contract_v1.py` uses for issue #18's fixtures.

## Validation behaviour

Every `load()` call (and therefore every `iter_all()` and `build_client()`
call) structurally validates the fixture:

- all required provenance fields are present;
- `classification` is one of the four values above, and `derivation` is one
  the classification actually allows (see the table above);
- `captured_at` is set if and only if `classification == "captured"`;
- the file's path matches its own `contract_version`/classification, and
  its `season_id`/`round_id`/`match_id` (when set) match the corresponding
  `season_<id>`/`round_<id>`/`match_<id>` path segments;
- an `unresolved`/`facts` fixture sets `endpoint: null`,
  `endpoint_kind: "scorer_ruling_note"`, and `facts.requires_scorer_ruling: true`;
  every other fixture has exactly one of `response`/`facts` and a real
  `afl-api`-shaped `response` for its `endpoint_kind` (required wrapper
  key(s) and fields per `seasons`/`rounds`/`matches`/`player_stats`/
  `player_detail` — e.g. `matches` entries need `match_id`, `status`,
  `home_team{team_id,name}`, `away_team{team_id,name}`);
- no field anywhere in the file looks like a credential/secret (`api_key`,
  `x-api-key`, `token`, `authorization`, `password`, `secret`, `bearer`,
  case/punctuation-insensitive).

Any failure raises `EvidenceValidationError` — a malformed or incompatible
fixture fails explicitly, it is never silently accepted. See
`tests/test_afl_evidence.py` for the negative-case tests covering each rule
above.

## Network independence

- `tests.afl_evidence.build_client()` wires a real `AflApiClient` to
  `httpx.MockTransport`, which never opens a socket — a test built this way
  **cannot** reach a live `afl-api` deployment even by accident.
- `tests/test_afl_evidence.py::test_build_client_wires_the_real_afl_api_client_with_no_socket_possible`
  asserts the client's transport actually is a `MockTransport`, as a
  regression guard against a future edit accidentally wiring a real one.
- No fixture-loading code lives in `app/` — production `AflApiClient`
  behaves identically whether or not this corpus exists.

## Adding or refreshing evidence

### Adding a new capture

Once an environment with network access to a real `afl-api` deployment is
available:

1. Make a **read-only** request to the endpoint you need (reuse
   `scripts/afl_contract_diagnostic.py`'s connection settings/pattern where
   convenient — never a mutating request).
2. Save the exact response body under `v1/captured/<season/round/match path
   per the convention above>/<endpoint_kind>.json`, wrapped in the
   `{"provenance": {...}, "response": {...}}` envelope.
3. Set `classification: "captured"`, `derivation: "live_capture"`, and a
   real `captured_at` timestamp. Fill in every other provenance field
   honestly, including `request_params` and `canonical_player_ids`.
4. Scrub the response for anything private/unnecessary (there should be
   none in a public `/api/v1` response) and double-check no header/query
   credential leaked into `request_params` or anywhere else in the file.
5. Add it to `tests.afl_evidence`-based tests as needed; run
   `tests/test_afl_evidence.py` to confirm it validates.

### Recording an upstream correction

**Never edit an existing committed fixture merely to make a failing
historical replay pass.** If later evidence shows a captured fixture was
wrong, or upstream data has since been corrected:

1. Leave the original file exactly as committed.
2. Add the new/corrected evidence as a **new file** (e.g.
   `player_stats.v2.json` beside the original `player_stats.json`), with
   its own complete provenance.
3. Set the new fixture's `supersedes` to the original fixture's
   `fixture_id`, and update the original's `superseded_by` to point at the
   new one — both files stay committed and loadable.
4. Explain the discrepancy in the new fixture's `notes` field.
5. Let replay/scorer logic decide which version governs a specific
   historical ruling — this corpus records both facts, it does not decide
   between them.

This way a fixture update is always a **reviewable repository change**
(a new file plus a diff to `superseded_by`) rather than a tool silently
replacing history.

### Populating `captured_bbbffl_historical`

Follow the same "new file, real provenance, never edit history" approach,
using `derivation: "bbbffl_recorded"` and citing the BBBFFL record (e.g. a
persisted `bbbffl_matchup_calculation` snapshot) the fixture was taken
from.

## Known limitations and follow-up

- `captured` and `captured_bbbffl_historical` are empty in this change —
  this environment cannot reach a live `afl-api` deployment (see
  [`afl-api-v1-contract.md`](afl-api-v1-contract.md)'s "Live validation
  status" for the same restriction against issue #18) and no BBBFFL round
  has produced historical evidence yet. This is a documented environment
  gap, not a design gap; the classification/loader/validation
  infrastructure above already supports both once real evidence exists.
- This is deliberately the **smallest representative** 2026 evidence set
  package 08 asks for (one round, three matches, a handful of players) —
  extending it with more rounds/cases as roadmap packages 32–36's replay
  checkpoints require is expected, following the same convention and
  "never edit committed evidence" rule.
- No complete-season replay harness is built here; roadmap packages 32–36
  consume this foundation.
