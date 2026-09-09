"""Deterministic tests for the second-half (AFL R10-24) replay evidence
acquisition/validation path added for issue #174, extended from R10-20 to
R10-24 to also acquire the historical AFL evidence a later finals/
SuperScore replay will need (the second-half *ordinary* replay, #168,
still only consumes rounds 10-20 itself -- see `acquire_second_half_2026`'s
docstring).

Mirrors the structure of `tests/test_replay_acquisition.py` (the first-half
suite, which this module leaves entirely unchanged) but drives
`acquire_second_half_2026` / `scripts.second_half_replay` /
`validate_replay_package` instead. Round IDs here (1353-1367) deliberately
continue on from the first-half fixture's Opening Round/R1-9 IDs (100-109
in the first-half tests; the real 2026 season's first-half AFL round IDs
are 1343-1352 per `docs/evidence/2026-first-half-replay/phase-one-closeout.md`)
so a stray cross-half checkpoint or evidence reference is obviously wrong
rather than coincidentally valid.
"""

import contextlib
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app.lockouts import LockState, evaluate_match_lock
from app.replay import ReplayAflDataSource, ReplayEvidenceError
from app.replay_acquisition import (
    SECOND_HALF_ROUND_NUMBERS,
    acquire_first_half_2026,
    acquire_second_half_2026,
    apply_checkpoint,
    validate_replay_package,
    write_package,
)
from scripts import second_half_replay

SECOND_HALF_ROUND_ID = {n: 1343 + n for n in range(10, 25)}  # 1353..1367, contiguous with the real 1343-1352 first half


class Api:
    """Fake consumer API modelling the real AFL-api v1 contract, scoped to
    AFL rounds 10-24 for the second half."""

    def __init__(
        self,
        *,
        players=None,
        no_roster=False,
        empty_stats=None,
        finality="final",
        extra_rounds=(),
        round_order=None,
        duplicate_match_round=None,
        duplicate_stat_player=None,
    ):
        self.no_roster = no_roster
        self.empty_stats = empty_stats
        self.finality = finality
        self.calls = []
        self.players = (
            players
            if players is not None
            else [
                {
                    "canonical_player_id": 44,
                    "display_name": "Participant",
                    "team": {"team_id": 1, "name": "A"},
                    "identifiers": {"provider": "p44"},
                },
                {
                    "canonical_player_id": 99,
                    "display_name": "Eligible non-participant",
                    "team": {"team_id": 2, "name": "B"},
                    "identifiers": {"provider": "p99"},
                },
            ]
        )
        self.extra_rounds = list(extra_rounds)
        self.round_order = round_order
        self.duplicate_match_round = duplicate_match_round
        self.duplicate_stat_player = duplicate_stat_player

    def get(self, path):
        self.calls.append(path)
        if path == "/api/v1/seasons":
            return {
                "seasons": [
                    {"season_id": 91, "year": 2025},
                    {"season_id": 712, "year": 2026, "current_round_number": 15},
                ]
            }
        if path.startswith("/api/v1/seasons/712/players"):
            return self._players_page(path)
        if path == "/api/v1/seasons/712/rounds":
            rounds = [
                {
                    "round_id": SECOND_HALF_ROUND_ID[n],
                    "round_number": n,
                    "name": f"Round {n}",
                    "abbreviation": f"R{n}",
                    "byes": [],
                }
                for n in range(10, 25)
            ]
            rounds.extend(self.extra_rounds)
            if self.round_order:
                rounds.sort(key=lambda r: self.round_order.index(r["round_id"]))
            return {"rounds": rounds}
        if "/rounds/" in path:
            round_id = int(path.split("/")[4])
            if self.duplicate_match_round == round_id:
                # A genuine upstream duplicate/ambiguous acquisition: this
                # round's match correctly claims *this* round_id (so it
                # passes the per-match round-consistency check) but reuses
                # the match_id already served for the immediately preceding
                # round.
                match_id = (round_id - 1) * 10
            else:
                match_id = round_id * 10
            return {
                "matches": [
                    {
                        "match_id": match_id,
                        "round_id": round_id,
                        "status": "CONCLUDED",
                        "start_time_utc": f"2026-{6 + (round_id % 4):02d}-{(round_id % 27) + 1:02d}T08:00:00Z",
                        "home_team": {"team_id": 1, "name": "A"},
                        "away_team": {"team_id": 2, "name": "B"},
                        "provider_match_id": f"m-{round_id}",
                    }
                ]
            }
        match_id = int(path.split("/")[4])
        if path.endswith("player-stats"):
            rows = []
            if self.empty_stats == match_id:
                rows = []
            else:
                rows = [
                    {
                        "canonical_player_id": 44,
                        "display_name": "Participant",
                        "team_id": 1,
                        "identifiers": {"provider": "p44"},
                        "stats": {"goals": 1, "behinds": 2, "disposals": 3, "marks": 4, "hitouts": 5, "tackles": 6},
                    }
                ]
                if self.duplicate_stat_player == match_id:
                    rows.append(dict(rows[0]))
            return {"lifecycle": {"finality": self.finality}, "players": rows}
        if self.no_roster:
            raise RuntimeError("not captured")
        return {"selected": [44], "emergencies": [], "ins": [], "outs": []}

    def _players_page(self, path):
        query = parse_qs(urlsplit(path).query)
        limit = int(query.get("limit", ["250"])[0])
        offset = int(query.get("offset", ["0"])[0])
        page = self.players[offset : offset + limit]
        return {"players": page, "limit": limit, "offset": offset}


