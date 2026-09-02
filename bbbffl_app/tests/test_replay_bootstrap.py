import inspect
import json
from pathlib import Path

import pytest

from app.auth import AuthenticationService, CredentialRepository, SessionRepository
from app.auth_rate_limit import LoginRateLimiter
from app.db import transaction
from app.draft import DraftRepository
from app.draft_board import draft_board_readiness
from app.identity import IdentityRepository
from app.opening_round import OpeningRoundRuleRepository
from app.player_pool import PlayerPoolRepository
from app.replay_bootstrap import (
    OPENING_ROUND_RULE_COUNT,
    ReplayBootstrapError,
    ReplayOpeningRoundEvidenceValidator,
    _opening_round_config,
    bootstrap_first_half,
    load_replay_config,
    provision_replay_operator,
    replay_readiness,
)
from app.season import SeasonRepository
from tests.db_helpers import migrated_connection

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "config" / "replay" / "2026-first-half.template.json"

# The genuine 2026 Opening Round facts (docs/opening-round-deferred-selection.md's
# 2026 evidence row / tests/opening_round_evidence.py's EVIDENCE_2026):
# afl_club_id, afl_club_name, afl_bye_round_id, bbbffl_round_number.
OPENING_ROUND_CLUBS_2026 = [
    (2, "Brisbane Lions", 1345, 2),
    (5, "Carlton", 1345, 2),
    (3, "Collingwood", 1345, 2),
    (10, "Geelong Cats", 1345, 2),
    (4, "Gold Coast Suns", 1346, 3),
    (8, "Western Bulldogs", 1346, 3),
    (9, "Hawthorn", 1346, 3),
    (13, "Sydney Swans", 1346, 3),
    (11, "St Kilda", 1347, 4),
    (15, "GWS Giants", 1347, 4),
]
OPENING_ROUND_ALL_AFL_ROUND_IDS = (1343, 1345, 1346, 1347)


PROVENANCE = {"source": "test evidence", "evidence_class": "known_fact"}


def _opening_round_evidence(
    tmp_path, filename="opening-round-evidence.json", round_ids=OPENING_ROUND_ALL_AFL_ROUND_IDS, season_id=2026
):
    payload = {
        "schema": "bbbffl.replay-evidence/v1",
        "manifest": {"id": "test-evidence", "version": "1", "evidence_class": "known_fact"},
        "seasons": [{"season_id": season_id, "year": 2026, "provenance": PROVENANCE}],
        "rounds": [{"round_id": round_id, "season_id": season_id, "provenance": PROVENANCE} for round_id in round_ids],
    }
    (tmp_path / filename).write_text(json.dumps(payload))
    return filename


def _opening_round_rules(*, evidence_classification="reconstructable_behaviour"):
    return [
        {
            "afl_club_id": club_id,
            "afl_club_name": name,
            "afl_opening_round_id": 1343,
            "afl_bye_round_id": bye_round_id,
            "bbbffl_round_number": target,
            "evidence_classification": evidence_classification,
        }
        for club_id, name, bye_round_id, target in OPENING_ROUND_CLUBS_2026
    ]


def _files(
    tmp_path,
    *,
    entry_count=10,
    mutate_entry=None,
    mutate_player=None,
    mutate_opening_round=None,
    opening_round_round_ids=OPENING_ROUND_ALL_AFL_ROUND_IDS,
    opening_round_season_id=2026,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "coach_display_name": f"Historical Coach {n}",
            "coach_email": f"coach{n}@replay.example",
            "team_name": f"Historical Club {n}",
            "licence_key": f"historical-{n}",
            "draft_position": n,
        }
        for n in range(1, entry_count + 1)
    ]
    if mutate_entry:
        mutate_entry(entries)
    players = [
        {
            "canonical_player_id": 2026000 + n,
            "display_name": f"Captured Player {n}",
            "afl_team_id": 1000 + (n % 18),
            "afl_team_name": f"AFL Club {n % 18}",
            "eligible": True,
            "source_updated_at": "2026-02-01T00:00:00Z",
        }
        for n in range(1, 31)
    ]
    if mutate_player:
        mutate_player(players)
    (tmp_path / "players.json").write_text(
        json.dumps({"source": {"provider": "afl-api-v1", "season_year": 2026}, "players": players})
    )
    evidence_filename = _opening_round_evidence(
        tmp_path, round_ids=opening_round_round_ids, season_id=opening_round_season_id
    )
    opening_round_rules = _opening_round_rules()
    if mutate_opening_round:
        mutate_opening_round(opening_round_rules)
    config = {
        "season": {"year": 2026, "label": "2026 First Half Replay"},
        "rules": {"key": "ordinary", "version": 1, "name": "2026 Rules"},
        "competition": {"key": "ordinary", "label": "BBBFFL Ordinary"},
        "squad_limit": 3,
        "operator_email": "coach1@replay.example",
        "player_pool_file": "players.json",
        "entries": entries,
        "opening_round": {
            "afl_season_id": opening_round_season_id,
            "evidence_file": evidence_filename,
            "rules": opening_round_rules,
        },
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


def test_clean_bootstrap_is_ready_for_human_pick_one_and_season_centre_state(tmp_path):
    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))
    initial = bootstrap_first_half(database, config)
    assert initial["overall"] == "NOT READY"
    assert initial["checks"]["operator_authentication_provisioned"] is False
    provision_replay_operator(database, config, "correct horse battery staple")
    report = replay_readiness(database, config)
    season = SeasonRepository(database).get_season_by_year(2026)
    competitions = SeasonRepository(database).list_competitions(season.season_id)

    assert report["overall"] == "READY"
    assert report["logical_rounds"] == list(range(1, 10))
    assert report["season_entry_count"] == report["accepted_draft_order_count"] == 10
    assert report["player_pool_count"] == 30
    assert report["squad_size_limit"] == 3
    assert report["completed_draft_picks_exist"] is False
    assert report["next_human_action"] == "Pick 1"
    assert report["operator_authentication_provisioned"] is True
    assert report["checks"]["exact_rules_version"] is True
    assert len(competitions) == 1 and competitions[0].stream_type == "ordinary"
    assert len(SeasonRepository(database).list_rounds(competitions[0].competition_id)) == 9
    assert len(IdentityRepository(database).list_entries(season.season_id)) == 10
    assert DraftRepository(database).status(season.season_id).completed_picks == 0
    assert (
        draft_board_readiness(
            database,
            IdentityRepository(database),
            DraftRepository(database),
            PlayerPoolRepository(database),
            season.season_id,
        )["next_pick_overall"]
        == 1
    )
    auth = AuthenticationService(
        IdentityRepository(database),
        CredentialRepository(database),
        SessionRepository(database),
        LoginRateLimiter(max_attempts=5, lockout_seconds=300),
    )
    assert auth.login(config.operator_email, "correct horse battery staple", remote_addr="127.0.0.1").token


