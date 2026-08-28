"""Deterministic, provenance-rich AFL evidence fixture loader (issue #40 /
roadmap package 08).

This module is the *only* supported way test code reads
`tests/fixtures/afl_evidence/`. It is deliberately test-only support code
(not under `app/`): production `AflApiClient` (app/afl_client.py) gains no
test-only branch or fixture awareness from this issue -- see
`docs/afl-evidence-fixtures.md` for the full design rationale, directory
convention, and refresh/addition policy.

Every fixture file is a small JSON envelope:

    {"provenance": {...}, "response": {...}}   -- a captured/synthetic
                                                   afl-api endpoint response
  or
    {"provenance": {...}, "facts": {...}}      -- an "unresolved" scorer-
                                                   ruling note with no
                                                   afl-api response of its
                                                   own

Provenance travels embedded in the same file as the payload it describes,
specifically so it survives a later file copy/reorganisation (issue #40's
"provenance must remain attached to the fixture") without depending on a
side-car index staying in sync.

`load()`/`iter_all()` validate structurally on every read -- a malformed or
incompatible fixture raises `EvidenceValidationError` rather than being
silently accepted. `build_client()` wires curated evidence into a real
`AflApiClient` via `httpx.MockTransport`, so callers exercise the actual
production parsing path with no socket ever opened.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.afl_client import AflApiClient

FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "afl_evidence"

CLASSIFICATIONS = ("captured", "captured_bbbffl_historical", "synthetic", "unresolved")

# Directory segment (immediately under v1/) each classification is stored
# under. Kept as an explicit mapping (rather than assuming classification
# name == directory name) so the two are checked against each other, not
# merely both hard-coded once.
_CLASSIFICATION_DIR = {
    "captured": "captured",
    "captured_bbbffl_historical": "captured_bbbffl_historical",
    "synthetic": "synthetic",
    "unresolved": "unresolved",
}

# A classification may only be claimed by evidence whose `derivation`
# honestly describes where it came from. In particular: nothing may be
# labelled "captured" (a real recorded afl-api response) unless its
# derivation says so -- this is the mechanical guardrail behind issue #40's
# "do not fabricate captured AFL evidence".
_ALLOWED_DERIVATIONS = {
    "captured": {"live_capture"},
    "captured_bbbffl_historical": {"bbbffl_recorded"},
    "synthetic": {"afl_api_source_derived", "hand_authored_edge_case"},
    "unresolved": {"hand_authored_edge_case"},
}

_ENDPOINT_KINDS = frozenset({"seasons", "rounds", "matches", "player_stats", "player_detail", "scorer_ruling_note"})

_REQUIRED_PROVENANCE_KEYS = frozenset(
    {
        "schema_version",
        "fixture_id",
        "classification",
        "derivation",
        "contract_version",
        "endpoint",
        "endpoint_kind",
        "request_params",
        "season_id",
        "round_id",
        "match_id",
        "canonical_player_ids",
        "captured_at",
        "authored_at",
        "source",
        "notes",
        "supersedes",
        "superseded_by",
    }
)

# Key names (after stripping punctuation/case) that must never appear
# anywhere in a committed fixture -- issue #40's "no credentials/secrets"
# requirement, enforced mechanically rather than by review alone.
_FORBIDDEN_KEY_NAMES = frozenset(
    {"apikey", "xapikey", "token", "accesstoken", "secret", "password", "authorization", "bearer"}
)


class EvidenceError(RuntimeError):
    """Base class for this module's fixture loading/validation failures."""


class EvidenceNotFoundError(EvidenceError):
    """No fixture file exists at the requested path."""


class EvidenceValidationError(EvidenceError):
    """A fixture file exists but is malformed or fails a provenance/shape
    check. Always includes the offending file's path in its message."""