def checkpoint_file(path, effective_at, finalised=(), stage="scheduled"):
    path.write_text(
        json.dumps(
            {
                "schema": "bbbffl.replay-checkpoint/v1",
                "effective_at": effective_at,
                "stage": stage,
                "finalised_round_ids": list(finalised),
            }
        )
    )


# --- Season resolution / round selection ------------------------------------


def test_acquisition_resolves_2026_season_by_metadata_not_hard_coded_id():
    api = Api(no_roster=True)
    payload = acquire_second_half_2026(api, source_base_url="http://api")
    assert payload["seasons"][0]["season_id"] == 712
    assert [c for c in api.calls if c == "/api/v1/seasons"]
    # The season_id used for every downstream call is the resolved one, not
    # the AFL calendar year -- 712 != 2026.
    assert any("/seasons/712/" in c for c in api.calls)


def test_acquisition_selects_exactly_afl_rounds_10_to_24():
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    numbers = [r["round_number"] for r in payload["rounds"]]
    assert numbers == list(range(10, 25))
    assert len(numbers) == 15
    assert set(SECOND_HALF_ROUND_NUMBERS) == set(range(10, 25))


def test_acquisition_output_round_and_match_ordering_is_deterministic_regardless_of_api_order():
    shuffled_order = [SECOND_HALF_ROUND_ID[n] for n in (15, 10, 24, 20, 12, 11, 23, 19, 13, 22, 18, 14, 21, 17, 16)]
    payload_a = acquire_second_half_2026(Api(no_roster=True, round_order=shuffled_order), source_base_url="http://api")
    payload_b = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    assert [r["round_id"] for r in payload_a["rounds"]] == [r["round_id"] for r in payload_b["rounds"]]
    assert [r["round_number"] for r in payload_a["rounds"]] == list(range(10, 25))
    assert [m["match_id"] for m in payload_a["matches"]] == [m["match_id"] for m in payload_b["matches"]]


def test_acquisition_missing_round_fails_closed():
    class MissingRoundApi(Api):
        def get(self, path):
            if path == "/api/v1/seasons/712/rounds":
                payload = super().get(path)
                payload["rounds"] = [r for r in payload["rounds"] if r["round_number"] != 15]
                return payload
            return super().get(path)

    with pytest.raises(ReplayEvidenceError, match="requires exactly AFL rounds 10-24"):
        acquire_second_half_2026(MissingRoundApi(no_roster=True), source_base_url="http://api")


def test_acquisition_missing_round_in_21_to_24_extension_fails_closed():
    """Missing/incomplete evidence must fail acquisition regardless of
    where in R10-24 it occurs -- not only within the pre-existing R10-20
    range."""

    class MissingLateRoundApi(Api):
        def get(self, path):
            if path == "/api/v1/seasons/712/rounds":
                payload = super().get(path)
                payload["rounds"] = [r for r in payload["rounds"] if r["round_number"] != 22]
                return payload
            return super().get(path)

    with pytest.raises(ReplayEvidenceError, match="requires exactly AFL rounds 10-24"):
        acquire_second_half_2026(MissingLateRoundApi(no_roster=True), source_base_url="http://api")


def test_acquisition_extra_round_outside_10_24_is_not_selected():
    extra = {"round_id": 9999, "round_number": 25, "name": "Round 25", "abbreviation": "R25", "byes": []}
    payload = acquire_second_half_2026(Api(no_roster=True, extra_rounds=[extra]), source_base_url="http://api")
    assert [r["round_number"] for r in payload["rounds"]] == list(range(10, 25))
    assert 9999 not in [r["round_id"] for r in payload["rounds"]]