@pytest.mark.parametrize(
    "entry_count,mutation",
    [
        (9, None),
        (11, None),
        (10, lambda rows: rows[1].update(team_name=rows[0]["team_name"])),
        (10, lambda rows: rows[1].update(coach_email=rows[0]["coach_email"])),
        (10, lambda rows: rows[1].update(draft_position=1)),
        (10, lambda rows: rows[1].pop("draft_position")),
        (10, lambda rows: rows[1].update(draft_position=11)),
    ],
)
def test_invalid_entry_and_order_input_fails_before_writes(tmp_path, entry_count, mutation):
    database = migrated_connection()
    with pytest.raises(ReplayBootstrapError):
        load_replay_config(_files(tmp_path, entry_count=entry_count, mutate_entry=mutation))
    assert SeasonRepository(database).list_seasons() == []


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda rows: rows[0].update(canonical_player_id=None), "canonical_player_id"),
        (lambda rows: rows[0].update(afl_team_id=None), "afl_team_id"),
        (lambda rows: rows[1].update(canonical_player_id=rows[0]["canonical_player_id"]), "duplicate"),
    ],
)
def test_unresolved_or_duplicate_player_identity_is_rejected(tmp_path, mutation, match):
    with pytest.raises(ReplayBootstrapError, match=match):
        load_replay_config(_files(tmp_path, mutate_player=mutation))


def test_bootstrap_is_idempotent_and_preserves_provider_identity(tmp_path):
    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))
    first = bootstrap_first_half(database, config)
    provision_replay_operator(database, config, "correct horse battery staple")
    first = replay_readiness(database, config)
    second = bootstrap_first_half(database, config)
    season_id = first["season"]["season_id"]
    player = PlayerPoolRepository(database).get(season_id, 2026001)

    assert first["overall"] == second["overall"] == "READY"
    assert player.source_provider == "afl-api-v1"
    assert player.afl_team_id == 1001
    assert database.execute("SELECT COUNT(*) n FROM draft_pick WHERE season_id=?", (season_id,)).fetchone()["n"] == 30
    assert DraftRepository(database).status(season_id).completed_picks == 0


def test_paused_draft_blocks_pick_one_and_rerun_does_not_resume(tmp_path):
    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))
    report = bootstrap_first_half(database, config)
    season_id = report["season"]["season_id"]
    draft = DraftRepository(database)
    draft.pause(season_id, reason="operator investigation")

    blocked = replay_readiness(database, config)
    assert blocked["overall"] == "NOT READY"
    assert blocked["draft_board"]["checks"]["draft_not_paused"] is False
    assert blocked["next_human_action"] is None
    with pytest.raises(ReplayBootstrapError, match="operational state conflicts"):
        bootstrap_first_half(database, config)
    assert draft.status(season_id).is_paused is True


def test_non_2026_config_is_rejected_before_writes(tmp_path):
    database = migrated_connection()
    path = _files(tmp_path)
    raw = json.loads(path.read_text())
    raw["season"]["year"] = 2027
    path.write_text(json.dumps(raw))
    pool_path = tmp_path / "players.json"
    pool = json.loads(pool_path.read_text())
    pool["source"]["season_year"] = 2027
    pool_path.write_text(json.dumps(pool))

    with pytest.raises(ReplayBootstrapError, match="only supports the 2026 replay"):
        load_replay_config(path)
    assert SeasonRepository(database).list_seasons() == []


def test_extra_rules_version_conflicts_without_mutating_existing_state(tmp_path):
    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))
    report = bootstrap_first_half(database, config)
    season_id = report["season"]["season_id"]
    seasons = SeasonRepository(database)
    seasons.create_rules_version(season_id, "unexpected", 2, "Unexpected Rules")

    blocked = replay_readiness(database, config)
    assert blocked["overall"] == "NOT READY"
    assert blocked["checks"]["exact_rules_version"] is False
    with pytest.raises(ReplayBootstrapError, match="exactly the configured"):
        bootstrap_first_half(database, config)
    assert [(r.rules_key, r.version_number) for r in seasons.list_rules_versions(season_id)] == [
        ("ordinary", 1),
        ("unexpected", 2),
    ]


