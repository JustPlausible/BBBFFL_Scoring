"""Supported exporter for hermetic 2026 AFL replay evidence (first- and
second-half).

The first-half (Opening Round + AFL R1-9) and second-half (AFL R10-20)
acquisition entry points below share the same season-resolution,
player-pool pagination, and per-round match/stat/roster acquisition
boundaries -- only round selection/validation and package manifest identity
differ between them. Neither half falls back to live AFL data once
acquisition has produced a package: both are consumed strictly offline
afterwards (see `app.replay.ReplayAflDataSource`)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from app.replay import EvidenceClass, ReplayAflDataSource, ReplayClock, ReplayEvidenceError


class ConsumerApi(Protocol):
    def get(self, path: str) -> dict | list: ...


def _rows(payload: Any, key: str, *, path: str) -> list[dict]:
    if isinstance(payload, dict):
        payload = payload.get(key, payload.get("results"))
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ReplayEvidenceError(f"malformed consumer API response at {path}: expected {key} list")
    return payload


def _prov(path: str) -> dict[str, str]:
    return {"source": f"afl-api-v1:{path}", "evidence_class": EvidenceClass.KNOWN_FACT.value}


SEASON_PLAYERS_PAGE_LIMIT = 250


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _acquire_season_players(api: ConsumerApi, players_path: str) -> tuple[dict[int, dict], int]:
    """Follow the AFL-api #248 season-player collection to exhaustion.

    ``_rows`` intentionally strips the response envelope, which is exactly
    the information (``offset``, page size) pagination progress needs to be
    validated, so this is a dedicated paginator rather than a generic-helper
    workaround. Each page is requested at ``limit=250`` starting at
    ``offset=0``; a page shorter than the requested limit (including an
    empty page) is the valid terminal condition. The requested offset is
    advanced by this function itself rather than trusted from the response,
    so a page reporting an unexpected offset fails closed instead of looping
    or silently skipping/repeating rows, and a canonical_player_id repeated
    across pages fails closed rather than being silently merged.
    """
    limit = SEASON_PLAYERS_PAGE_LIMIT
    offset = 0
    page_count = 0
    players: dict[int, dict] = {}
    while True:
        page_path = f"{players_path}?limit={limit}&offset={offset}"
        payload = api.get(page_path)
        if not isinstance(payload, dict):
            raise ReplayEvidenceError(f"malformed consumer API response at {page_path}: expected an object envelope")
        rows = payload.get("players")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ReplayEvidenceError(f"malformed consumer API response at {page_path}: expected a players list")
        returned_offset = payload.get("offset")
        if returned_offset != offset:
            raise ReplayEvidenceError(
                f"AFL season-player page at {page_path} reports offset {returned_offset!r}, expected {offset}"
            )
        returned_limit = payload.get("limit")
        if returned_limit != limit:
            # A page shorter than `limit` is only a valid terminal page when
            # the envelope confirms `limit` is what we actually asked for --
            # otherwise a server-side clamp (e.g. limit: 100) would look
            # identical to a genuine final page and silently truncate the
            # pool.
            raise ReplayEvidenceError(
                f"AFL season-player page at {page_path} reports limit {returned_limit!r}, expected {limit}"
            )
        page_count += 1
        for row in rows:
            player_id = row.get("canonical_player_id")
            if not _is_positive_int(player_id):
                raise ReplayEvidenceError(
                    f"AFL season-player page at {page_path} has a malformed canonical_player_id: {player_id!r}"
                )
            if player_id in players:
                raise ReplayEvidenceError(
                    f"AFL season {players_path} contains duplicate canonical player {player_id} "
                    f"(seen again at {page_path})"
                )
            display_name = row.get("display_name")
            if not isinstance(display_name, str) or not display_name.strip():
                raise ReplayEvidenceError(
                    f"AFL season-player {player_id} at {page_path} has a blank or missing display_name"
                )
            # BBBFFL requires a resolved requested-season team even though
            # AFL-api permits team: null for unresolved membership; never
            # fall back to current_team, another season, or match-stat team
            # identity -- an unresolved team blocks acquisition instead of
            # being guessed.
            team = row.get("team")
            if not isinstance(team, dict):
                raise ReplayEvidenceError(
                    f"AFL season-player {player_id} at {page_path} has no resolved requested-season team"
                )
            team_id = team.get("team_id")
            if not _is_positive_int(team_id):
                raise ReplayEvidenceError(
                    f"AFL season-player {player_id} at {page_path} has a malformed team.team_id: {team_id!r}"
                )
            team_name = team.get("name")
            if not isinstance(team_name, str) or not team_name.strip():
                raise ReplayEvidenceError(
                    f"AFL season-player {player_id} at {page_path} has a blank or missing team.name"
                )
            players[player_id] = {
                "canonical_player_id": player_id,
                "display_name": display_name,
                "team_id": team_id,
                "team_name": team_name,
                "identifiers": row.get("identifiers", {}),
                "provenance": _prov(players_path),
            }
        if len(rows) < limit:
            break
        offset += limit
    if not players:
        raise ReplayEvidenceError(f"authoritative AFL season-player pool is empty at {players_path}")
    return players, page_count


def _resolve_2026_season_and_players(api: ConsumerApi) -> tuple[dict, int, str, list[dict], int, dict[int, dict]]:
    """Resolve the single AFL 2026 season and its full season-player pool
    through API metadata (never a hard-coded database ID), shared by both
    the first- and second-half acquisition entry points."""
    seasons = _rows(api.get("/api/v1/seasons"), "seasons", path="/api/v1/seasons")
    candidates = [s for s in seasons if s.get("year") == 2026]
    if len(candidates) != 1:
        raise ReplayEvidenceError(f"expected exactly one AFL 2026 season, found {len(candidates)}")
    season = candidates[0]
    season_id = season.get("season_id")
    if season_id is None:
        raise ReplayEvidenceError("AFL 2026 season is missing season_id")
    rounds_path = f"/api/v1/seasons/{season_id}/rounds"
    all_rounds = _rows(api.get(rounds_path), "rounds", path=rounds_path)
    players_path = f"/api/v1/seasons/{season_id}/players"
    players, player_page_count = _acquire_season_players(api, players_path)
    return season, season_id, rounds_path, all_rounds, player_page_count, players


def _acquire_match_evidence(
    api: ConsumerApi, rounds: list[dict], players: dict[int, dict], season_id: int
) -> tuple[list[dict], dict[str, list[dict]], dict[str, Any], list[dict]]:
    """Acquire every match, its required final player stats, and optional
    roster evidence for `rounds`. Shared by both acquisition halves: this is
    the acquisition/domain boundary the second-half entry point reuses
    rather than reimplementing.

    Fails closed on a match acquired twice under two different selected
    rounds (an ambiguous/duplicate round selection upstream) and on a
    duplicate player-stat row within one match's response, neither of which
    the underlying dict/list accumulation below would otherwise notice
    silently."""
    matches_out: list[dict] = []
    stats_out: dict[str, list[dict]] = {}
    rosters: dict[str, Any] = {}
    roster_missing: list[dict] = []
    for round_row in rounds:
        round_id = round_row.get("round_id")
        if round_id is None:
            raise ReplayEvidenceError("selected AFL round is missing round_id")
        path = f"/api/v1/rounds/{round_id}/matches"
        matches = _rows(api.get(path), "matches", path=path)
        if not matches:
            raise ReplayEvidenceError(f"AFL round {round_id} contains no matches")
        for match in matches:
            match_id = match.get("match_id")
            if match_id is None:
                raise ReplayEvidenceError(f"AFL round {round_id} contains a match missing match_id")
            if match.get("round_id", round_id) != round_id:
                raise ReplayEvidenceError(f"AFL match {match_id} references inconsistent round {match.get('round_id')}")
            if str(match_id) in stats_out:
                raise ReplayEvidenceError(
                    f"AFL match {match_id} was already acquired under a different selected round "
                    "(duplicate/ambiguous round selection)"
                )
            for field in ("home_team", "away_team", "start_time_utc", "status"):
                if not match.get(field):
                    raise ReplayEvidenceError(f"AFL match {match_id} is missing required {field}")
            try:
                start = datetime.fromisoformat(str(match["start_time_utc"]).replace("Z", "+00:00"))
                if start.tzinfo is None:
                    raise ValueError("timestamp is not timezone-aware")
                for side in ("home_team", "away_team"):
                    int(match[side]["team_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ReplayEvidenceError(f"AFL match {match_id} has malformed start/team identity: {exc}") from exc
            matches_out.append({**match, "round_id": round_id, "provenance": _prov(path)})
            stats_path = f"/api/v1/matches/{match_id}/player-stats"
            stats_payload = api.get(stats_path)
            if not isinstance(stats_payload, dict):
                raise ReplayEvidenceError(f"malformed consumer API response at {stats_path}: expected object")
            finality = (stats_payload.get("lifecycle") or {}).get("finality")
            if str(finality).lower() != "final":
                raise ReplayEvidenceError(
                    f"required final player stats unavailable for AFL match {match_id} "
                    f"in round {round_id}: finality={finality!r}"
                )
            stat_rows = _rows(stats_payload, "players", path=stats_path)
            if not stat_rows:
                raise ReplayEvidenceError(f"required final player stats missing for AFL match {match_id}")
            exported_stats = []
            seen_stat_players: set[int] = set()
            for row in stat_rows:
                pid = row.get("canonical_player_id")
                if pid is None or row.get("team_id") is None or not isinstance(row.get("stats"), dict):
                    raise ReplayEvidenceError(
                        f"AFL match {match_id} has malformed player-stat identity for player {pid}"
                    )
                if pid not in players:
                    raise ReplayEvidenceError(
                        f"AFL match {match_id} stats reference player {pid} missing from season {season_id} player pool"
                    )
                if pid in seen_stat_players:
                    raise ReplayEvidenceError(f"AFL match {match_id} has duplicate player-stat rows for player {pid}")
                seen_stat_players.add(pid)
                stat = row["stats"]
                exported_stats.append(
                    {
                        "canonical_player_id": pid,
                        **{k: stat.get(k) for k in ("goals", "behinds", "disposals", "marks", "hitouts", "tackles")},
                        "identifiers": row.get("identifiers", {}),
                        "provenance": _prov(stats_path),
                    }
                )
            stats_out[str(match_id)] = exported_stats
            roster_path = f"/api/v1/matches/{match_id}/rosters"
            try:
                roster = api.get(roster_path)
            except Exception as exc:  # optional endpoint: absence is coverage, never invented evidence
                rosters[str(match_id)] = None
                roster_missing.append({"match_id": match_id, "reason": type(exc).__name__})
            else:
                rosters[str(match_id)] = roster
    return matches_out, stats_out, rosters, roster_missing


def _safe_source_string(source_base_url: str) -> str:
    """Strip credentials/paths, keeping only scheme://host[:port] for the
    committed manifest -- never expose an embedded API key."""
    source = urlsplit(source_base_url)
    return f"{source.scheme}://{source.hostname}" + (f":{source.port}" if source.port else "")


def _included_rounds_summary(rounds: list[dict]) -> list[dict]:
    return [
        {
            "round_id": r["round_id"],
            "round_number": r.get("round_number"),
            "name": r.get("name"),
            "abbreviation": r.get("abbreviation"),
        }
        for r in rounds
    ]


def acquire_first_half_2026(api: ConsumerApi, *, source_base_url: str, acquired_at: datetime | None = None) -> dict:
    """Acquire Opening Round and rounds 1--9; fail before returning partial evidence."""
    acquired_at = acquired_at or datetime.now(timezone.utc)
    if acquired_at.tzinfo is None:
        raise ReplayEvidenceError("acquisition timestamp must be timezone-aware")
    season, season_id, rounds_path, all_rounds, player_page_count, players = _resolve_2026_season_and_players(api)

    def wanted(row: dict) -> bool:
        number = row.get("round_number")
        label = " ".join(str(row.get(k, "")) for k in ("name", "abbreviation")).strip().lower()
        return number in range(1, 10) or number == 0 or "opening round" in label

    rounds = [r for r in all_rounds if wanted(r)]
    round_ids = [r.get("round_id") for r in rounds]
    if len(round_ids) != len(set(round_ids)):
        raise ReplayEvidenceError(
            f"AFL season {season_id} has duplicate round_id entries among selected Opening Round/R1-9 rounds: "
            f"{round_ids}"
        )
    ordinary_numbers = [r.get("round_number") for r in rounds if r.get("round_number") in range(1, 10)]
    if len(ordinary_numbers) != len(set(ordinary_numbers)):
        raise ReplayEvidenceError(
            f"AFL season {season_id} has duplicate/ambiguous round_number entries among rounds 1-9: "
            f"{sorted(ordinary_numbers)}"
        )
    ordinary = set(ordinary_numbers)
    opening = [r for r in rounds if r.get("round_number") == 0 or "opening round" in str(r.get("name", "")).lower()]
    if ordinary != set(range(1, 10)) or len(opening) != 1:
        raise ReplayEvidenceError(
            f"AFL season {season_id} requires one Opening Round and rounds 1-9; "
            f"found ordinary={sorted(ordinary)}, opening={len(opening)}"
        )
    rounds.sort(key=lambda r: (r.get("round_number", 999), r.get("round_id", 0)))
    matches_out, stats_out, rosters, roster_missing = _acquire_match_evidence(api, rounds, players, season_id)
    safe_source = _safe_source_string(source_base_url)
    included = _included_rounds_summary(rounds)
    return {
        "schema": ReplayAflDataSource.SCHEMA,
        "manifest": {
            "id": "afl-2026-first-half",
            "version": "1",
            "package_version": "bbbffl.first-half/v1",
            "evidence_class": EvidenceClass.KNOWN_FACT.value,
            "acquired_at": acquired_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "afl_season": 2026,
            "source_api": safe_source,
            "source_api_version": "v1",
            "exporter": "bbbffl replay acquisition v1",
            "included_rounds": included,
            "match_count": len(matches_out),
            "player_stat_match_count": len(stats_out),
            "roster_coverage": {"available": len(matches_out) - len(roster_missing), "unavailable": roster_missing},
            "player_pool_count": len(players),
            "player_pool_page_count": player_page_count,
            "lifecycle_semantics": "scheduled-start-plus-final-results-checkpoint",
        },
        "seasons": [
            {
                "season_id": season_id,
                "year": 2026,
                "is_current": True,
                "current_round_number": season.get("current_round_number"),
                "identifiers": season.get("identifiers", {}),
                "provenance": _prov("/api/v1/seasons"),
            }
        ],
        "rounds": [{**r, "season_id": season_id, "provenance": _prov(rounds_path)} for r in rounds],
        "matches": matches_out,
        "players": sorted(players.values(), key=lambda p: p["canonical_player_id"]),
        "player_stats": stats_out,
        "rosters": rosters,
        "lineups": [],
    }


SECOND_HALF_ROUND_NUMBERS = tuple(range(10, 21))


def acquire_second_half_2026(api: ConsumerApi, *, source_base_url: str, acquired_at: datetime | None = None) -> dict:
    """Acquire AFL rounds 10--20 inclusive (11 rounds); fail before returning
    partial evidence.

    Reuses the same season-resolution, player-pool pagination, and
    match/stat/roster acquisition boundaries as `acquire_first_half_2026`
    (see `_resolve_2026_season_and_players` / `_acquire_match_evidence`) --
    only round selection/validation and package manifest identity differ.
    There is no Opening Round concept in the second half: every selected
    round must carry an ordinary `round_number` in 10-20, and the set must
    be exactly that range, no more and no fewer, with no duplicate/ambiguous
    round identity."""
    acquired_at = acquired_at or datetime.now(timezone.utc)
    if acquired_at.tzinfo is None:
        raise ReplayEvidenceError("acquisition timestamp must be timezone-aware")
    season, season_id, rounds_path, all_rounds, player_page_count, players = _resolve_2026_season_and_players(api)

    expected = set(SECOND_HALF_ROUND_NUMBERS)

    def wanted(row: dict) -> bool:
        return row.get("round_number") in expected

    rounds = [r for r in all_rounds if wanted(r)]
    round_ids = [r.get("round_id") for r in rounds]
    if len(round_ids) != len(set(round_ids)):
        raise ReplayEvidenceError(
            f"AFL season {season_id} has duplicate round_id entries among selected AFL rounds 10-20: {round_ids}"
        )
    numbers = [r.get("round_number") for r in rounds]
    if len(numbers) != len(set(numbers)):
        raise ReplayEvidenceError(
            f"AFL season {season_id} has duplicate/ambiguous round_number entries among AFL rounds 10-20: "
            f"{sorted(numbers)}"
        )
    if set(numbers) != expected:
        raise ReplayEvidenceError(
            f"AFL season {season_id} requires exactly AFL rounds 10-20 inclusive (11 rounds); "
            f"found {sorted(set(numbers))}"
        )
    rounds.sort(key=lambda r: (r.get("round_number", 999), r.get("round_id", 0)))
    matches_out, stats_out, rosters, roster_missing = _acquire_match_evidence(api, rounds, players, season_id)
    safe_source = _safe_source_string(source_base_url)
    included = _included_rounds_summary(rounds)
    return {
        "schema": ReplayAflDataSource.SCHEMA,
        "manifest": {
            "id": "afl-2026-second-half",
            "version": "1",
            "package_version": "bbbffl.second-half/v1",
            "evidence_class": EvidenceClass.KNOWN_FACT.value,
            "acquired_at": acquired_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "afl_season": 2026,
            "source_api": safe_source,
            "source_api_version": "v1",
            "exporter": "bbbffl replay acquisition v1",
            "included_rounds": included,
            "match_count": len(matches_out),
            "player_stat_match_count": len(stats_out),
            "roster_coverage": {"available": len(matches_out) - len(roster_missing), "unavailable": roster_missing},
            "player_pool_count": len(players),
            "player_pool_page_count": player_page_count,
            "lifecycle_semantics": "scheduled-start-plus-final-results-checkpoint",
        },
        "seasons": [
            {
                "season_id": season_id,
                "year": 2026,
                "is_current": True,
                "current_round_number": season.get("current_round_number"),
                "identifiers": season.get("identifiers", {}),
                "provenance": _prov("/api/v1/seasons"),
            }
        ],
        "rounds": [{**r, "season_id": season_id, "provenance": _prov(rounds_path)} for r in rounds],
        "matches": matches_out,
        "players": sorted(players.values(), key=lambda p: p["canonical_player_id"]),
        "player_stats": stats_out,
        "rosters": rosters,
        "lineups": [],
    }


def validate_replay_package(
    evidence_path: str | Path,
    checkpoint_path: str | Path,
    *,
    expected_package_version: str,
    expected_afl_season: int,
    expected_round_numbers: Iterable[int],
) -> ReplayAflDataSource:
    """Load and validate a deterministic replay evidence package offline,
    failing closed on anything short of full completeness for the required
    round set. This is a stricter, package-shape-aware validation layered
    on top of `ReplayAflDataSource`'s own structural/provenance checks
    (schema, cross-references, required player_stats coverage per match),
    which already run as part of loading it.

    Beyond that structural loading, this additionally proves: the package
    declares a supported `package_version`; it resolves the expected AFL
    season; it carries exactly the expected (unique) set of AFL round
    numbers; its declared `match_count`/`player_stat_match_count` manifest
    counters are consistent with what was actually loaded and are complete;
    and every required match carries a valid, parseable, timezone-aware
    scheduled start time. Never accessed live -- `ReplayAflDataSource`
    loading here is purely local file I/O."""
    source = ReplayAflDataSource(evidence_path, checkpoint_path=checkpoint_path)
    manifest = source.manifest
    if manifest.get("package_version") != expected_package_version:
        raise ReplayEvidenceError(
            f"unsupported replay package version: {manifest.get('package_version')!r}, "
            f"expected {expected_package_version!r}"
        )
    # The manifest's afl_season is a label the acquirer wrote; it is not
    # itself proof the evidence resolves that season. Resolve the season
    # independently from what ReplayAflDataSource actually loaded, so a
    # manifest that claims 2026 while its own `seasons` evidence record
    # declares a different year fails here instead of validation reading
    # PASS for evidence that does not resolve the requested AFL season.
    matching_seasons = [season for season in source.get_seasons() if season.year == expected_afl_season]
    if len(matching_seasons) != 1:
        raise ReplayEvidenceError(
            f"replay package evidence does not resolve exactly one season for year {expected_afl_season}; "
            f"found {len(matching_seasons)}"
        )
    resolved_season = matching_seasons[0]
    if manifest.get("afl_season") != expected_afl_season:
        raise ReplayEvidenceError(
            f"replay package manifest declares AFL season {manifest.get('afl_season')!r}, "
            f"expected {expected_afl_season}"
        )
    expected_numbers = set(expected_round_numbers)
    included = manifest.get("included_rounds")
    if not isinstance(included, list):
        raise ReplayEvidenceError("manifest.included_rounds must be a list")
    included_numbers = [round_row.get("round_number") for round_row in included]
    if len(included_numbers) != len(expected_numbers) or len(included_numbers) != len(set(included_numbers)):
        raise ReplayEvidenceError(
            f"replay package must include exactly {len(expected_numbers)} unique AFL rounds; "
            f"found {len(included_numbers)}: {sorted(n for n in included_numbers if n is not None)}"
        )
    if set(included_numbers) != expected_numbers:
        raise ReplayEvidenceError(
            f"replay package rounds {sorted(set(included_numbers))} do not match the required set "
            f"{sorted(expected_numbers)}"
        )
    for round_row in included:
        if round_row.get("round_id") is None:
            raise ReplayEvidenceError("manifest.included_rounds entry is missing round_id")
    # manifest.included_rounds is a summary the acquirer wrote alongside the
    # authoritative `rounds` evidence, not itself authoritative -- cross-check
    # it against the round records ReplayAflDataSource actually loaded for
    # the resolved season, in *both* directions: every declared round_id
    # must resolve to a matching evidence round_number (a corrupted/
    # mislabelled summary entry fails here rather than only surfacing later,
    # deep inside replay's own `get_round` lookup), and every round the
    # resolved season's evidence actually carries must itself be declared
    # (a physically-present-but-undeclared extra round -- e.g. an AFL Round
    # 21 record smuggled into "rounds" while the manifest still claims
    # exactly 10-20 -- fails here instead of silently remaining reachable
    # through the returned, supposedly fully-validated source).
    declared_round_ids = {row["round_id"] for row in included}
    actual_rounds = {round_.round_id: round_.round_number for round_ in source.get_rounds(resolved_season.season_id)}
    missing_from_evidence = declared_round_ids - set(actual_rounds)
    if missing_from_evidence:
        raise ReplayEvidenceError(
            f"manifest.included_rounds references round_id(s) {sorted(missing_from_evidence)} not present "
            f"in the evidence's rounds for season {resolved_season.season_id}"
        )
    undeclared_in_manifest = set(actual_rounds) - declared_round_ids
    if undeclared_in_manifest:
        raise ReplayEvidenceError(
            f"replay package's season {resolved_season.season_id} evidence contains round_id(s) "
            f"{sorted(undeclared_in_manifest)} not declared in manifest.included_rounds"
        )
    all_matches = []
    for round_row in included:
        round_id = round_row["round_id"]
        actual_number = actual_rounds[round_id]
        if actual_number != round_row.get("round_number"):
            raise ReplayEvidenceError(
                f"manifest.included_rounds declares round_id {round_id} as round_number "
                f"{round_row.get('round_number')!r}, but the evidence round record itself declares "
                f"round_number {actual_number!r}"
            )
        all_matches.extend(source.get_matches(round_id))
    match_ids = [match.match_id for match in all_matches]
    if len(match_ids) != len(set(match_ids)):
        raise ReplayEvidenceError(f"replay package contains duplicate match identities: {sorted(match_ids)}")
    if manifest.get("match_count") != len(match_ids):
        raise ReplayEvidenceError(
            f"replay package manifest.match_count {manifest.get('match_count')!r} does not match "
            f"{len(match_ids)} matches loaded for its included rounds"
        )
    if manifest.get("player_stat_match_count") != len(match_ids):
        raise ReplayEvidenceError(
            "replay package final-stat coverage is incomplete: "
            f"player_stat_match_count={manifest.get('player_stat_match_count')!r}, match_count={len(match_ids)}"
        )
    for match in all_matches:
        if match.start_time_utc is None:
            raise ReplayEvidenceError(f"match {match.match_id} has no scheduled start time")
        try:
            parsed = datetime.fromisoformat(str(match.start_time_utc).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timestamp is not timezone-aware")
        except (TypeError, ValueError) as exc:
            raise ReplayEvidenceError(
                f"match {match.match_id} has an invalid scheduled start time: {match.start_time_utc!r}"
            ) from exc
    return source


def apply_checkpoint(state_path: str | Path, *, effective_at: str, stage: str, round_id: int | None) -> dict[str, Any]:
    """Advance (or initialise) a replay checkpoint JSON file at `state_path`.

    Deliberately evidence-agnostic: this only ever reads/writes the
    checkpoint file itself, never the evidence package it will later be
    paired with, so a fresh second-half checkpoint never needs -- and this
    function never requires -- the first-half evidence namespace's
    `finalised_round_ids` (see `docs/2026-second-half-replay-playbook.md`
    section D step 3). `ReplayAflDataSource._load` separately rejects any
    `finalised_round_ids` entry absent from whichever evidence package is
    actually loaded alongside this checkpoint."""
    if stage not in ("scheduled", "final-results"):
        raise ReplayEvidenceError(f"unsupported replay checkpoint stage: {stage!r}")
    clock = ReplayClock.from_iso(effective_at)
    target = Path(state_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    finalised_round_ids: set[int] = set()
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing.get("schema") != "bbbffl.replay-checkpoint/v1":
            raise ReplayEvidenceError(f"unsupported replay checkpoint schema: {existing.get('schema')!r}")
        previous = ReplayClock.from_iso(existing["effective_at"])
        if clock.now() < previous.now():
            raise ReplayEvidenceError(
                f"replay effective time cannot move backwards: {clock.now().isoformat()} < {previous.now().isoformat()}"
            )
        finalised_round_ids.update(int(value) for value in existing.get("finalised_round_ids", []))
    if stage == "final-results":
        if round_id is None:
            raise ReplayEvidenceError("--round-id is required with --stage final-results")
        finalised_round_ids.add(round_id)
    elif round_id is not None:
        raise ReplayEvidenceError("--round-id is only valid with --stage final-results")
    payload = {
        "schema": "bbbffl.replay-checkpoint/v1",
        "effective_at": clock.now().isoformat(),
        "stage": stage,
        "finalised_round_ids": sorted(finalised_round_ids),
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(target)
    return payload


def write_json_pair_atomic(items: list[tuple[dict, str | Path]]) -> None:
    """Write every ``(payload, path)`` pair via temp-file + replace, staging
    *all* temp files before replacing *any* target.

    A reader never observes a partially-written file, and a failure while
    staging any item (e.g. a read-only directory) leaves every target
    untouched -- so writing the acquisition CLI's evidence and player-pool
    files as one call here never replaces one of the pair while leaving the
    other stale, which a naive write-one-then-the-other sequence could.

    Every target must resolve to a distinct path. Two items sharing a
    destination (most plausibly `--output` and `--player-pool-output`
    accidentally given the same path) would otherwise share one temp file:
    the second item's write clobbers the first item's staged content before
    either replace runs, so the destination ends up holding the wrong
    payload -- rejected up front instead.

    Every target that already exists must be a regular file. `replace()`
    on an existing directory raises, and since the replace loop below runs
    after every item is staged, an earlier target could already have been
    replaced by the time a later one fails that way -- rejected up front,
    before any target is touched, rather than partway through the loop.

    Temp filenames are randomised (not a deterministic `.tmp` suffix): a
    deterministic name derived from one target could otherwise collide with
    another item's *actual* requested target (e.g. `--output pool.json.tmp
    --player-pool-output pool.json`), which would silently corrupt that
    target during staging -- before either item's replace even runs.

    Any temp file this call created but did not end up moving into its
    target (staging failed partway, or a target's replace itself failed) is
    removed before the exception propagates, so a caller retrying a failed
    acquisition against a bad destination never accumulates orphaned
    (potentially large, since these mirror the evidence payload) temp files
    on disk."""
    resolved_targets = [Path(path).resolve() for _, path in items]
    if len(set(resolved_targets)) != len(resolved_targets):
        raise ValueError(
            "write_json_pair_atomic requires distinct output paths, got duplicates among: "
            f"{[str(target) for target in resolved_targets]}"
        )
    non_regular = [target for target in resolved_targets if target.exists() and not target.is_file()]
    if non_regular:
        raise ValueError(
            f"write_json_pair_atomic targets must be regular files, not directories: "
            f"{[str(target) for target in non_regular]}"
        )
    target_set = set(resolved_targets)
    created: list[Path] = []
    staged: list[tuple[Path, Path]] = []
    try:
        for (payload, _path), target in zip(items, resolved_targets):
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
            if temporary in target_set:
                # Astronomically unlikely (a 128-bit random collision), but
                # fail closed rather than silently overwriting another
                # item's target.
                raise ValueError(f"write_json_pair_atomic temp path collides with an output target: {temporary}")
            # Tracked before write_text runs: a write that fails partway
            # (e.g. ENOSPC) can still leave a partial file at `temporary`,
            # and it must be cleaned up too, not just a fully-written one.
            created.append(temporary)
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            staged.append((temporary, target))
        for temporary, target in staged:
            temporary.replace(target)
    except BaseException:
        for temporary in created:
            temporary.unlink(missing_ok=True)
        raise


def write_json_atomic(payload: dict, path: str | Path) -> None:
    """Write a single ``payload`` as JSON via temp-file + replace."""
    write_json_pair_atomic([(payload, path)])


def write_package(payload: dict, path: str | Path) -> None:
    write_json_atomic(payload, path)


def package_summary(source: ReplayAflDataSource) -> str:
    m = source.manifest
    return (
        f"validation PASS\nseason: {m.get('afl_season')}\nrounds: {len(m.get('included_rounds', []))}\n"
        f"matches: {m.get('match_count')}\nstats coverage: {m.get('player_stat_match_count')}/{m.get('match_count')}\n"
        f"roster coverage: {m.get('roster_coverage')}\npackage: {m.get('package_version')}\nacquired: {m.get('acquired_at')}"
    )