def test_duplicate_round_number_with_different_round_id_fails_closed():
    duplicate = {"round_id": 5001, "round_number": 15, "name": "Round 15 (dup)", "abbreviation": "R15b", "byes": []}
    with pytest.raises(ReplayEvidenceError, match="duplicate/ambiguous round_number"):
        acquire_second_half_2026(Api(no_roster=True, extra_rounds=[duplicate]), source_base_url="http://api")


def test_exact_duplicate_round_id_entry_fails_closed():
    duplicate = dict(round_id=SECOND_HALF_ROUND_ID[15], round_number=15, name="Round 15", abbreviation="R15", byes=[])
    with pytest.raises(ReplayEvidenceError, match="duplicate round_id entries"):
        acquire_second_half_2026(Api(no_roster=True, extra_rounds=[duplicate]), source_base_url="http://api")


def test_duplicate_round_number_within_21_to_24_extension_fails_closed():
    duplicate = {"round_id": 5002, "round_number": 22, "name": "Round 22 (dup)", "abbreviation": "R22b", "byes": []}
    with pytest.raises(ReplayEvidenceError, match="duplicate/ambiguous round_number"):
        acquire_second_half_2026(Api(no_roster=True, extra_rounds=[duplicate]), source_base_url="http://api")


# --- Match/stat completeness --------------------------------------------------


def test_all_matches_across_fifteen_rounds_are_acquired():
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    assert len(payload["matches"]) == 15
    assert len(payload["player_stats"]) == 15
    assert {m["round_id"] for m in payload["matches"]} == set(SECOND_HALF_ROUND_ID.values())


def test_acquisition_includes_rounds_21_to_24_with_full_match_stat_coverage():
    """Rounds 21-24 are acquired now even though the second-half *ordinary*
    replay (#168) only consumes rounds 10-20 itself -- the later finals/
    SuperScore replay needs this same historical AFL evidence. Proves it is
    actually present with full match/stat coverage, not merely declared."""
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    late_rounds = {r["round_number"]: r for r in payload["rounds"] if r["round_number"] >= 21}
    assert set(late_rounds) == {21, 22, 23, 24}
    late_round_ids = {r["round_id"] for r in late_rounds.values()}
    late_matches = [m for m in payload["matches"] if m["round_id"] in late_round_ids]
    assert len(late_matches) == 4
    assert all(str(m["match_id"]) in payload["player_stats"] for m in late_matches)
    assert all(payload["player_stats"][str(m["match_id"])] for m in late_matches)


def test_missing_required_stats_fails_with_match_identity():
    missing_match_id = SECOND_HALF_ROUND_ID[13] * 10
    with pytest.raises(ReplayEvidenceError, match=f"AFL match {missing_match_id}"):
        acquire_second_half_2026(Api(empty_stats=missing_match_id), source_base_url="http://api")


def test_missing_required_stats_fails_for_a_match_in_the_21_to_24_extension():
    missing_match_id = SECOND_HALF_ROUND_ID[23] * 10
    with pytest.raises(ReplayEvidenceError, match=f"AFL match {missing_match_id}"):
        acquire_second_half_2026(Api(empty_stats=missing_match_id), source_base_url="http://api")


@pytest.mark.parametrize("finality", ["partial", "not_available", None, "unknown"])
def test_non_final_stats_response_fails_closed(finality):
    with pytest.raises(ReplayEvidenceError, match=rf"finality={finality!r}"):
        acquire_second_half_2026(Api(finality=finality), source_base_url="http://api")


def test_final_stats_response_is_accepted_for_every_match():
    payload = acquire_second_half_2026(Api(finality="final"), source_base_url="http://api")
    assert len(payload["player_stats"]) == 15
    assert payload["manifest"]["player_stat_match_count"] == 15


def test_duplicate_match_across_rounds_fails_closed():
    duplicate_round_id = SECOND_HALF_ROUND_ID[16]
    with pytest.raises(ReplayEvidenceError, match="already acquired under a different selected round"):
        acquire_second_half_2026(
            Api(no_roster=True, duplicate_match_round=duplicate_round_id), source_base_url="http://api"
        )


def test_duplicate_player_stat_row_within_a_match_fails_closed():
    duplicate_match_id = SECOND_HALF_ROUND_ID[12] * 10
    with pytest.raises(ReplayEvidenceError, match="duplicate player-stat rows"):
        acquire_second_half_2026(
            Api(no_roster=True, duplicate_stat_player=duplicate_match_id), source_base_url="http://api"
        )