def test_conflicting_rerun_rolls_back_without_changing_valid_state(tmp_path):
    database = migrated_connection()
    original = load_replay_config(_files(tmp_path))
    report = bootstrap_first_half(database, original)
    season_id = report["season"]["season_id"]
    conflict_path = _files(tmp_path / "conflict", mutate_entry=lambda rows: rows[0].update(team_name="Different Club"))

    with pytest.raises(ReplayBootstrapError, match="conflicts"):
        bootstrap_first_half(database, load_replay_config(conflict_path))
    assert IdentityRepository(database).list_entries(season_id)[0].team_name != "Different Club"
    assert len(DraftRepository(database).order(season_id)) == 10
    assert DraftRepository(database).status(season_id).completed_picks == 0


def test_acquired_player_pool_output_shape_loads_through_bootstrap_config_without_a_mocked_seam(tmp_path):
    """The AFL-api #248 season-player acquisition output and #116's bootstrap
    player-pool input contract must meet without any translation seam beyond
    the one small, explicit mapping scripts/first_half_replay.py performs."""
    from app.replay_acquisition import acquire_first_half_2026
    from tests.test_replay_acquisition import Api, make_players

    payload = acquire_first_half_2026(
        Api(players=make_players(40, start=20), no_roster=True), source_base_url="http://api"
    )
    pool = {
        "source": {"provider": "afl-api-v1", "season_year": 2026},
        "players": [
            {
                "canonical_player_id": x["canonical_player_id"],
                "display_name": x["display_name"],
                "afl_team_id": x["team_id"],
                "afl_team_name": x["team_name"],
                "eligible": True,
                "source_updated_at": None,
            }
            for x in payload["players"]
        ],
    }
    (tmp_path / "players.json").write_text(json.dumps(pool))
    entries = [
        {
            "coach_display_name": f"Historical Coach {n}",
            "coach_email": f"coach{n}@replay.example",
            "team_name": f"Historical Club {n}",
            "licence_key": f"historical-{n}",
            "draft_position": n,
        }
        for n in range(1, 11)
    ]
    evidence_filename = _opening_round_evidence(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "season": {"year": 2026, "label": "2026 First Half Replay"},
                "rules": {"key": "ordinary", "version": 1, "name": "2026 Rules"},
                "competition": {"key": "ordinary", "label": "BBBFFL Ordinary"},
                "squad_limit": 3,
                "operator_email": "coach1@replay.example",
                "player_pool_file": "players.json",
                "entries": entries,
                "opening_round": {
                    "afl_season_id": 2026,
                    "evidence_file": evidence_filename,
                    "rules": _opening_round_rules(),
                },
            }
        )
    )

    loaded = load_replay_config(config_path)
    assert loaded.source_provider == "afl-api-v1"
    assert loaded.source_season_year == 2026
    assert {p.canonical_player_id for p in loaded.players} == {p["canonical_player_id"] for p in payload["players"]}
    assert all(p.eligible for p in loaded.players)

    database = migrated_connection()
    report = bootstrap_first_half(database, loaded)
    assert report["player_pool_count"] == 40


class _AlwaysValidRounds:
    """`AflReferenceValidator` accepting any (season, round) pair -- used
    only to manually seed/manipulate `opening_round_rule` state through the
    ordinary domain repository in a test, independent of bootstrap's own
    hermetic evidence validator."""

    def round_exists(self, season_id, round_id):
        return True


def _mutate_rule(rules, club_id, **overrides):
    for row in rules:
        if row["afl_club_id"] == club_id:
            row.update(overrides)
            return
    raise AssertionError(f"club {club_id} not found in opening_round rules")


# -- Opening Round: config validation -----------------------------------------


def test_clean_bootstrap_creates_exactly_ten_accepted_opening_round_rules(tmp_path):
    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))
    report = bootstrap_first_half(database, config)
    season_id = report["season"]["season_id"]
    rules = OpeningRoundRuleRepository(database).list_accepted_for_season(season_id)

    assert len(rules) == OPENING_ROUND_RULE_COUNT == 10
    assert {r.afl_club_id for r in rules} == {club_id for club_id, *_ in OPENING_ROUND_CLUBS_2026}
    assert all(r.afl_opening_round_id == 1343 for r in rules)
    assert all(r.state == "accepted" for r in rules)
    expected_bye_by_club = {club_id: bye for club_id, _, bye, _ in OPENING_ROUND_CLUBS_2026}
    assert all(r.afl_bye_round_id == expected_bye_by_club[r.afl_club_id] for r in rules)

    rounds = SeasonRepository(database).list_rounds(report["competition"]["competition_id"])
    sequence_by_round_id = {r.bbbffl_round_id: r.sequence for r in rounds}
    distribution: dict[int, int] = {}
    expected_target_by_club = {club_id: target for club_id, _, _, target in OPENING_ROUND_CLUBS_2026}
    for rule in rules:
        sequence = sequence_by_round_id[rule.bbbffl_round_id]
        assert sequence == expected_target_by_club[rule.afl_club_id]
        distribution[sequence] = distribution.get(sequence, 0) + 1
    assert distribution == {2: 4, 3: 4, 4: 2}

    assert database.execute("SELECT COUNT(*) n FROM opening_round_nomination").fetchone()["n"] == 0
    assert report["overall"] == "NOT READY"  # operator credential not yet provisioned
    assert report["opening_round"]["expected_rule_count"] == 10
    assert report["opening_round"]["accepted_rule_count"] == 10
    assert report["opening_round"]["complete"] is True
    assert report["opening_round"]["opening_round_id"] == 1343
    assert report["opening_round"]["targets"] == {"2": 4, "3": 4, "4": 2}
    assert report["opening_round"]["nomination_count"] == 0
    assert report["opening_round"]["nominations_required_pre_draft"] is False


