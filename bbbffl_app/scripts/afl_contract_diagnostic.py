"""Opt-in live integration diagnostic for the afl-api /api/v1 consumer
contract (issue #18 / roadmap package 04).

This is NOT part of the hermetic test suite and is never run by CI or plain
`pytest`. It makes real, read-only GET requests against a configured
afl-api deployment and reports which contract checks pass, fail, or are
skipped. Nothing here mutates afl-api state.

Usage
-----

    cd bbbffl_app
    export AFL_API_BASE_URL=https://afl-api.example.net   # service root, no /api/v1 suffix
    export AFL_API_KEY=...                                 # a real consumer key
    python -m scripts.afl_contract_diagnostic

Exit code is 0 only if every REQUIRED check PASSes -- a required check that
can only SKIP (e.g. no historical season available to probe, or no match
with player rows was found) counts as not validated and also produces a
non-zero exit, since exit 0 is meant to certify the contract was actually
confirmed, not merely that nothing failed outright. INFORMATIONAL checks
(committed future dependencies BBBFFL does not consume yet, and the optional
OpenAPI cross-check) are always reported but never affect the exit code.

The API key is read only from AFL_API_KEY (via app.config.get_settings(),
the same settings boundary the application itself uses) and is never
printed, logged, or included in any diagnostic output.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from app.config import get_settings

REQUIRED_STAT_FIELDS = {"goals", "behinds", "disposals", "marks", "tackles", "hitouts"}
VALID_FINALITY = {"final", "partial", "not_available"}
VALID_MATCH_STATES = {"UPCOMING", "LIVE", "POSTGAME", "CONCLUDED"}


@dataclass
class CheckResult:
    name: str
    status: str  # "PASS" | "FAIL" | "SKIP"
    detail: str = ""
    required: bool = True


@dataclass
class DiagnosticContext:
    client: httpx.Client
    results: list[CheckResult] = field(default_factory=list)
    # Discovered state threaded between checks.
    season_id: int | None = None
    current_round_number: int | None = None
    round_id: int | None = None
    match_id: int | None = None
    canonical_player_id: int | None = None
    historical_season_id: int | None = None

    def record(self, name: str, ok: bool, detail: str = "", required: bool = True) -> None:
        self.results.append(CheckResult(name, "PASS" if ok else "FAIL", detail, required))

    def skip(self, name: str, reason: str, required: bool = True) -> None:
        self.results.append(CheckResult(name, "SKIP", reason, required))


class _NetworkFailure(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _get(ctx: DiagnosticContext, path: str, **kwargs: Any) -> httpx.Response:
    try:
        return ctx.client.get(path, **kwargs)
    except httpx.HTTPError as exc:
        # Network/proxy/TLS failures are reported as a normal FAIL for the
        # calling check rather than crashing the whole diagnostic with a
        # traceback -- e.g. an egress policy blocking the configured host.
        raise _NetworkFailure(f"{type(exc).__name__}: {exc}") from exc


def check_discovery(ctx: DiagnosticContext) -> None:
    resp = _get(ctx, "/api/v1")
    if resp.status_code != 200:
        ctx.record("GET /api/v1 (discovery)", False, f"status={resp.status_code}")
        return
    body = resp.json()
    ok = {"name", "version", "documentation"} <= body.keys()
    ctx.record("GET /api/v1 (discovery)", ok, f"version={body.get('version')!r}")


def check_seasons(ctx: DiagnosticContext) -> None:
    resp = _get(ctx, "/api/v1/seasons")
    if resp.status_code != 200:
        ctx.record("GET /api/v1/seasons", False, f"status={resp.status_code}")
        return
    seasons = resp.json().get("seasons", [])
    if not seasons:
        ctx.record("GET /api/v1/seasons", False, "no seasons returned")
        return
    current = [s for s in seasons if s.get("is_current")]
    ok = len(current) == 1 and "season_id" in current[0]
    ctx.record(
        "GET /api/v1/seasons",
        ok,
        f"{len(seasons)} seasons, {len(current)} flagged is_current=true",
    )
    if current:
        ctx.season_id = current[0]["season_id"]
        ctx.current_round_number = current[0].get("current_round_number")
        ctx.record(
            "seasons: current_round_number present or explicitly null",
            "current_round_number" in current[0],
            required=False,
        )
    historical = [s for s in seasons if not s.get("is_current")]
    if historical:
        ctx.historical_season_id = historical[0]["season_id"]


def check_rounds(ctx: DiagnosticContext) -> None:
    if ctx.season_id is None:
        ctx.skip("GET /api/v1/seasons/{id}/rounds", "no current season resolved")
        return
    resp = _get(ctx, f"/api/v1/seasons/{ctx.season_id}/rounds")
    if resp.status_code != 200:
        ctx.record("GET /api/v1/seasons/{id}/rounds", False, f"status={resp.status_code}")
        return
    rounds = resp.json().get("rounds", [])
    ctx.record("GET /api/v1/seasons/{id}/rounds", bool(rounds), f"{len(rounds)} rounds")
    if not rounds:
        return
    ctx.record(
        "rounds: byes field is array-or-null (never a bare truthy sentinel)",
        all(r.get("byes") is None or isinstance(r.get("byes"), list) for r in rounds),
        required=False,
    )
    # Prefer the round matching the season's own reported current_round_number
    # (set by check_seasons) -- falling back to the first round with a known
    # round_number only when that lookup fails (e.g. current_round_number is
    # null, or no round in the list actually carries that number).
    preferred = None
    if ctx.current_round_number is not None:
        preferred = next((r for r in rounds if r.get("round_number") == ctx.current_round_number), None)
    if preferred is None:
        preferred = next((r for r in rounds if r.get("round_number") is not None), rounds[0])
    ctx.round_id = preferred["round_id"]


def check_historical_season(ctx: DiagnosticContext) -> None:
    if ctx.historical_season_id is None:
        ctx.skip(
            "GET /api/v1/seasons/{historical_id}/rounds (2026 replay prerequisite)",
            "only one season returned; cannot confirm historical access",
        )
        return
    resp = _get(ctx, f"/api/v1/seasons/{ctx.historical_season_id}/rounds")
    ok = resp.status_code == 200
    detail = f"status={resp.status_code}"
    if ok:
        rounds = resp.json().get("rounds", [])
        detail = f"{len(rounds)} rounds for season_id={ctx.historical_season_id}"
        if not rounds:
            detail += " -- WARNING: season listed but has no persisted rounds; replay data gap"
    ctx.record("GET /api/v1/seasons/{historical_id}/rounds (2026 replay prerequisite)", ok, detail)


def check_matches(ctx: DiagnosticContext) -> None:
    if ctx.round_id is None:
        ctx.skip("GET /api/v1/rounds/{id}/matches", "no round resolved")
        return
    resp = _get(ctx, f"/api/v1/rounds/{ctx.round_id}/matches")
    if resp.status_code != 200:
        ctx.record("GET /api/v1/rounds/{id}/matches", False, f"status={resp.status_code}")
        return
    matches = resp.json().get("matches", [])
    ctx.record("GET /api/v1/rounds/{id}/matches", True, f"{len(matches)} matches")
    if not matches:
        return
    statuses = {(m.get("status") or "").upper() for m in matches}
    unrecognised = statuses - VALID_MATCH_STATES
    ctx.record(
        "matches: status values are within the known lifecycle vocabulary",
        not unrecognised,
        f"observed={sorted(statuses)}" + (f" UNRECOGNISED={sorted(unrecognised)}" if unrecognised else ""),
        required=False,
    )
    # Prefer a CONCLUDED match so the player-stats check below sees final data.
    preferred = next((m for m in matches if (m.get("status") or "").upper() == "CONCLUDED"), matches[0])
    ctx.match_id = preferred["match_id"]


def check_match_detail(ctx: DiagnosticContext) -> None:
    if ctx.match_id is None:
        ctx.skip("GET /api/v1/matches/{id}", "no match resolved")
        return
    resp = _get(ctx, f"/api/v1/matches/{ctx.match_id}")
    ok = resp.status_code == 200
    detail = f"status={resp.status_code}"
    if ok:
        body = resp.json()
        required_keys = {"match_id", "round_id", "status", "home_team", "away_team"}
        ok = required_keys <= body.keys()
        detail = f"status_field={body.get('status')!r}"
    ctx.record("GET /api/v1/matches/{id}", ok, detail)


def check_player_stats(ctx: DiagnosticContext) -> None:
    if ctx.match_id is None:
        ctx.skip("GET /api/v1/matches/{id}/player-stats", "no match resolved")
        return
    resp = _get(ctx, f"/api/v1/matches/{ctx.match_id}/player-stats")
    if resp.status_code != 200:
        ctx.record("GET /api/v1/matches/{id}/player-stats", False, f"status={resp.status_code}")
        return
    body = resp.json()
    finality = body.get("lifecycle", {}).get("finality")
    ctx.record(
        "player-stats: lifecycle.finality is a recognised value",
        finality in VALID_FINALITY,
        f"finality={finality!r}",
    )
    players = body.get("players", [])
    if players:
        # Checked across every row, not just the first -- a later row is
        # just as capable of omitting a scored field, and AflApiClient
        # currently coerces a missing/null field to 0 rather than raising,
        # so an incomplete row would otherwise pass unnoticed.
        missing_by_row = [
            (i, sorted(REQUIRED_STAT_FIELDS - set(row.get("stats", {}).keys()))) for i, row in enumerate(players)
        ]
        missing_by_row = [(i, fields) for i, fields in missing_by_row if fields]
        ctx.record(
            "player-stats: rows expose all BBBFFL-scored stat fields",
            not missing_by_row,
            "all rows complete" if not missing_by_row else f"missing (row_index, fields)={missing_by_row[:5]}",
        )
        resolved = [p for p in players if p.get("canonical_player_id") is not None]
        if resolved:
            ctx.canonical_player_id = resolved[0]["canonical_player_id"]
        ctx.record(
            "player-stats: canonical_player_id is present-or-null (never guessed)",
            all("canonical_player_id" in p for p in players),
            required=False,
        )
    else:
        ctx.skip(
            "player-stats: rows expose all BBBFFL-scored stat fields",
            "no player rows on this match yet",
        )


def check_player_identity(ctx: DiagnosticContext) -> None:
    if ctx.canonical_player_id is None:
        ctx.skip("GET /api/v1/players/{id}", "no resolved canonical_player_id available")
        ctx.skip("GET /api/v1/players/{id}/seasons", "no resolved canonical_player_id available", required=False)
        return
    resp = _get(ctx, f"/api/v1/players/{ctx.canonical_player_id}")
    ok = resp.status_code == 200
    detail = f"status={resp.status_code}"
    if ok:
        player = resp.json().get("player", {})
        ok = {"canonical_player_id", "display_name", "current_team", "identifiers"} <= player.keys()
    ctx.record("GET /api/v1/players/{id}", ok, detail)

    resp2 = _get(ctx, f"/api/v1/players/{ctx.canonical_player_id}/seasons")
    ok2 = resp2.status_code == 200 and "seasons" in resp2.json()
    ctx.record(
        "GET /api/v1/players/{id}/seasons (package 11 dependency)",
        ok2,
        f"status={resp2.status_code}",
        required=False,
    )


def check_player_search(ctx: DiagnosticContext) -> None:
    resp = _get(ctx, "/api/v1/players", params={"search": "a"})
    ok = resp.status_code == 200 and "players" in resp.json()
    ctx.record(
        "GET /api/v1/players?search= (package 11 dependency)",
        ok,
        f"status={resp.status_code}",
        required=False,
    )


def check_injuries(ctx: DiagnosticContext) -> None:
    resp = _get(ctx, "/api/v1/injuries")
    ok = resp.status_code == 200 and "injuries" in resp.json()
    ctx.record(
        "GET /api/v1/injuries (package 27 dependency)",
        ok,
        f"status={resp.status_code}",
        required=False,
    )


def check_rosters(ctx: DiagnosticContext) -> None:
    if ctx.match_id is None:
        ctx.skip("GET /api/v1/matches/{id}/rosters (package 23/27 dependency)", "no match resolved", required=False)
        return
    resp = _get(ctx, f"/api/v1/matches/{ctx.match_id}/rosters")
    ok = resp.status_code == 200 and {"home_team", "away_team"} <= resp.json().keys()
    ctx.record(
        "GET /api/v1/matches/{id}/rosters (package 23/27 dependency)",
        ok,
        f"status={resp.status_code}",
        required=False,
    )


def check_not_found_error_shape(ctx: DiagnosticContext) -> None:
    resp = _get(ctx, "/api/v1/players/999999999999")
    ok = resp.status_code == 404
    detail = f"status={resp.status_code}"
    if ok:
        body = resp.json()
        ok = body.get("error", {}).get("code") == "player_not_found"
        detail = f"code={body.get('error', {}).get('code')!r}"
    ctx.record("GET /api/v1/players/{nonexistent} -> structured 404", ok, detail)


def check_blank_search_error_shape(ctx: DiagnosticContext) -> None:
    resp = _get(ctx, "/api/v1/players", params={"search": ""})
    ok = resp.status_code == 422
    detail = f"status={resp.status_code}"
    if ok:
        body = resp.json()
        ok = body.get("error", {}).get("code") == "search_required"
        detail = f"code={body.get('error', {}).get('code')!r}"
    ctx.record("GET /api/v1/players?search= (blank) -> structured 422", ok, detail, required=False)


def check_auth_failures(base_url: str, transport: httpx.BaseTransport | None = None) -> list[CheckResult]:
    """Deliberately uses a *separate* client with no/garbage credentials --
    never the configured real key -- so this never risks locking out or
    otherwise touching the real credential."""
    results: list[CheckResult] = []
    try:
        with httpx.Client(base_url=base_url, timeout=20.0, transport=transport) as no_key_client:
            resp = no_key_client.get("/api/v1/seasons")
            ok = resp.status_code == 401 and "detail" in resp.json()
            results.append(
                CheckResult(
                    "GET /api/v1/seasons (no key) -> 401", "PASS" if ok else "FAIL", f"status={resp.status_code}"
                )
            )
        with httpx.Client(
            base_url=base_url, headers={"x-api-key": "not-a-real-key"}, timeout=20.0, transport=transport
        ) as bad_key_client:
            resp = bad_key_client.get("/api/v1/seasons")
            ok = resp.status_code == 401 and "detail" in resp.json()
            results.append(
                CheckResult(
                    "GET /api/v1/seasons (invalid key) -> 401", "PASS" if ok else "FAIL", f"status={resp.status_code}"
                )
            )
    except httpx.HTTPError as exc:
        results.append(CheckResult("auth failure checks", "FAIL", f"{type(exc).__name__}: {exc}"))
    return results


def check_openapi_optional(base_url: str, transport: httpx.BaseTransport | None = None) -> CheckResult:
    """Best-effort only. BBBFFL's runtime never depends on this succeeding --
    see docs/afl-api-v1-contract.md's "OpenAPI usage" section."""
    required_paths = {
        "/api/v1",
        "/api/v1/seasons",
        "/api/v1/seasons/{season_id}/rounds",
        "/api/v1/rounds/{round_id}/matches",
        "/api/v1/matches/{match_id}",
        "/api/v1/matches/{match_id}/player-stats",
        "/api/v1/players/{canonical_player_id}",
    }
    try:
        with httpx.Client(base_url=base_url, timeout=20.0, transport=transport) as client:
            resp = client.get("/openapi.json")
        if resp.status_code != 200:
            return CheckResult("GET /openapi.json (optional)", "SKIP", f"status={resp.status_code}", required=False)
        paths = set(resp.json().get("paths", {}).keys())
        missing = required_paths - paths
        ok = not missing
        detail = "all required paths advertised" if ok else f"missing={sorted(missing)}"
        return CheckResult("GET /openapi.json (optional)", "PASS" if ok else "FAIL", detail, required=False)
    except httpx.HTTPError as exc:
        return CheckResult("GET /openapi.json (optional)", "SKIP", str(exc), required=False)