# --- Scheduled starts / lockout ----------------------------------------------


def test_scheduled_starts_are_preserved_and_drive_lockout(tmp_path):
    payload = acquire_second_half_2026(
        Api(no_roster=True), source_base_url="http://api", acquired_at=datetime(2026, 6, 1, tzinfo=timezone.utc)
    )
    evidence = tmp_path / "evidence.json"
    write_package(payload, evidence)
    state = tmp_path / "checkpoint.json"
    r10_match = next(m for m in payload["matches"] if m["round_id"] == SECOND_HALF_ROUND_ID[10])
    start = r10_match["start_time_utc"]

    checkpoint_file(state, "2026-01-01T00:00:00Z")
    before = ReplayAflDataSource(evidence, checkpoint_path=state)
    match_before = before.get_matches(SECOND_HALF_ROUND_ID[10])[0]
    assert match_before.start_time_utc == start
    assert evaluate_match_lock(match_before, before.clock.now())[0] is LockState.EDITABLE

    checkpoint_file(state, start)
    after = ReplayAflDataSource(evidence, checkpoint_path=state)
    match_after = after.get_matches(SECOND_HALF_ROUND_ID[10])[0]
    assert evaluate_match_lock(match_after, after.clock.now())[0] is LockState.LOCKED


# --- Roster evidence (optional/available semantics) --------------------------


def test_roster_evidence_follows_optional_available_semantics():
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    assert payload["manifest"]["roster_coverage"]["available"] == 0
    assert len(payload["manifest"]["roster_coverage"]["unavailable"]) == 15
    assert all(v is None for v in payload["rosters"].values())


def test_roster_evidence_is_captured_when_available():
    payload = acquire_second_half_2026(Api(no_roster=False), source_base_url="http://api")
    assert payload["manifest"]["roster_coverage"]["available"] == 15
    assert payload["manifest"]["roster_coverage"]["unavailable"] == []
    assert all(v is not None for v in payload["rosters"].values())


# --- Manifest / provenance ----------------------------------------------------