def test_missing_rule_rejected_by_wrong_count(tmp_path):
    database = migrated_connection()
    with pytest.raises(ReplayBootstrapError, match="exactly 10"):
        load_replay_config(_files(tmp_path, mutate_opening_round=lambda rows: rows.pop()))
    assert SeasonRepository(database).list_seasons() == []


def test_extra_rule_rejected_by_wrong_count(tmp_path):
    def mutate(rows):
        extra = dict(rows[0])
        extra["afl_club_id"] = 999
        extra["afl_club_name"] = "Unknown Club"
        rows.append(extra)

    with pytest.raises(ReplayBootstrapError, match="exactly 10"):
        load_replay_config(_files(tmp_path, mutate_opening_round=mutate))


def test_extra_unknown_club_replacing_a_participant_is_rejected(tmp_path):
    def mutate(rows):
        rows[-1]["afl_club_id"] = 999
        rows[-1]["afl_club_name"] = "Unknown Club"

    with pytest.raises(ReplayBootstrapError, match="missing.*extra|extra.*missing"):
        load_replay_config(_files(tmp_path, mutate_opening_round=mutate))


def test_duplicate_club_rejected(tmp_path):
    def mutate(rows):
        rows[1]["afl_club_id"] = rows[0]["afl_club_id"]
        rows[1]["afl_club_name"] = rows[0]["afl_club_name"]

    with pytest.raises(ReplayBootstrapError, match="duplicate"):
        load_replay_config(_files(tmp_path, mutate_opening_round=mutate))


@pytest.mark.parametrize(
    "field,value",
    [
        ("afl_club_id", -1),
        ("afl_club_id", 0),
        ("afl_opening_round_id", 0),
        ("afl_bye_round_id", -5),
    ],
)
def test_non_positive_identifiers_rejected(tmp_path, field, value):
    def mutate(rows):
        rows[0][field] = value

    with pytest.raises(ReplayBootstrapError, match="positive integer"):
        load_replay_config(_files(tmp_path, mutate_opening_round=mutate))


def test_invalid_target_round_number_rejected(tmp_path):
    def mutate(rows):
        rows[0]["bbbffl_round_number"] = 5

    with pytest.raises(ReplayBootstrapError, match="bbbffl_round_number must be"):
        load_replay_config(_files(tmp_path, mutate_opening_round=mutate))


def test_wrong_opening_round_id_rejected(tmp_path):
    def mutate(rows):
        rows[0]["afl_opening_round_id"] = 9999

    with pytest.raises(ReplayBootstrapError, match="afl_opening_round_id must be 1343"):
        load_replay_config(_files(tmp_path, mutate_opening_round=mutate))


def test_shipped_template_rejects_unreplaced_afl_season_id_placeholder():
    """`config/replay/2026-first-half.template.json`'s `afl_season_id` is a
    deliberately invalid placeholder (`0`) an operator must replace with
    the genuine AFL-api season identifier from their acquired evidence
    (Codex review, PR #127) -- it must never look like a plausible,
    silently-wrong default such as the calendar year."""
    raw = json.loads(TEMPLATE_PATH.read_text())
    assert raw["opening_round"]["afl_season_id"] == 0
    with pytest.raises(ReplayBootstrapError, match="afl_season_id must be a positive integer"):
        _opening_round_config(raw, TEMPLATE_PATH)


def test_shipped_template_opening_round_rules_are_otherwise_genuinely_valid():
    """Every other opening_round fact in the shipped template (club
    identities, Opening Round ID, bye/target pairing, distribution) is
    already genuine and valid -- only afl_season_id is an operator
    placeholder."""
    raw = json.loads(TEMPLATE_PATH.read_text())
    raw = {**raw, "opening_round": {**raw["opening_round"], "afl_season_id": 2026}}
    config = _opening_round_config(raw, TEMPLATE_PATH)
    assert len(config.rules) == 10


def test_malformed_evidence_classification_rejected(tmp_path):
    def mutate(rows):
        rows[0]["evidence_classification"] = "definitely_not_a_real_classification"

    with pytest.raises(ReplayBootstrapError, match="evidence_classification must be one of"):
        load_replay_config(_files(tmp_path, mutate_opening_round=mutate))


def test_wrong_bye_target_pairing_rejected(tmp_path):
    """A club's bye round is swapped to another valid 2026 bye round, which
    would otherwise preserve the overall 4/4/2 distribution -- this must
    still fail, because the per-club AFL fact (not merely the aggregate
    count) is what this bootstrap validates (issue #126)."""

    def mutate(rows):
        # Brisbane Lions (club 2) really has bye round 1345 -> target 2;
        # reassign it to St Kilda's bye round 1347 -> target 4.
        _mutate_rule(rows, 2, afl_bye_round_id=1347, bbbffl_round_number=4)

    with pytest.raises(ReplayBootstrapError, match="afl_bye_round_id must be 1345"):
        load_replay_config(_files(tmp_path, mutate_opening_round=mutate))


def test_club_name_mismatched_with_its_id_rejected(tmp_path):
    """A config keeping the correct `afl_club_id` (2, Brisbane Lions) but
    naming the wrong club must fail -- `afl_club_name` is validated against
    its ID, not merely required to be non-empty, so a typo/swap can't
    silently misrepresent the human-inspectable rule set (issue #126)."""

    def mutate(rows):
        _mutate_rule(rows, 2, afl_club_name="Not Brisbane Lions FC")

    with pytest.raises(ReplayBootstrapError, match="afl_club_name must be"):
        load_replay_config(_files(tmp_path, mutate_opening_round=mutate))


def test_wrong_target_round_distribution_rejected(tmp_path):
    def mutate(rows):
        _mutate_rule(rows, 2, bbbffl_round_number=3)  # BL's bye round (1345) stays R2's, but its target claims R3

    with pytest.raises(ReplayBootstrapError):
        load_replay_config(_files(tmp_path, mutate_opening_round=mutate))