@dataclass(frozen=True)
class Provenance:
    """Everything issue #40 requires BBBFFL to be able to say about one
    piece of curated evidence. See this module's docstring for the on-disk
    envelope this is parsed from."""

    fixture_id: str
    classification: str
    derivation: str
    contract_version: str
    endpoint: str | None
    endpoint_kind: str
    request_params: dict
    season_id: int | None
    round_id: int | None
    match_id: int | None
    canonical_player_ids: tuple[int, ...]
    captured_at: str | None
    authored_at: str
    source: dict
    notes: str
    supersedes: str | None
    superseded_by: str | None

    @staticmethod
    def from_dict(raw: dict) -> "Provenance":
        return Provenance(
            fixture_id=raw["fixture_id"],
            classification=raw["classification"],
            derivation=raw["derivation"],
            contract_version=raw["contract_version"],
            endpoint=raw["endpoint"],
            endpoint_kind=raw["endpoint_kind"],
            request_params=dict(raw["request_params"]),
            season_id=raw["season_id"],
            round_id=raw["round_id"],
            match_id=raw["match_id"],
            canonical_player_ids=tuple(raw["canonical_player_ids"]),
            captured_at=raw["captured_at"],
            authored_at=raw["authored_at"],
            source=dict(raw["source"]),
            notes=raw["notes"],
            supersedes=raw["supersedes"],
            superseded_by=raw["superseded_by"],
        )


@dataclass(frozen=True)
class EvidenceFixture:
    path: Path
    relative_path: str
    provenance: Provenance
    response: Any | None
    facts: dict | None


def _normalise_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _scan_for_secrets(node: Any, where: str) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if _normalise_key(str(key)) in _FORBIDDEN_KEY_NAMES:
                raise EvidenceValidationError(
                    f"{where}: field {key!r} looks like a credential/secret -- "
                    "fixtures must never carry API keys, tokens, or request secrets"
                )
            _scan_for_secrets(value, where)
    elif isinstance(node, list):
        for item in node:
            _scan_for_secrets(item, where)


def _require(condition: bool, where: str, message: str) -> None:
    if not condition:
        raise EvidenceValidationError(f"{where}: {message}")


def _validate_provenance(where: str, raw: Any, path_parts: tuple[str, ...]) -> None:
    _require(isinstance(raw, dict), where, "'provenance' must be an object")
    missing = _REQUIRED_PROVENANCE_KEYS - set(raw)
    _require(not missing, where, f"provenance missing required key(s): {sorted(missing)}")

    classification = raw["classification"]
    _require(
        classification in CLASSIFICATIONS,
        where,
        f"unknown classification {classification!r}; must be one of {CLASSIFICATIONS}",
    )
    derivation = raw["derivation"]
    allowed = _ALLOWED_DERIVATIONS[classification]
    _require(
        derivation in allowed,
        where,
        f"classification {classification!r} requires derivation in {sorted(allowed)}, got {derivation!r} "
        "(this mismatch is exactly what would let synthetic data be mislabelled as captured evidence)",
    )
    if classification == "captured":
        _require(raw["captured_at"] is not None, where, "classification 'captured' requires a non-null captured_at")
    else:
        _require(raw["captured_at"] is None, where, f"classification {classification!r} must not set captured_at")

    _require(raw["endpoint_kind"] in _ENDPOINT_KINDS, where, f"unknown endpoint_kind {raw['endpoint_kind']!r}")
    _require(bool(raw["fixture_id"]), where, "fixture_id must be non-empty")
    _require(
        bool(raw["notes"]) and len(raw["notes"]) > 10,
        where,
        "notes must be a non-trivial explanation, not empty/placeholder",
    )
    _require(bool(raw["authored_at"]), where, "authored_at is required")

    expected_dir = _CLASSIFICATION_DIR[classification]
    _require(
        len(path_parts) >= 2 and path_parts[0] == raw["contract_version"] and path_parts[1] == expected_dir,
        where,
        f"path must be under {raw['contract_version']}/{expected_dir}/ for classification {classification!r}, "
        f"got {'/'.join(path_parts)}",
    )
    if raw["season_id"] is not None:
        _require(
            f"season_{raw['season_id']}" in path_parts,
            where,
            "season_id is set but no matching season_<id> path segment was found",
        )
    if raw["round_id"] is not None:
        _require(
            f"round_{raw['round_id']}" in path_parts,
            where,
            "round_id is set but no matching round_<id> path segment was found",
        )
    if raw["match_id"] is not None:
        _require(
            f"match_{raw['match_id']}" in path_parts,
            where,
            "match_id is set but no matching match_<id> path segment was found",
        )