REQUIRED_CHECKS: list[Callable[[DiagnosticContext], None]] = [
    check_discovery,
    check_seasons,
    check_rounds,
    check_historical_season,
    check_matches,
    check_match_detail,
    check_player_stats,
    check_player_identity,
    check_player_search,
    check_injuries,
    check_rosters,
    check_not_found_error_shape,
    check_blank_search_error_shape,
]


def run(
    base_url: str,
    api_key: str,
    timeout: float = 20.0,
    transport: httpx.BaseTransport | None = None,
) -> list[CheckResult]:
    """`transport` is exposed only so the offline hermetic test suite can
    prove this diagnostic's own check logic against a mock transport --
    normal (real) usage never passes it, and the network path is otherwise
    identical to any other httpx.Client call."""
    client = httpx.Client(base_url=base_url, headers={"x-api-key": api_key}, timeout=timeout, transport=transport)
    ctx = DiagnosticContext(client=client)
    try:
        for check in REQUIRED_CHECKS:
            try:
                check(ctx)
            except _NetworkFailure as exc:
                ctx.record(f"{check.__name__} (network)", False, exc.detail)
            except Exception as exc:  # noqa: BLE001
                # A malformed/incompatible 200 response (e.g. a required
                # field missing or wrongly typed) must be reported as a
                # failed check, never crash the whole diagnostic and hide
                # every check after it -- this is exactly the "materially
                # incompatible deployment" case the diagnostic exists to
                # detect. See docs/afl-api-v1-contract.md.
                ctx.record(f"{check.__name__} (malformed response)", False, f"{type(exc).__name__}: {exc}")
    finally:
        client.close()
    ctx.results.extend(check_auth_failures(base_url, transport=transport))
    ctx.results.append(check_openapi_optional(base_url, transport=transport))
    return ctx.results