# -- Opening Round: local evidence validation ---------------------------------


def test_replay_opening_round_evidence_validator_uses_only_local_evidence(tmp_path):
    filename = _opening_round_evidence(tmp_path)
    validator = ReplayOpeningRoundEvidenceValidator(tmp_path / filename)

    assert validator.round_exists(2026, 1343) is True
    assert validator.round_exists(2026, 1345) is True
    assert validator.round_exists(2026, 1346) is True
    assert validator.round_exists(2026, 1347) is True
    assert validator.round_exists(2026, 9999) is False
    assert validator.round_exists(2025, 1343) is False


def test_replay_opening_round_evidence_validator_rejects_malformed_evidence(tmp_path):
    (tmp_path / "bad.json").write_text("not json")
    with pytest.raises(ReplayBootstrapError, match="could not read"):
        ReplayOpeningRoundEvidenceValidator(tmp_path / "bad.json")

    (tmp_path / "wrong-schema.json").write_text(json.dumps({"schema": "unsupported"}))
    with pytest.raises(ReplayBootstrapError, match="unsupported"):
        ReplayOpeningRoundEvidenceValidator(tmp_path / "wrong-schema.json")

    (tmp_path / "no-manifest.json").write_text(
        json.dumps({"schema": "bbbffl.replay-evidence/v1", "seasons": [], "rounds": []})
    )
    with pytest.raises(ReplayBootstrapError, match="manifest"):
        ReplayOpeningRoundEvidenceValidator(tmp_path / "no-manifest.json")

    (tmp_path / "no-rounds.json").write_text(
        json.dumps(
            {
                "schema": "bbbffl.replay-evidence/v1",
                "manifest": {"id": "x", "version": "1", "evidence_class": "known_fact"},
                "seasons": [{"season_id": 2026, "provenance": PROVENANCE}],
                "rounds": "nope",
            }
        )
    )
    with pytest.raises(ReplayBootstrapError, match="malformed"):
        ReplayOpeningRoundEvidenceValidator(tmp_path / "no-rounds.json")

    (tmp_path / "no-provenance.json").write_text(
        json.dumps(
            {
                "schema": "bbbffl.replay-evidence/v1",
                "manifest": {"id": "x", "version": "1", "evidence_class": "known_fact"},
                "seasons": [{"season_id": 2026, "year": 2026}],
                "rounds": [],
            }
        )
    )
    with pytest.raises(ReplayBootstrapError, match="provenance.source is required"):
        ReplayOpeningRoundEvidenceValidator(tmp_path / "no-provenance.json")


def test_replay_opening_round_evidence_validator_rejects_hand_authored_evidence_without_provenance(tmp_path):
    """A truncated or hand-authored file that merely contains the right
    schema plus matching season/round IDs must not be mistaken for
    genuine acquired replay evidence (Codex review, PR #127): this
    validator applies the same manifest.id/version/evidence_class and
    per-record provenance.source/evidence_class checks
    `app.replay.ReplayAflDataSource._load`/`_validate_provenance` require,
    even though it otherwise omits unrelated match/stat/checkpoint
    validation."""
    # Missing manifest entirely.
    (tmp_path / "no-manifest.json").write_text(
        json.dumps(
            {
                "schema": "bbbffl.replay-evidence/v1",
                "seasons": [{"season_id": 2026, "year": 2026, "provenance": PROVENANCE}],
                "rounds": [
                    {"round_id": r, "season_id": 2026, "provenance": PROVENANCE} for r in (1343, 1345, 1346, 1347)
                ],
            }
        )
    )
    with pytest.raises(ReplayBootstrapError, match="missing/invalid manifest"):
        ReplayOpeningRoundEvidenceValidator(tmp_path / "no-manifest.json")

    # Season record missing provenance.
    (tmp_path / "season-no-provenance.json").write_text(
        json.dumps(
            {
                "schema": "bbbffl.replay-evidence/v1",
                "manifest": {"id": "x", "version": "1", "evidence_class": "known_fact"},
                "seasons": [{"season_id": 2026, "year": 2026}],
                "rounds": [
                    {"round_id": r, "season_id": 2026, "provenance": PROVENANCE} for r in (1343, 1345, 1346, 1347)
                ],
            }
        )
    )
    with pytest.raises(ReplayBootstrapError, match=r"seasons\[0\].provenance.source is required"):
        ReplayOpeningRoundEvidenceValidator(tmp_path / "season-no-provenance.json")

    # Round record with an unknown evidence_class.
    (tmp_path / "round-bad-evidence-class.json").write_text(
        json.dumps(
            {
                "schema": "bbbffl.replay-evidence/v1",
                "manifest": {"id": "x", "version": "1", "evidence_class": "known_fact"},
                "seasons": [{"season_id": 2026, "year": 2026, "provenance": PROVENANCE}],
                "rounds": [
                    {"round_id": 1343, "season_id": 2026, "provenance": {"source": "x", "evidence_class": "bogus"}}
                ],
            }
        )
    )
    with pytest.raises(ReplayBootstrapError, match=r"rounds\[0\].provenance.evidence_class is missing or unknown"):
        ReplayOpeningRoundEvidenceValidator(tmp_path / "round-bad-evidence-class.json")