def test_manifest_identity_and_no_fabricated_evidence(tmp_path):
    payload = acquire_second_half_2026(
        Api(no_roster=True),
        source_base_url="https://user:secret@example.test:8443/private",
        acquired_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    assert payload["manifest"]["id"] == "afl-2026-second-half"
    assert payload["manifest"]["package_version"] == "bbbffl.second-half/v1"
    assert payload["manifest"]["afl_season"] == 2026
    assert payload["manifest"]["source_api"] == "https://example.test:8443"
    assert "secret" not in json.dumps(payload)
    assert payload["manifest"]["match_count"] == 15
    assert [r["round_number"] for r in payload["manifest"]["included_rounds"]] == list(range(10, 25))
    assert payload["manifest"]["lifecycle_semantics"] == "scheduled-start-plus-final-results-checkpoint"


def test_acquisition_timestamp_must_be_timezone_aware():
    with pytest.raises(ReplayEvidenceError, match="timezone-aware"):
        acquire_second_half_2026(Api(), source_base_url="http://api", acquired_at=datetime(2026, 6, 1))


# --- Offline package validation ------------------------------------------------


def _write_valid_package(tmp_path, *, acquired_at=None):
    payload = acquire_second_half_2026(
        Api(no_roster=True),
        source_base_url="http://api",
        acquired_at=acquired_at or datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    evidence = tmp_path / "evidence.json"
    write_package(payload, evidence)
    state = tmp_path / "checkpoint.json"
    apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    return evidence, state, payload


def test_validate_replay_package_passes_for_a_complete_package(tmp_path):
    evidence, state, _ = _write_valid_package(tmp_path)
    source = validate_replay_package(
        evidence,
        state,
        expected_package_version="bbbffl.second-half/v1",
        expected_afl_season=2026,
        expected_round_numbers=SECOND_HALF_ROUND_NUMBERS,
    )
    assert source.manifest["match_count"] == 15


def test_validate_replay_package_loads_with_no_live_api_object_at_all(tmp_path):
    """The 'AFL-api unavailable' proof: validation touches only the local
    evidence/checkpoint files, never a consumer API client."""
    evidence, state, _ = _write_valid_package(tmp_path)
    source = ReplayAflDataSource(evidence, checkpoint_path=state)
    assert source.manifest["afl_season"] == 2026


def test_validate_replay_package_rejects_unsupported_package_version(tmp_path):
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    payload["manifest"]["package_version"] = "bbbffl.first-half/v1"
    evidence = tmp_path / "evidence.json"
    write_package(payload, evidence)
    state = tmp_path / "checkpoint.json"
    apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    with pytest.raises(ReplayEvidenceError, match="unsupported replay package version"):
        validate_replay_package(
            evidence,
            state,
            expected_package_version="bbbffl.second-half/v1",
            expected_afl_season=2026,
            expected_round_numbers=SECOND_HALF_ROUND_NUMBERS,
        )


def test_validate_replay_package_rejects_wrong_season_manifest_label(tmp_path):
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    payload["manifest"]["afl_season"] = 2025
    evidence = tmp_path / "evidence.json"
    write_package(payload, evidence)
    state = tmp_path / "checkpoint.json"
    apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    with pytest.raises(ReplayEvidenceError, match="manifest declares AFL season 2025"):
        validate_replay_package(
            evidence,
            state,
            expected_package_version="bbbffl.second-half/v1",
            expected_afl_season=2026,
            expected_round_numbers=SECOND_HALF_ROUND_NUMBERS,
        )


def test_validate_replay_package_rejects_season_mismatch_between_manifest_and_evidence(tmp_path):
    """The manifest's afl_season is a label the acquirer wrote, not proof by
    itself -- a manifest that still claims 2026 while the evidence's own
    `seasons` record actually declares a different year must fail, not
    read PASS for evidence that doesn't actually resolve 2026."""
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    payload["seasons"][0]["year"] = 2025  # manifest.afl_season is left at 2026
    evidence = tmp_path / "evidence.json"
    write_package(payload, evidence)
    state = tmp_path / "checkpoint.json"
    apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    with pytest.raises(ReplayEvidenceError, match="does not resolve exactly one season for year 2026"):
        validate_replay_package(
            evidence,
            state,
            expected_package_version="bbbffl.second-half/v1",
            expected_afl_season=2026,
            expected_round_numbers=SECOND_HALF_ROUND_NUMBERS,
        )


def test_validate_replay_package_rejects_undeclared_extra_round_in_evidence(tmp_path):
    """A round physically present in the evidence's `rounds` section but
    not declared in manifest.included_rounds must fail closed -- otherwise
    it stays silently reachable through the returned, supposedly
    fully-validated source (e.g. via get_rounds()) even though validation
    only ever walked the manifest's declared entries."""
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    season_id = payload["seasons"][0]["season_id"]
    extra_round = dict(payload["rounds"][0])
    extra_round["round_id"] = 999999
    extra_round["round_number"] = 25
    extra_round["season_id"] = season_id
    payload["rounds"].append(extra_round)
    evidence = tmp_path / "evidence.json"
    write_package(payload, evidence)
    state = tmp_path / "checkpoint.json"
    apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    with pytest.raises(ReplayEvidenceError, match="not declared in manifest.included_rounds"):
        validate_replay_package(
            evidence,
            state,
            expected_package_version="bbbffl.second-half/v1",
            expected_afl_season=2026,
            expected_round_numbers=SECOND_HALF_ROUND_NUMBERS,
        )


def test_validate_replay_package_rejects_missing_round(tmp_path):
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    payload["manifest"]["included_rounds"] = [
        r for r in payload["manifest"]["included_rounds"] if r["round_number"] != 15
    ]
    evidence = tmp_path / "evidence.json"
    write_package(payload, evidence)
    state = tmp_path / "checkpoint.json"
    apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    with pytest.raises(ReplayEvidenceError, match="must include exactly 15 unique AFL rounds"):
        validate_replay_package(
            evidence,
            state,
            expected_package_version="bbbffl.second-half/v1",
            expected_afl_season=2026,
            expected_round_numbers=SECOND_HALF_ROUND_NUMBERS,
        )


def test_validate_replay_package_rejects_missing_round_in_21_to_24_extension(tmp_path):
    """Missing/incomplete evidence must fail validation regardless of where
    in R10-24 it occurs -- not only within the pre-existing R10-20 range."""
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    payload["manifest"]["included_rounds"] = [
        r for r in payload["manifest"]["included_rounds"] if r["round_number"] != 23
    ]
    evidence = tmp_path / "evidence.json"
    write_package(payload, evidence)
    state = tmp_path / "checkpoint.json"
    apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    with pytest.raises(ReplayEvidenceError, match="must include exactly 15 unique AFL rounds"):
        validate_replay_package(
            evidence,
            state,
            expected_package_version="bbbffl.second-half/v1",
            expected_afl_season=2026,
            expected_round_numbers=SECOND_HALF_ROUND_NUMBERS,
        )


def test_validate_replay_package_rejects_mismatched_match_count(tmp_path):
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    payload["manifest"]["match_count"] = 999
    evidence = tmp_path / "evidence.json"
    write_package(payload, evidence)
    state = tmp_path / "checkpoint.json"
    apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    with pytest.raises(ReplayEvidenceError, match="manifest.match_count"):
        validate_replay_package(
            evidence,
            state,
            expected_package_version="bbbffl.second-half/v1",
            expected_afl_season=2026,
            expected_round_numbers=SECOND_HALF_ROUND_NUMBERS,
        )


def test_validate_replay_package_rejects_manifest_round_number_mismatch(tmp_path):
    """The manifest's `included_rounds` summary is written by the acquirer
    alongside the authoritative `rounds` evidence, but is not itself
    authoritative -- a corrupted/mislabelled summary entry (a round_id whose
    evidence round record actually declares a different round_number) must
    fail validation rather than only surfacing later, deep inside replay's
    own `get_round` lookup."""
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    # Swap two entries' declared round_number (not round_id): the *set* of
    # declared round numbers is still exactly {10..24} (so the earlier
    # "must match required set" check still passes), but each entry's
    # (round_id, round_number) pairing no longer matches the round record
    # the evidence's own "rounds" section actually declares for that
    # round_id -- exactly the corruption the earlier, coarser set-only
    # check could not detect.
    round_15 = next(r for r in payload["manifest"]["included_rounds"] if r["round_number"] == 15)
    round_16 = next(r for r in payload["manifest"]["included_rounds"] if r["round_number"] == 16)
    round_15["round_number"], round_16["round_number"] = round_16["round_number"], round_15["round_number"]
    evidence = tmp_path / "evidence.json"
    write_package(payload, evidence)
    state = tmp_path / "checkpoint.json"
    apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    with pytest.raises(ReplayEvidenceError, match="evidence round record itself declares round_number 15"):
        validate_replay_package(
            evidence,
            state,
            expected_package_version="bbbffl.second-half/v1",
            expected_afl_season=2026,
            expected_round_numbers=SECOND_HALF_ROUND_NUMBERS,
        )


def test_validate_replay_package_rejects_manifest_round_id_absent_from_evidence(tmp_path):
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    round_15 = next(r for r in payload["manifest"]["included_rounds"] if r["round_number"] == 15)
    round_15["round_id"] = 999999  # no such round in payload["rounds"]
    evidence = tmp_path / "evidence.json"
    write_package(payload, evidence)
    state = tmp_path / "checkpoint.json"
    apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    with pytest.raises(ReplayEvidenceError, match="not present in the evidence's rounds"):
        validate_replay_package(
            evidence,
            state,
            expected_package_version="bbbffl.second-half/v1",
            expected_afl_season=2026,
            expected_round_numbers=SECOND_HALF_ROUND_NUMBERS,
        )


def test_validate_replay_package_rejects_incomplete_stats_coverage(tmp_path):
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    payload["manifest"]["player_stat_match_count"] = 10
    evidence = tmp_path / "evidence.json"
    write_package(payload, evidence)
    state = tmp_path / "checkpoint.json"
    apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    with pytest.raises(ReplayEvidenceError, match="final-stat coverage is incomplete"):
        validate_replay_package(
            evidence,
            state,
            expected_package_version="bbbffl.second-half/v1",
            expected_afl_season=2026,
            expected_round_numbers=SECOND_HALF_ROUND_NUMBERS,
        )


def test_validate_replay_package_rejects_malformed_scheduled_start(tmp_path):
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    payload["matches"][0]["start_time_utc"] = "not-a-timestamp"
    evidence = tmp_path / "evidence.json"
    write_package(payload, evidence)
    state = tmp_path / "checkpoint.json"
    apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    # Caught while ReplayAflDataSource itself resolves match status at the
    # replay effective time -- fails closed before validate_replay_package's
    # own scheduled-start check ever runs.
    with pytest.raises(ReplayEvidenceError, match="invalid replay effective timestamp"):
        validate_replay_package(
            evidence,
            state,
            expected_package_version="bbbffl.second-half/v1",
            expected_afl_season=2026,
            expected_round_numbers=SECOND_HALF_ROUND_NUMBERS,
        )


def test_validate_replay_package_rejects_null_scheduled_start(tmp_path):
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    payload["matches"][0]["start_time_utc"] = None
    evidence = tmp_path / "evidence.json"
    write_package(payload, evidence)
    state = tmp_path / "checkpoint.json"
    apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    # A null start silently reads as "not yet started" inside
    # ReplayAflDataSource itself, so validate_replay_package's own explicit
    # check is what catches it.
    with pytest.raises(ReplayEvidenceError, match="has no scheduled start time"):
        validate_replay_package(
            evidence,
            state,
            expected_package_version="bbbffl.second-half/v1",
            expected_afl_season=2026,
            expected_round_numbers=SECOND_HALF_ROUND_NUMBERS,
        )


def test_validate_replay_package_rejects_structurally_corrupt_package(tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_text("not json at all")
    state = tmp_path / "checkpoint.json"
    apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    with pytest.raises(ReplayEvidenceError):
        validate_replay_package(
            evidence,
            state,
            expected_package_version="bbbffl.second-half/v1",
            expected_afl_season=2026,
            expected_round_numbers=SECOND_HALF_ROUND_NUMBERS,
        )


# --- Checkpoint compatibility (fresh second-half checkpoint, issue #174 #7) --


def test_fresh_second_half_checkpoint_never_requires_first_half_finalised_round_ids(tmp_path):
    evidence, _, _ = _write_valid_package(tmp_path)
    fresh_state = tmp_path / "fresh-checkpoint.json"
    payload = apply_checkpoint(fresh_state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    assert payload["finalised_round_ids"] == []
    # Loads cleanly with nothing finalised yet -- no dependency on the
    # first-half evidence namespace's own finalised_round_ids (1343-1352).
    source = ReplayAflDataSource(evidence, checkpoint_path=fresh_state)
    assert source.finalised_round_ids == frozenset()
    assert source.get_matches(SECOND_HALF_ROUND_ID[10])[0].status == "UPCOMING"


def test_checkpoint_carrying_first_half_round_ids_is_rejected_against_second_half_evidence(tmp_path):
    evidence, _, _ = _write_valid_package(tmp_path)
    stale_state = tmp_path / "stale-checkpoint.json"
    # Simulates the exact mistake the playbook warns against: copying the
    # first-half checkpoint.json (carrying AFL rounds 1343-1352) verbatim
    # instead of initialising a fresh second-half checkpoint.
    checkpoint_file(stale_state, "2026-06-01T00:00:00Z", finalised=[1343, 1344], stage="final-results")
    with pytest.raises(ReplayEvidenceError, match="unknown finalised AFL rounds"):
        ReplayAflDataSource(evidence, checkpoint_path=stale_state)


def test_checkpoint_command_finalises_second_half_round_and_refuses_rewind(tmp_path):
    state = tmp_path / "checkpoint.json"
    apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    apply_checkpoint(
        state, effective_at="2026-06-08T12:00:00Z", stage="final-results", round_id=SECOND_HALF_ROUND_ID[10]
    )
    assert json.loads(state.read_text())["finalised_round_ids"] == [SECOND_HALF_ROUND_ID[10]]
    with pytest.raises(ReplayEvidenceError, match="cannot move backwards"):
        apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)


# --- First-half behaviour is unaffected by the second-half addition ---------


def test_first_half_acquisition_still_rejects_second_half_shaped_rounds():
    """`acquire_first_half_2026` must still refuse a season whose rounds
    collection only contains AFL R10-24 -- the two acquisition entry points
    stay independently scoped even though they share helpers."""
    api = Api(no_roster=True)  # rounds 10-24 only, no Opening Round / R1-9
    with pytest.raises(ReplayEvidenceError, match="requires one Opening Round and rounds 1-9"):
        acquire_first_half_2026(api, source_base_url="http://api")


def test_second_half_acquisition_still_rejects_first_half_shaped_rounds():
    class FirstHalfShapedApi(Api):
        def get(self, path):
            if path == "/api/v1/seasons/712/rounds":
                return {
                    "rounds": [
                        {"round_id": 100, "round_number": 0, "name": "Opening Round", "byes": []},
                        *[
                            {"round_id": 100 + n, "round_number": n, "name": f"Round {n}", "byes": []}
                            for n in range(1, 10)
                        ],
                    ]
                }
            return super().get(path)

    with pytest.raises(ReplayEvidenceError, match="requires exactly AFL rounds 10-24"):
        acquire_second_half_2026(FirstHalfShapedApi(no_roster=True), source_base_url="http://api")


# --- CLI (scripts.second_half_replay) -----------------------------------------


def _fake_afl_api_client_factory(api):
    class FakeAflApiClient:
        def __init__(self, base_url, api_key):
            self.base_url = base_url
            self.api_key = api_key

        def _get(self, path):
            return api.get(path)

        def close(self):
            pass

    return FakeAflApiClient


def test_cli_successful_acquisition_writes_package_and_prints_pass(tmp_path, monkeypatch, capsys):
    output = tmp_path / "2026-second-half.json"
    monkeypatch.setattr(second_half_replay, "AflApiClient", _fake_afl_api_client_factory(Api(no_roster=True)))
    monkeypatch.setattr(
        "sys.argv",
        ["second_half_replay", "acquire", "--output", str(output), "--base-url", "http://api"],
    )
    assert second_half_replay.main() == 0
    captured = capsys.readouterr()
    assert "acquisition PASS" in captured.out
    assert "rounds: 15" in captured.out
    payload = json.loads(output.read_text())
    assert payload["manifest"]["package_version"] == "bbbffl.second-half/v1"


def test_cli_failed_acquisition_does_not_report_pass_or_write_partial_output(tmp_path, monkeypatch, capsys):
    output = tmp_path / "evidence.json"
    output.write_text("PREVIOUS-GOOD-EVIDENCE")
    monkeypatch.setattr(second_half_replay, "AflApiClient", _fake_afl_api_client_factory(Api(finality="not_available")))
    monkeypatch.setattr(
        "sys.argv",
        ["second_half_replay", "acquire", "--output", str(output), "--base-url", "http://api"],
    )
    assert second_half_replay.main() == 1
    captured = capsys.readouterr()
    assert "PASS" not in captured.out
    assert output.read_text() == "PREVIOUS-GOOD-EVIDENCE"


def test_cli_has_no_player_pool_output_flag(monkeypatch):
    """Deliberate: second-half acquisition must never offer a path that
    invites silently replacing the established `2026-player-pool.json`
    source of truth."""
    monkeypatch.setattr(sys, "argv", ["second_half_replay", "acquire", "--help"])
    buf = io.StringIO()
    with contextlib.suppress(SystemExit), contextlib.redirect_stdout(buf):
        second_half_replay.main()
    assert "--player-pool-output" not in buf.getvalue()


def test_cli_validate_reports_pass_for_a_complete_package(tmp_path, monkeypatch, capsys):
    evidence, state, _ = _write_valid_package(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["second_half_replay", "validate", "--evidence", str(evidence), "--state", str(state)],
    )
    assert second_half_replay.main() == 0
    captured = capsys.readouterr()
    assert "validation PASS" in captured.out
    assert "rounds: 15" in captured.out


def test_cli_validate_fails_closed_on_corrupt_package(tmp_path, monkeypatch, capsys):
    payload = acquire_second_half_2026(Api(no_roster=True), source_base_url="http://api")
    payload["manifest"]["match_count"] = 3
    evidence = tmp_path / "evidence.json"
    write_package(payload, evidence)
    state = tmp_path / "checkpoint.json"
    apply_checkpoint(state, effective_at="2026-06-01T00:00:00Z", stage="scheduled", round_id=None)
    monkeypatch.setattr(
        "sys.argv",
        ["second_half_replay", "validate", "--evidence", str(evidence), "--state", str(state)],
    )
    assert second_half_replay.main() == 1
    captured = capsys.readouterr()
    assert "FAILED" in captured.err


def test_playbook_documents_the_exact_second_half_acquisition_commands():
    root = Path(__file__).resolve().parents[2]
    playbook = (root / "docs/2026-second-half-replay-playbook.md").read_text()
    assert "python -m scripts.second_half_replay acquire" in playbook
    assert "python -m scripts.second_half_replay validate" in playbook
    assert "--output /replay/evidence/2026-second-half.json" in playbook
    # --player-pool-output is discussed (explaining its deliberate absence)
    # but must never appear as an actual command-line flag in this doc.
    assert "acquire \\\n     --player-pool-output" not in playbook
    assert "no equivalent acquisition path" not in playbook


def test_cli_checkpoint_command_creates_fresh_checkpoint(tmp_path, monkeypatch):
    state = tmp_path / "checkpoint.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "second_half_replay",
            "checkpoint",
            "--state",
            str(state),
            "--effective-at",
            "2026-06-01T00:00:00Z",
            "--stage",
            "scheduled",
        ],
    )
    assert second_half_replay.main() == 0
    assert json.loads(state.read_text())["finalised_round_ids"] == []