def _validate_response_shape(where: str, endpoint_kind: str, response: Any) -> None:
    if endpoint_kind == "seasons":
        _require(
            isinstance(response, dict) and isinstance(response.get("seasons"), list), where, "expected a 'seasons' list"
        )
        for entry in response["seasons"]:
            _require(
                isinstance(entry, dict) and "season_id" in entry and "is_current" in entry and "year" in entry,
                where,
                "each season entry needs season_id, is_current and year",
            )
    elif endpoint_kind == "rounds":
        _require(
            isinstance(response, dict) and isinstance(response.get("rounds"), list), where, "expected a 'rounds' list"
        )
        for entry in response["rounds"]:
            _require(
                isinstance(entry, dict) and "round_id" in entry and "round_number" in entry and "byes" in entry,
                where,
                "each round entry needs round_id, round_number and byes",
            )
    elif endpoint_kind == "matches":
        _require(
            isinstance(response, dict) and isinstance(response.get("matches"), list), where, "expected a 'matches' list"
        )
        for entry in response["matches"]:
            _require(
                isinstance(entry, dict)
                and {"match_id", "status", "home_team", "away_team"} <= set(entry)
                and {"team_id", "name"} <= set(entry["home_team"])
                and {"team_id", "name"} <= set(entry["away_team"]),
                where,
                "each match entry needs match_id, status, home_team{team_id,name}, away_team{team_id,name}",
            )
    elif endpoint_kind == "player_stats":
        _require(
            isinstance(response, dict) and {"match", "lifecycle", "players"} <= set(response),
            where,
            "expected top-level match, lifecycle and players keys",
        )
        _require(
            response["lifecycle"].get("finality") in ("final", "partial", "not_available"),
            where,
            "lifecycle.finality must be one of final/partial/not_available",
        )
        for entry in response["players"]:
            _require(
                isinstance(entry, dict) and "canonical_player_id" in entry and isinstance(entry.get("stats"), dict),
                where,
                "each player-stats row needs canonical_player_id and a stats object",
            )
    elif endpoint_kind == "player_detail":
        _require(
            isinstance(response, dict) and isinstance(response.get("player"), dict), where, "expected a 'player' object"
        )
        player = response["player"]
        _require(
            "canonical_player_id" in player and "display_name" in player,
            where,
            "player object needs canonical_player_id and display_name",
        )
    else:  # pragma: no cover - guarded by _ENDPOINT_KINDS membership above
        raise EvidenceValidationError(f"{where}: endpoint_kind {endpoint_kind!r} has no response payload of its own")


def _validate_envelope(path: Path, envelope: Any) -> None:
    where = str(path)
    _require(isinstance(envelope, dict), where, "fixture file must contain a single JSON object")
    _require("provenance" in envelope, where, "missing 'provenance'")
    has_response, has_facts = "response" in envelope, "facts" in envelope
    _require(has_response != has_facts, where, "fixture must have exactly one of 'response' or 'facts'")

    path_parts = path.relative_to(FIXTURES_ROOT).parts
    _validate_provenance(where, envelope["provenance"], path_parts)
    provenance = envelope["provenance"]

    if has_facts:
        _require(provenance["endpoint"] is None, where, "a 'facts' fixture must not set an endpoint")
        _require(
            provenance["endpoint_kind"] == "scorer_ruling_note",
            where,
            "a 'facts' fixture must use endpoint_kind 'scorer_ruling_note'",
        )
        _require(
            provenance["classification"] == "unresolved", where, "a 'facts' fixture must be classified 'unresolved'"
        )
        facts = envelope["facts"]
        _require(
            isinstance(facts, dict) and facts.get("requires_scorer_ruling") is True,
            where,
            "facts.requires_scorer_ruling must be true",
        )
    else:
        _require(
            provenance["endpoint_kind"] != "scorer_ruling_note",
            where,
            "endpoint_kind 'scorer_ruling_note' must use 'facts', not 'response'",
        )
        _validate_response_shape(where, provenance["endpoint_kind"], envelope["response"])

    _scan_for_secrets(envelope, where)