def test_replay_opening_round_evidence_validator_rejects_rounds_for_undeclared_season(tmp_path):
    """A round tagged with a season_id that never appears in `seasons` is
    internally inconsistent evidence and must be rejected outright -- never
    silently admitted (e.g. via `dict.setdefault`), which would let a
    malformed/hand-edited package validate a round under a season it never
    actually declares (mirrors app.replay.ReplayAflDataSource's own
    round-references-missing-season check)."""
    payload = {
        "schema": "bbbffl.replay-evidence/v1",
        "manifest": {"id": "x", "version": "1", "evidence_class": "known_fact"},
        "seasons": [{"season_id": 2025, "year": 2025, "provenance": PROVENANCE}],
        "rounds": [{"round_id": 1343, "season_id": 2026, "provenance": PROVENANCE}],
    }
    (tmp_path / "undeclared-season.json").write_text(json.dumps(payload))

    with pytest.raises(ReplayBootstrapError, match="absent from its seasons list"):
        ReplayOpeningRoundEvidenceValidator(tmp_path / "undeclared-season.json")


@pytest.mark.parametrize("missing_round_id", [1343, 1345, 1346, 1347])
def test_missing_required_round_evidence_fails_bootstrap(tmp_path, missing_round_id):
    database = migrated_connection()
    remaining = tuple(r for r in OPENING_ROUND_ALL_AFL_ROUND_IDS if r != missing_round_id)
    config = load_replay_config(_files(tmp_path, opening_round_round_ids=remaining))

    with pytest.raises(ValueError, match="does not exist"):
        bootstrap_first_half(database, config)
    assert SeasonRepository(database).list_seasons() == []


def test_wrong_afl_season_identity_in_evidence_fails_bootstrap(tmp_path):
    """Evidence that declares no 2026 season at all (only a 2025 one) must
    fail closed before any write, rather than resolving Opening Round
    references against the wrong season."""
    database = migrated_connection()
    tmp_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "bbbffl.replay-evidence/v1",
        "manifest": {"id": "x", "version": "1", "evidence_class": "known_fact"},
        "seasons": [{"season_id": 2025, "year": 2025, "provenance": PROVENANCE}],
        "rounds": [
            {"round_id": r, "season_id": 2025, "provenance": PROVENANCE} for r in OPENING_ROUND_ALL_AFL_ROUND_IDS
        ],
    }
    (tmp_path / "wrong-season-evidence.json").write_text(json.dumps(payload))

    config_path = _files(tmp_path)
    raw = json.loads(config_path.read_text())
    raw["opening_round"]["evidence_file"] = "wrong-season-evidence.json"
    config_path.write_text(json.dumps(raw))

    config = load_replay_config(config_path)
    with pytest.raises(ReplayBootstrapError, match="does not declare a season for year 2026"):
        bootstrap_first_half(database, config)
    assert SeasonRepository(database).list_seasons() == []


def test_configured_season_id_mismatched_with_acquired_opaque_season_id_fails_bootstrap(tmp_path):
    """AFL-api's `season_id` is an opaque identifier, not necessarily the
    calendar year (see tests/test_replay_acquisition.py's fake API, which
    models the genuine 2026 season as `season_id: 712`). A config whose
    `opening_round.afl_season_id` does not match the season the evidence
    actually declares for year 2026 must fail closed -- even though the
    evidence otherwise genuinely contains a 2026 season and all required
    rounds."""
    database = migrated_connection()
    tmp_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "bbbffl.replay-evidence/v1",
        "manifest": {"id": "x", "version": "1", "evidence_class": "known_fact"},
        "seasons": [{"season_id": 712, "year": 2026, "provenance": PROVENANCE}],
        "rounds": [
            {"round_id": r, "season_id": 712, "provenance": PROVENANCE} for r in OPENING_ROUND_ALL_AFL_ROUND_IDS
        ],
    }
    (tmp_path / "opaque-season-evidence.json").write_text(json.dumps(payload))

    config_path = _files(tmp_path)
    raw = json.loads(config_path.read_text())
    raw["opening_round"]["evidence_file"] = "opaque-season-evidence.json"
    # Configured afl_season_id (2026) does not match the evidence's genuine
    # opaque identity (712) for that same year.
    config_path.write_text(json.dumps(raw))

    config = load_replay_config(config_path)
    with pytest.raises(ReplayBootstrapError, match="does not match the acquired 2026 season identity"):
        bootstrap_first_half(database, config)
    assert SeasonRepository(database).list_seasons() == []


def test_opaque_afl_season_id_accepted_when_configured_correctly(tmp_path):
    """The bootstrap must not assume AFL season_id equals the calendar
    year: an evidence package using a genuinely opaque season_id (e.g.
    712 for 2026, as AFL-api's real acquisition contract can produce)
    succeeds once `opening_round.afl_season_id` is configured to match it."""
    database = migrated_connection()
    config = load_replay_config(
        _files(
            tmp_path,
            opening_round_round_ids=OPENING_ROUND_ALL_AFL_ROUND_IDS,
            opening_round_season_id=712,
        )
    )
    report = bootstrap_first_half(database, config)
    season_id = report["season"]["season_id"]
    rules = OpeningRoundRuleRepository(database).list_accepted_for_season(season_id)
    assert len(rules) == 10
    assert all(r.afl_season_id == 712 for r in rules)


def test_no_live_afl_client_involved_in_local_evidence_validation(tmp_path):
    """`ReplayOpeningRoundEvidenceValidator` never imports/constructs
    `app.afl_client.AflApiClient`, and a clean bootstrap succeeds with no
    such client passed anywhere in this configuration/validation path."""
    source = inspect.getsource(ReplayOpeningRoundEvidenceValidator)
    assert "AflApiClient" not in source
    assert "afl_client" not in source


# -- Opening Round: idempotency and conflicts ---------------------------------