def _print_report(results: list[CheckResult]) -> bool:
    width = max((len(r.name) for r in results), default=0)
    all_required_passed = True
    for r in results:
        tag = "   " if r.required else "opt"
        print(f"[{r.status:4}][{tag}] {r.name.ljust(width)}  {r.detail}")
        if r.required and r.status in ("FAIL", "SKIP"):
            # A required check that could only SKIP (e.g. no historical
            # season, or no match with player rows was found to probe)
            # means that piece of the contract was never actually
            # confirmed -- exit 0 must not claim it was validated.
            all_required_passed = False
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    skipped = sum(1 for r in results if r.status == "SKIP")
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped ({len(results)} checks total)")
    if not all_required_passed:
        print("\nOne or more REQUIRED contract checks failed -- see docs/afl-api-v1-contract.md")
        print("before trusting this deployment for BBBFFL scoring/replay work.")
    return all_required_passed


def main() -> int:
    settings = get_settings()
    if not settings.afl_api_key:
        print(
            "AFL_API_KEY is not set. This diagnostic requires a real consumer key "
            "(see bbbffl_app README's afl-api contract section). Aborting.",
            file=sys.stderr,
        )
        return 2
    base_url = settings.afl_api_base_url
    print(f"afl-api v1 contract diagnostic against {base_url} (key redacted)\n")
    results = run(base_url, settings.afl_api_key, timeout=settings.afl_api_timeout_seconds)
    ok = _print_report(results)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