def load(relative_path: str) -> EvidenceFixture:
    """Load and validate one fixture by its path under
    `tests/fixtures/afl_evidence/` (e.g.
    "v1/synthetic/season_85/round_1500/matches.json"). Raises
    `EvidenceNotFoundError` for a missing file, `EvidenceValidationError`
    for anything malformed/incompatible."""
    path = FIXTURES_ROOT / relative_path
    if not path.is_file():
        raise EvidenceNotFoundError(f"no evidence fixture at {relative_path!r} (looked in {path})")
    try:
        envelope = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise EvidenceValidationError(f"{path}: invalid JSON ({exc})") from exc
    _validate_envelope(path, envelope)
    provenance = Provenance.from_dict(envelope["provenance"])
    return EvidenceFixture(
        path=path,
        relative_path=relative_path,
        provenance=provenance,
        response=envelope.get("response"),
        facts=envelope.get("facts"),
    )


def iter_all() -> list[EvidenceFixture]:
    """Load and validate every committed fixture. Used by this module's own
    test suite to prove the whole corpus is clean; also handy for a
    future replay tool that wants to enumerate everything available."""
    paths = sorted(p for p in FIXTURES_ROOT.rglob("*.json"))
    return [load(p.relative_to(FIXTURES_ROOT).as_posix()) for p in paths]


def build_client(routes: dict[str, str], base_url: str = "http://afl-api.test") -> AflApiClient:
    """Build a real `AflApiClient` wired to an `httpx.MockTransport` that
    serves curated evidence fixtures' `response` payloads.

    `routes` maps an afl-api URL path (e.g. "/api/v1/rounds/1500/matches")
    to a fixture's relative path under `tests/fixtures/afl_evidence/`. This
    is the same offline seam `tests/test_afl_contract_v1.py` uses for issue
    #18's fixtures -- the production adapter's own parsing code runs
    unchanged, and `httpx.MockTransport` never opens a socket, so this
    client cannot reach a live afl-api deployment even by accident.
    """
    fixtures = {url_path: load(relative_path) for url_path, relative_path in routes.items()}

    def handler(request: httpx.Request) -> httpx.Response:
        fixture = fixtures.get(request.url.path)
        if fixture is None:
            return httpx.Response(
                404, json={"error": {"code": "not_found", "message": "no curated evidence route for this path"}}
            )
        return httpx.Response(200, json=fixture.response)

    client = AflApiClient(base_url=base_url)
    client._client = httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))
    return client


class RoundMatchFacts:
    """Duck-typed `app.lockouts.MatchFactsProvider` backed by a real
    `AflApiClient` built from curated evidence (see `build_client`).

    Lets a domain test (e.g. a staged-lockout scenario, issue #34) supply
    curated fixture evidence to `LockoutRepository`/`LockoutTriggerRepository`
    through the exact same protocol production code uses
    (`app.lockouts.RoundMatchFactsProvider`), rather than a hand-built
    `Match` list -- so the test proves the fixture-to-adapter-to-domain
    path, not just that the domain logic works in isolation.
    """

    def __init__(self, client: AflApiClient, afl_round_id: int):
        self._client = client
        self._afl_round_id = afl_round_id

    def matches_for(self, bbbffl_round_id: str):
        return self._client.get_matches(self._afl_round_id)