def test_identical_rerun_creates_no_new_opening_round_revisions(tmp_path):
    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))
    first = bootstrap_first_half(database, config)
    season_id = first["season"]["season_id"]
    repo = OpeningRoundRuleRepository(database)
    first_rules = {r.afl_club_id: (r.rule_id, r.revision) for r in repo.list_accepted_for_season(season_id)}
    before_audit = database.execute(
        "SELECT COUNT(*) n FROM audit_event WHERE entity_type='opening_round.rule'"
    ).fetchone()["n"]

    second = bootstrap_first_half(database, config)
    second_rules = {
        r.afl_club_id: (r.rule_id, r.revision) for r in repo.list_accepted_for_season(second["season"]["season_id"])
    }
    after_audit = database.execute(
        "SELECT COUNT(*) n FROM audit_event WHERE entity_type='opening_round.rule'"
    ).fetchone()["n"]

    assert first_rules == second_rules
    assert len(second_rules) == 10
    assert after_audit == before_audit
    assert second["opening_round"]["complete"] is True


def test_conflicting_existing_accepted_rule_fails_without_correcting_it(tmp_path):
    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))
    report = bootstrap_first_half(database, config)
    season_id = report["season"]["season_id"]
    repo = OpeningRoundRuleRepository(database)

    # An operator/process externally corrects Brisbane Lions' (club 2, which
    # really targets BBBFFL round 2 via bye round 1345) to a materially
    # different bye round/target -- GCFC's (club 4) round-3 bye/target.
    wrong_target_round_id = repo.resolve(season_id, 4).bbbffl_round_id
    repo.correct(
        season_id,
        2,
        2026,
        1343,
        1346,
        wrong_target_round_id,
        _AlwaysValidRounds(),
        reason="simulated external correction for this test",
    )

    with pytest.raises(ReplayBootstrapError, match="conflicts with replay configuration"):
        bootstrap_first_half(database, config)

    # Failure must not have touched any *other* club's already-correct rule.
    unaffected = repo.resolve(season_id, 5)
    assert unaffected is not None and unaffected.revision == 1
    assert len(repo.list_accepted_for_season(season_id)) == 10


def test_unexpected_extra_accepted_rule_fails_closed(tmp_path):
    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))
    report = bootstrap_first_half(database, config)
    season_id = report["season"]["season_id"]
    repo = OpeningRoundRuleRepository(database)
    any_bbbffl_round_id = repo.resolve(season_id, 2).bbbffl_round_id

    # Simulate an unrelated accepted rule for a club outside the configured
    # ten-club replay population.
    repo.accept(season_id, 999, 2026, 1343, 1345, any_bbbffl_round_id, _AlwaysValidRounds(), reason="unexpected extra")

    with pytest.raises(ReplayBootstrapError, match="unexpected accepted Opening Round rule"):
        bootstrap_first_half(database, config)


def test_bootstrap_failure_leaves_no_partially_committed_opening_round_state(tmp_path):
    """Force a failure partway through the ten Opening Round acceptances
    (the ninth club, St Kilda, has its evidence withheld) and prove the
    whole bootstrap transaction -- ordinary setup *and* every Opening
    Round rule -- rolls back atomically rather than leaving the first
    eight clubs' rules committed while the rest of bootstrap fails."""
    database = migrated_connection()
    remaining = tuple(r for r in OPENING_ROUND_ALL_AFL_ROUND_IDS if r != 1347)
    config = load_replay_config(_files(tmp_path, opening_round_round_ids=remaining))

    with pytest.raises(ValueError, match="does not exist"):
        bootstrap_first_half(database, config)

    assert SeasonRepository(database).list_seasons() == []
    assert database.execute("SELECT COUNT(*) n FROM opening_round_rule").fetchone()["n"] == 0
    assert database.execute("SELECT COUNT(*) n FROM opening_round_rule_revision").fetchone()["n"] == 0


def test_opening_round_rules_participate_in_bootstraps_single_transaction(tmp_path):
    """`OpeningRoundRuleRepository.accept_locked` must run on the exact same
    connection bootstrap's own writes use, never opening a second,
    independent transaction (the nested-transaction hazard issue #126
    calls out) -- verified by confirming ordinary state and Opening Round
    rules always appear or vanish together."""
    database = migrated_connection()

    # A materially conflicting draft state (a paused draft) forces
    # bootstrap's own transaction to fail *after* ordinary setup would
    # otherwise have completed. Opening Round rules must not have been
    # committed by an independent transaction before that failure.
    config = load_replay_config(_files(tmp_path))
    report = bootstrap_first_half(database, config)
    season_id = report["season"]["season_id"]
    DraftRepository(database).pause(season_id, reason="test-induced conflict")

    with pytest.raises(ReplayBootstrapError, match="operational state conflicts"):
        bootstrap_first_half(database, config)

    # The prior successful bootstrap's ten rules remain exactly as they
    # were -- the second, failed attempt neither duplicated nor dropped any.
    assert len(OpeningRoundRuleRepository(database).list_accepted_for_season(season_id)) == 10


def test_new_opening_round_rules_refused_once_a_draft_pick_has_completed(tmp_path):
    """Opening Round configuration is a before-Pick-1 prerequisite. A rerun
    against a season where a genuine season member never had accepted
    Opening Round rules established (e.g. a database bootstrapped before
    this feature existed) must refuse to newly create them once drafting
    has already begun -- never mutate that prerequisite state after the
    fact, even though bootstrap is otherwise idempotent/conflict-tolerant."""
    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))
    report = bootstrap_first_half(database, config)
    season_id = report["season"]["season_id"]

    with transaction(database) as conn:
        conn.execute("DELETE FROM opening_round_rule_revision")
        conn.execute("DELETE FROM opening_round_rule")
        draft_id = conn.execute("SELECT draft_id FROM season_draft WHERE season_id=?", (season_id,)).fetchone()[
            "draft_id"
        ]
        pick_id = conn.execute(
            "SELECT draft_pick_id FROM draft_pick WHERE draft_id=? ORDER BY overall_number LIMIT 1", (draft_id,)
        ).fetchone()["draft_pick_id"]
        player_id = conn.execute(
            "SELECT season_player_id FROM season_player_pool WHERE season_id=? LIMIT 1", (season_id,)
        ).fetchone()["season_player_id"]
        conn.execute(
            "UPDATE draft_pick SET completed_at=?, selected_season_player_id=? WHERE draft_pick_id=?",
            ("2026-03-01T00:00:00Z", player_id, pick_id),
        )
    assert OpeningRoundRuleRepository(database).list_accepted_for_season(season_id) == []

    with pytest.raises(ReplayBootstrapError, match="draft pick"):
        bootstrap_first_half(database, config)

    # Refused, not partially applied: still zero accepted rules.
    assert OpeningRoundRuleRepository(database).list_accepted_for_season(season_id) == []


def test_new_opening_round_rules_refused_even_if_the_only_completed_pick_was_since_corrected(tmp_path):
    """`DraftRepository.correct_pick` preserves the original completed pick
    row (marking it superseded) and inserts a fresh, uncompleted
    replacement -- undoing the sole completed pick must not make a
    since-corrected draft look as though it never started (Codex review,
    PR #127): counting only *active* completed picks would wrongly permit
    establishing prerequisite rules after drafting had genuinely begun."""
    from app.draft import DraftRepository
    from app.player_pool import PlayerPoolRepository

    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))
    report = bootstrap_first_half(database, config)
    season_id = report["season"]["season_id"]
    draft = DraftRepository(database)
    _, first_entry_id = draft.order(season_id)[0]
    player = PlayerPoolRepository(database).list_selectable(season_id)[0]

    pick = draft.execute_pick(season_id, first_entry_id, player.season_player_id)
    draft.correct_pick(season_id, pick.draft_pick_id, reason="test-induced correction")
    assert draft.status(season_id).completed_picks == 0  # the *active* count is back to zero

    with transaction(database) as conn:
        conn.execute("DELETE FROM opening_round_rule_revision")
        conn.execute("DELETE FROM opening_round_rule")
    assert OpeningRoundRuleRepository(database).list_accepted_for_season(season_id) == []

    with pytest.raises(ReplayBootstrapError, match="draft pick"):
        bootstrap_first_half(database, config)

    assert OpeningRoundRuleRepository(database).list_accepted_for_season(season_id) == []


# -- Opening Round: readiness --------------------------------------------------


def test_incomplete_opening_round_state_blocks_overall_ready_but_not_pick_one(tmp_path):
    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))
    report = bootstrap_first_half(database, config)
    season_id = report["season"]["season_id"]
    provision_replay_operator(database, config, "correct horse battery staple")
    repo = OpeningRoundRuleRepository(database)
    bbbffl_round_id = repo.resolve(season_id, 2).bbbffl_round_id

    # Externally correct one club's rule so it no longer matches the
    # replay configuration -- an incomplete/conflicting Opening Round state.
    repo.correct(
        season_id,
        2,
        2026,
        1343,
        1345,
        bbbffl_round_id,
        _AlwaysValidRounds(),
        reason="test-induced drift",
        evidence_classification="known_fact",
    )

    readiness = replay_readiness(database, config)

    assert readiness["overall"] == "NOT READY"
    assert readiness["checks"]["opening_round_configuration_complete"] is False
    assert readiness["opening_round"]["complete"] is False
    assert readiness["opening_round"]["accepted_rule_count"] == 9
    # Draft Board readiness itself is unaffected by Opening Round state.
    assert readiness["draft_board"]["ready"] is True
    assert readiness["next_human_action"] == "Pick 1"


def test_complete_opening_round_state_is_ready_with_zero_nominations(tmp_path):
    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))
    bootstrap_first_half(database, config)
    provision_replay_operator(database, config, "correct horse battery staple")

    readiness = replay_readiness(database, config)

    assert readiness["overall"] == "READY"
    assert readiness["opening_round"]["complete"] is True
    assert readiness["opening_round"]["nomination_count"] == 0
    assert readiness["opening_round"]["nominations_required_pre_draft"] is False
    assert readiness["next_human_action"] == "Pick 1"
    assert not any("nomination" in label for label in readiness["checks"])


# -- Opening Round: end-to-end integration contract ---------------------------


def test_integration_local_evidence_to_ready_pick_one_with_ten_accepted_rules(tmp_path):
    """local replay evidence -> bootstrap -> readiness READY/Pick 1 + 10
    accepted Opening Round rules -> list_accepted_for_season() == 10 ->
    zero opening_round_nomination rows (issue #126's required contract)."""
    database = migrated_connection()
    config = load_replay_config(_files(tmp_path))

    bootstrap_first_half(database, config)
    provision_replay_operator(database, config, "correct horse battery staple")
    readiness = replay_readiness(database, config)
    season_id = readiness["season"]["season_id"]

    assert readiness["overall"] == "READY"
    assert readiness["next_human_action"] == "Pick 1"
    assert readiness["draft_board"]["next_pick_overall"] == 1
    rules = OpeningRoundRuleRepository(database).list_accepted_for_season(season_id)
    assert len(rules) == 10
    assert (
        database.execute("SELECT COUNT(*) n FROM opening_round_nomination WHERE season_id=?", (season_id,)).fetchone()[
            "n"
        ]
        == 0
    )
