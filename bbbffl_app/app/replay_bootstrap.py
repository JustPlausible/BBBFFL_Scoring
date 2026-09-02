"""Transactional first-half replay bootstrap and reusable readiness report.

The JSON files consumed here are operator input, not application defaults.  All
validation happens before the transaction starts; reconciliation inside the
transaction is deliberately conservative and rejects any existing state which
does not exactly describe the requested replay.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.audit import ActorContext, append_event
from app.auth import CredentialRepository, RoleGrantRepository
from app.db import transaction
from app.draft import DraftRepository
from app.draft_board import draft_board_readiness
from app.identity import IdentityRepository
from app.opening_round import EVIDENCE_CLASSIFICATIONS, OpeningRoundRule, OpeningRoundRuleRepository
from app.player_pool import PlayerPoolRepository
from app.season import BBBFFLRound, SeasonRepository

TEAM_COUNT = 10
FIRST_HALF_ROUNDS = tuple(range(1, 10))
REPLAY_YEAR = 2026

# -- 2026 Opening Round rule facts -------------------------------------------
#
# Transcribed from docs/opening-round-deferred-selection.md's 2026 evidence
# row / tests/opening_round_evidence.py's EVIDENCE_2026 -- genuine AFL-side
# facts (Opening Round participation and compensating-bye placement), never
# invented. This bootstrap command is explicitly restricted to the 2026
# first-half replay (see REPLAY_YEAR above), so it is acceptable for *this*
# validator -- and only this validator -- to know the intended 2026 shape;
# app.opening_round itself never infers activation from a season year (see
# that module's docstring).
OPENING_ROUND_RULE_COUNT = 10
EXPECTED_OPENING_ROUND_AFL_SEASON_ID = REPLAY_YEAR
EXPECTED_OPENING_ROUND_ID = 1343
# afl_club_id -> the AFL round ID carrying that club's compensating bye.
EXPECTED_BYE_ROUND_BY_CLUB_ID = {
    2: 1345,  # BL
    5: 1345,  # CARL
    3: 1345,  # COLL
    10: 1345,  # GEEL
    4: 1346,  # GCFC
    8: 1346,  # WB
    9: 1346,  # HAW
    13: 1346,  # SYD
    11: 1347,  # STK
    15: 1347,  # GWS
}
# afl_club_id -> the one canonical name this validator accepts, so a
# config typo/swap that keeps a valid club ID but names the wrong club is
# still caught, rather than `afl_club_name` being a purely decorative,
# unvalidated inspectability field.
EXPECTED_CLUB_NAME_BY_ID = {
    2: "Brisbane Lions",
    5: "Carlton",
    3: "Collingwood",
    10: "Geelong Cats",
    4: "Gold Coast Suns",
    8: "Western Bulldogs",
    9: "Hawthorn",
    13: "Sydney Swans",
    11: "St Kilda",
    15: "GWS Giants",
}
# compensating-bye AFL round ID -> the BBBFFL logical round number that
# operationalises it for this replay (explicit replay/reconstructed
# behaviour -- see this module's `_opening_round_config` and issue #126).
EXPECTED_TARGET_ROUND_BY_BYE_ROUND_ID = {1345: 2, 1346: 3, 1347: 4}
EXPECTED_TARGET_ROUND_DISTRIBUTION = {2: 4, 3: 4, 4: 2}


class ReplayBootstrapError(ValueError):
    """Configuration or persisted state makes a safe bootstrap impossible."""


@dataclass(frozen=True)
class ReplayPlayer:
    canonical_player_id: int
    display_name: str
    afl_team_id: int
    afl_team_name: str
    eligible: bool = True
    source_updated_at: str | None = None


@dataclass(frozen=True)
class ReplayEntry:
    coach_display_name: str
    coach_email: str
    team_name: str
    licence_key: str
    draft_position: int


@dataclass(frozen=True)
class OpeningRoundRuleConfig:
    """One inspectable, human-facing configured 2026 Opening Round rule.

    `bbbffl_round_number` is the stable logical round number (matching
    `bbbffl_round.sequence`); the bootstrap resolves it against the actual
    persisted `bbbffl_round_id` once the ordinary rounds are known -- the
    operator never supplies a generated round UUID (see issue #126)."""

    afl_club_id: int
    afl_club_name: str
    afl_opening_round_id: int
    afl_bye_round_id: int
    bbbffl_round_number: int
    evidence_classification: str


@dataclass(frozen=True)
class OpeningRoundReplayConfig:
    """The complete, validated 2026 Opening Round rule set plus the local
    replay evidence used to validate acceptance (see
    `ReplayOpeningRoundEvidenceValidator`) -- never a live AFL-api client."""

    afl_season_id: int
    evidence_file: Path
    rules: tuple[OpeningRoundRuleConfig, ...]


@dataclass(frozen=True)
class ReplayConfig:
    year: int
    season_label: str
    rules_key: str
    rules_version: int
    rules_name: str
    competition_key: str
    competition_label: str
    squad_limit: int
    operator_email: str
    source_provider: str
    source_season_year: int
    players: tuple[ReplayPlayer, ...]
    entries: tuple[ReplayEntry, ...]
    opening_round: OpeningRoundReplayConfig


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReplayBootstrapError(f"{field} must be a non-empty string")
    return value.strip()


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ReplayBootstrapError(f"{field} must be a positive integer")
    return value


def _opening_round_config(raw: dict, config_path: Path) -> OpeningRoundReplayConfig:
    """Parse and fully validate the `opening_round` replay config section
    (issue #126) before any bootstrap write is attempted. Every 2026-shape
    fact validated here (club identity, Opening Round ID, bye/target
    pairing, R2/R3/R4 distribution) is transcribed from
    docs/opening-round-deferred-selection.md's 2026 evidence row -- see the
    module-level constants above."""
    try:
        section = raw["opening_round"]
        if not isinstance(section, dict):
            raise TypeError("opening_round must be an object")
        afl_season_id = section["afl_season_id"]
        evidence_file_value = _text(section.get("evidence_file"), "opening_round.evidence_file")
        rule_rows = section["rules"]
        if not isinstance(rule_rows, list):
            raise TypeError("opening_round.rules must be a list")
    except (KeyError, TypeError) as exc:
        raise ReplayBootstrapError(f"invalid or missing opening_round configuration: {exc}") from exc
    if afl_season_id != EXPECTED_OPENING_ROUND_AFL_SEASON_ID:
        raise ReplayBootstrapError(
            f"opening_round.afl_season_id must be {EXPECTED_OPENING_ROUND_AFL_SEASON_ID} for this 2026 replay"
        )
    evidence_path = Path(evidence_file_value)
    if not evidence_path.is_absolute():
        evidence_path = config_path.parent / evidence_path

    rules = []
    for index, row in enumerate(rule_rows):
        if not isinstance(row, dict):
            raise ReplayBootstrapError(f"opening_round.rules[{index}] must be an object")
        afl_club_id = _positive_int(row.get("afl_club_id"), f"opening_round.rules[{index}].afl_club_id")
        afl_club_name = _text(row.get("afl_club_name"), f"opening_round.rules[{index}].afl_club_name")
        afl_opening_round_id = _positive_int(
            row.get("afl_opening_round_id"), f"opening_round.rules[{index}].afl_opening_round_id"
        )
        afl_bye_round_id = _positive_int(row.get("afl_bye_round_id"), f"opening_round.rules[{index}].afl_bye_round_id")
        bbbffl_round_number = row.get("bbbffl_round_number")
        if not isinstance(bbbffl_round_number, int) or isinstance(bbbffl_round_number, bool):
            raise ReplayBootstrapError(f"opening_round.rules[{index}].bbbffl_round_number must be an integer")
        evidence_classification = row.get("evidence_classification")
        if evidence_classification not in EVIDENCE_CLASSIFICATIONS:
            raise ReplayBootstrapError(
                f"opening_round.rules[{index}].evidence_classification must be one of "
                f"{sorted(EVIDENCE_CLASSIFICATIONS)}"
            )
        rules.append(
            OpeningRoundRuleConfig(
                afl_club_id,
                afl_club_name,
                afl_opening_round_id,
                afl_bye_round_id,
                bbbffl_round_number,
                evidence_classification,
            )
        )

    if len(rules) != OPENING_ROUND_RULE_COUNT:
        raise ReplayBootstrapError(
            f"opening_round.rules must contain exactly {OPENING_ROUND_RULE_COUNT} rules (received {len(rules)})"
        )
    club_ids = [rule.afl_club_id for rule in rules]
    if len(club_ids) != len(set(club_ids)):
        raise ReplayBootstrapError("opening_round.rules contains a duplicate afl_club_id")
    expected_club_ids = set(EXPECTED_BYE_ROUND_BY_CLUB_ID)
    if set(club_ids) != expected_club_ids:
        missing = sorted(expected_club_ids - set(club_ids))
        extra = sorted(set(club_ids) - expected_club_ids)
        raise ReplayBootstrapError(
            "opening_round.rules must cover exactly the 2026 Opening Round participating clubs; "
            f"missing={missing} extra={extra}"
        )
    for rule in rules:
        expected_name = EXPECTED_CLUB_NAME_BY_ID[rule.afl_club_id]
        if rule.afl_club_name != expected_name:
            raise ReplayBootstrapError(
                f"opening_round.rules afl_club_id={rule.afl_club_id} afl_club_name must be {expected_name!r}"
            )
        if rule.afl_opening_round_id != EXPECTED_OPENING_ROUND_ID:
            raise ReplayBootstrapError(
                f"opening_round.rules afl_club_id={rule.afl_club_id} afl_opening_round_id must be "
                f"{EXPECTED_OPENING_ROUND_ID}"
            )
        expected_bye = EXPECTED_BYE_ROUND_BY_CLUB_ID[rule.afl_club_id]
        if rule.afl_bye_round_id != expected_bye:
            raise ReplayBootstrapError(
                f"opening_round.rules afl_club_id={rule.afl_club_id} afl_bye_round_id must be {expected_bye}"
            )
        expected_target = EXPECTED_TARGET_ROUND_BY_BYE_ROUND_ID[expected_bye]
        if rule.bbbffl_round_number != expected_target:
            raise ReplayBootstrapError(
                f"opening_round.rules afl_club_id={rule.afl_club_id} bbbffl_round_number must be {expected_target}"
            )
    distribution: dict[int, int] = {}
    for rule in rules:
        distribution[rule.bbbffl_round_number] = distribution.get(rule.bbbffl_round_number, 0) + 1
    if distribution != EXPECTED_TARGET_ROUND_DISTRIBUTION:
        raise ReplayBootstrapError(
            f"opening_round.rules target-round distribution must be {EXPECTED_TARGET_ROUND_DISTRIBUTION}, "
            f"got {distribution}"
        )
    return OpeningRoundReplayConfig(afl_season_id, evidence_path, tuple(rules))


def load_replay_config(path: str | Path) -> ReplayConfig:
    """Load and fully validate replay facts and the captured afl-api pool."""
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayBootstrapError(f"could not read replay config {config_path}: {exc}") from exc
    try:
        pool_path = Path(_text(raw["player_pool_file"], "player_pool_file"))
        if not pool_path.is_absolute():
            pool_path = config_path.parent / pool_path
        pool_raw = json.loads(pool_path.read_text())
        season = raw["season"]
        rules = raw["rules"]
        competition = raw["competition"]
        source = pool_raw["source"]
        entry_rows = raw["entries"]
        player_rows = pool_raw["players"]
    except (KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        raise ReplayBootstrapError(f"invalid replay configuration or player-pool capture: {exc}") from exc

    entries = tuple(
        ReplayEntry(
            _text(row.get("coach_display_name"), f"entries[{index}].coach_display_name"),
            _text(row.get("coach_email"), f"entries[{index}].coach_email").casefold(),
            _text(row.get("team_name"), f"entries[{index}].team_name"),
            _text(row.get("licence_key"), f"entries[{index}].licence_key"),
            row.get("draft_position"),
        )
        for index, row in enumerate(entry_rows)
    )
    if len(entries) != TEAM_COUNT:
        raise ReplayBootstrapError(f"exactly {TEAM_COUNT} entries are required (received {len(entries)})")
    if any(
        "REPLACE_" in value or value.endswith(".invalid")
        for entry in entries
        for value in (entry.coach_display_name, entry.coach_email, entry.team_name, entry.licence_key)
    ):
        raise ReplayBootstrapError("template placeholders must be replaced with genuine replay identities")
    for attribute, label in (
        ("coach_email", "coach emails"),
        ("coach_display_name", "coach names"),
        ("team_name", "team names"),
        ("licence_key", "licence keys"),
    ):
        values = [getattr(entry, attribute).casefold() for entry in entries]
        if len(values) != len(set(values)):
            raise ReplayBootstrapError(f"{label} must be unique")
    positions = [entry.draft_position for entry in entries]
    if any(not isinstance(position, int) for position in positions):
        raise ReplayBootstrapError("every entry requires an integer draft_position")
    if sorted(positions) != list(range(1, TEAM_COUNT + 1)):
        raise ReplayBootstrapError("draft positions must contain each position 1 through 10 exactly once")

    players = tuple(
        ReplayPlayer(
            row.get("canonical_player_id"),
            _text(row.get("display_name"), f"players[{index}].display_name"),
            row.get("afl_team_id"),
            _text(row.get("afl_team_name"), f"players[{index}].afl_team_name"),
            row.get("eligible", True),
            row.get("source_updated_at"),
        )
        for index, row in enumerate(player_rows)
    )
    if not players:
        raise ReplayBootstrapError("player-pool capture contains no players")
    for index, player in enumerate(players):
        if not isinstance(player.canonical_player_id, int) or player.canonical_player_id <= 0:
            raise ReplayBootstrapError(f"players[{index}].canonical_player_id must be a positive integer")
        if not isinstance(player.afl_team_id, int) or player.afl_team_id <= 0:
            raise ReplayBootstrapError(f"players[{index}].afl_team_id must be a positive integer")
        if not isinstance(player.eligible, bool):
            raise ReplayBootstrapError(f"players[{index}].eligible must be boolean")
    canonical_ids = [player.canonical_player_id for player in players]
    if len(canonical_ids) != len(set(canonical_ids)):
        raise ReplayBootstrapError("player-pool capture contains duplicate canonical_player_id values")

    year = season.get("year")
    source_year = source.get("season_year")
    squad_limit = raw.get("squad_limit")
    rules_version = rules.get("version")
    if year != REPLAY_YEAR or source_year != REPLAY_YEAR:
        raise ReplayBootstrapError("this command only supports the 2026 replay; season and player source must be 2026")
    if not isinstance(squad_limit, int) or squad_limit <= 0:
        raise ReplayBootstrapError("squad_limit must be a positive integer")
    if not isinstance(rules_version, int) or rules_version <= 0:
        raise ReplayBootstrapError("rules.version must be a positive integer")
    if sum(player.eligible for player in players) < TEAM_COUNT * squad_limit:
        raise ReplayBootstrapError("player pool has fewer eligible players than the complete draft requires")
    provider = _text(source.get("provider"), "source.provider")
    if not provider.startswith("afl-api-"):
        raise ReplayBootstrapError("player pool source.provider must identify a supported afl-api capture")
    operator_email = _text(raw.get("operator_email"), "operator_email").casefold()
    if operator_email not in {entry.coach_email for entry in entries}:
        raise ReplayBootstrapError("operator_email must identify one of the ten configured coaches")
    opening_round_config = _opening_round_config(raw, config_path)
    return ReplayConfig(
        year,
        _text(season.get("label"), "season.label"),
        _text(rules.get("key"), "rules.key"),
        rules_version,
        _text(rules.get("name"), "rules.name"),
        _text(competition.get("key"), "competition.key"),
        _text(competition.get("label"), "competition.label"),
        squad_limit,
        operator_email,
        provider,
        source_year,
        players,
        entries,
        opening_round_config,
    )


def _id() -> str:
    return str(uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conflict(condition: bool, message: str) -> None:
    if condition:
        raise ReplayBootstrapError(message)


class ReplayOpeningRoundEvidenceValidator:
    """`app.round_mapping.AflReferenceValidator` backed only by the season
    and round *identity* facts (`seasons`/`rounds` sections) of a local
    acquired replay evidence file -- the same `bbbffl.replay-evidence/v1`
    schema `app.replay.ReplayAflDataSource` reads (see
    `app.replay_acquisition.acquire_first_half_2026`), so an operator can
    point `opening_round.evidence_file` at the exact evidence already
    acquired for the whole first-half replay.

    This intentionally does not construct a full `ReplayAflDataSource`:
    that class also demands complete match/player-stat coverage and, for a
    historical-checkpoint package (as first-half acquisition produces), an
    explicit persisted replay checkpoint -- none of which
    `OpeningRoundRuleRepository.accept()`'s round-existence check needs.
    Reading only season/round identity keeps Opening Round rule acceptance
    independent of whether match/stat evidence or a checkpoint exists yet,
    while still requiring genuine local evidence and never a live AFL-api
    client (`round_exists` never makes a network call)."""

    SCHEMA = "bbbffl.replay-evidence/v1"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReplayBootstrapError(f"could not read Opening Round replay evidence {self.path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema") != self.SCHEMA:
            raise ReplayBootstrapError(f"unsupported Opening Round replay evidence schema at {self.path}")
        try:
            seasons = payload["seasons"]
            rounds = payload["rounds"]
            if not isinstance(seasons, list) or not isinstance(rounds, list):
                raise TypeError("seasons and rounds must be lists")
            rounds_by_season: dict[int, set[int]] = {int(season["season_id"]): set() for season in seasons}
            round_entries = [(int(round_["round_id"]), int(round_["season_id"])) for round_ in rounds]
        except (KeyError, TypeError, ValueError) as exc:
            raise ReplayBootstrapError(f"malformed Opening Round replay evidence at {self.path}: {exc}") from exc
        # Mirrors app.replay.ReplayAflDataSource's own cross-reference check
        # (app/replay.py's round->season validation): a round referencing a
        # season missing from `seasons` is malformed evidence and must be
        # rejected outright, never silently admitted via `dict.setdefault`
        # -- which would let an internally inconsistent package validate a
        # round that no genuinely declared season actually carries.
        undeclared = sorted({season_id for _, season_id in round_entries if season_id not in rounds_by_season})
        if undeclared:
            raise ReplayBootstrapError(
                f"Opening Round replay evidence at {self.path} has rounds referencing "
                f"season(s) {undeclared} absent from its seasons list"
            )
        for round_id, season_id in round_entries:
            rounds_by_season[season_id].add(round_id)
        self._rounds_by_season = rounds_by_season

    def round_exists(self, afl_season_id: int, afl_round_id: int) -> bool:
        return afl_round_id in self._rounds_by_season.get(afl_season_id, set())


def _resolve_opening_round_targets(conn, competition_id: str, config: ReplayConfig) -> dict[int, str]:
    """Resolve every configured `bbbffl_round_number` to the persisted
    `bbbffl_round_id` created/reconciled earlier in this same transaction --
    the operator config never supplies a generated round UUID (issue #126).
    Fails if a configured target round cannot be uniquely resolved."""
    round_id_by_sequence = {
        row["sequence"]: row["bbbffl_round_id"]
        for row in conn.execute(
            "SELECT bbbffl_round_id, sequence FROM bbbffl_round WHERE competition_id=?", (competition_id,)
        ).fetchall()
    }
    targets: dict[int, str] = {}
    for rule_cfg in config.opening_round.rules:
        number = rule_cfg.bbbffl_round_number
        if number in targets:
            continue
        round_id = round_id_by_sequence.get(number)
        _conflict(round_id is None, f"expected BBBFFL round {number} to be uniquely resolved for Opening Round targets")
        targets[number] = round_id
    return targets


def _opening_round_rule_conflicts(
    existing: OpeningRoundRule, rule_cfg: OpeningRoundRuleConfig, config: ReplayConfig, bbbffl_round_id: str
) -> bool:
    return (
        existing.afl_season_id != config.opening_round.afl_season_id
        or existing.afl_opening_round_id != rule_cfg.afl_opening_round_id
        or existing.afl_bye_round_id != rule_cfg.afl_bye_round_id
        or existing.bbbffl_round_id != bbbffl_round_id
        or existing.evidence_classification != rule_cfg.evidence_classification
    )


def _reconcile_opening_round_rules(
    database, conn, season_id: str, competition_id: str, config: ReplayConfig, completed_picks: int
) -> None:
    """Accept the configured 2026 Opening Round rules through the ordinary
    `OpeningRoundRuleRepository` domain semantics, inside the caller's
    already-open bootstrap transaction (see `OpeningRoundRuleRepository.
    accept_locked` -- this never opens a second, independent transaction,
    which would let Opening Round rules commit even if the rest of
    bootstrap later rolls back). Conservative like #116: an identical
    accepted rule is a no-op, a materially different one fails closed, and
    an unexpected extra accepted rule for this season fails closed rather
    than being silently ignored.

    Establishing a *new* accepted rule (one with no existing accepted
    revision yet) is refused once any draft pick has completed: Opening
    Round configuration is a before-Pick-1 prerequisite, so a rerun against
    a season where drafting has already begun must never newly mutate that
    prerequisite state, even though the rest of this reconciliation is
    otherwise idempotent. An already-correct accepted rule remains a
    harmless no-op regardless of draft progress.

    `database` (the plain `DatabaseConnection`, distinct from the open
    transaction `conn`) is only used for `OpeningRoundRuleRepository`'s
    dialect-aware `FOR UPDATE` suffix -- every actual read/write below goes
    through `conn`, inside the caller's transaction."""
    rule_repo = OpeningRoundRuleRepository(database)
    target_round_ids = _resolve_opening_round_targets(conn, competition_id, config)
    validator = ReplayOpeningRoundEvidenceValidator(config.opening_round.evidence_file)
    expected_club_ids = {rule_cfg.afl_club_id for rule_cfg in config.opening_round.rules}
    existing_accepted = rule_repo.list_accepted_for_season_locked(conn, season_id)
    unexpected = sorted(rule.afl_club_id for rule in existing_accepted if rule.afl_club_id not in expected_club_ids)
    _conflict(
        bool(unexpected),
        f"unexpected accepted Opening Round rule(s) for club(s) {unexpected} not present in replay configuration",
    )
    actor = ActorContext("anonymous_operator", "replay-bootstrap", "admin")
    for rule_cfg in config.opening_round.rules:
        bbbffl_round_id = target_round_ids[rule_cfg.bbbffl_round_number]
        existing = rule_repo.resolve_locked(conn, season_id, rule_cfg.afl_club_id)
        if existing is not None:
            _conflict(
                _opening_round_rule_conflicts(existing, rule_cfg, config, bbbffl_round_id),
                f"existing accepted Opening Round rule for club {rule_cfg.afl_club_id} conflicts with replay configuration",
            )
            continue
        _conflict(
            completed_picks > 0,
            f"cannot establish a new Opening Round rule for club {rule_cfg.afl_club_id}: "
            f"{completed_picks} draft pick(s) already completed; Opening Round configuration "
            "is a before-Pick-1 prerequisite",
        )
        rule_repo.accept_locked(
            conn,
            season_id,
            rule_cfg.afl_club_id,
            config.opening_round.afl_season_id,
            rule_cfg.afl_opening_round_id,
            rule_cfg.afl_bye_round_id,
            bbbffl_round_id,
            validator,
            evidence_classification=rule_cfg.evidence_classification,
            actor=actor,
            reason="2026 first-half replay bootstrap: accepted Opening Round rule",
        )


def bootstrap_first_half(database, config: ReplayConfig) -> dict:
    """Create or exactly reconcile the complete Pick-1 prerequisite state."""
    now = _now()
    with transaction(database) as conn:
        season = conn.execute("SELECT * FROM bbbffl_season WHERE year=?", (config.year,)).fetchone()
        if season:
            _conflict(
                season["label"] != config.season_label
                or season["regular_season_round_count"] != len(FIRST_HALF_ROUNDS)
                or season["lifecycle_state"] != "setup",
                "existing season conflicts with replay label, nine-round length, or setup lifecycle",
            )
            season_id = season["season_id"]
        else:
            season_id = _id()
            conn.execute(
                "INSERT INTO bbbffl_season VALUES (?, ?, ?, 'setup', ?, ?, 1, ?)",
                (season_id, config.year, config.season_label, now, now, len(FIRST_HALF_ROUNDS)),
            )

        rules_rows = conn.execute("SELECT * FROM season_rules_version WHERE season_id=?", (season_id,)).fetchall()
        wanted_rules = [
            r for r in rules_rows if r["rules_key"] == config.rules_key and r["version_number"] == config.rules_version
        ]
        _conflict(
            bool(rules_rows) and (len(rules_rows) != 1 or len(wanted_rules) != 1),
            "existing rules versions conflict; exactly the configured replay rules version is required",
        )
        if wanted_rules:
            rules = wanted_rules[0]
            _conflict(rules["name"] != config.rules_name, "existing replay rules name conflicts with configuration")
            rules_id = rules["rules_version_id"]
        else:
            rules_id = _id()
            conn.execute(
                "INSERT INTO season_rules_version (rules_version_id, season_id, rules_key, version_number, name, notes, created_at, created_by, scoring_rules) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, NULL)",
                (
                    rules_id,
                    season_id,
                    config.rules_key,
                    config.rules_version,
                    config.rules_name,
                    now,
                    "replay-bootstrap",
                ),
            )

        competitions = conn.execute("SELECT * FROM competition_stream WHERE season_id=?", (season_id,)).fetchall()
        wanted = [c for c in competitions if c["stream_key"] == config.competition_key]
        _conflict(len(competitions) > 1 or (competitions and len(wanted) != 1), "existing competition streams conflict")
        if wanted:
            competition = wanted[0]
            _conflict(
                competition["label"] != config.competition_label
                or competition["stream_type"] != "ordinary"
                or competition["rules_version_id"] != rules_id,
                "existing ordinary competition conflicts with replay configuration",
            )
            competition_id = competition["competition_id"]
        else:
            competition_id = _id()
            conn.execute(
                "INSERT INTO competition_stream VALUES (?, ?, ?, ?, ?, 'ordinary', ?)",
                (competition_id, season_id, rules_id, config.competition_key, config.competition_label, now),
            )
        rounds = conn.execute("SELECT * FROM bbbffl_round WHERE competition_id=?", (competition_id,)).fetchall()
        if rounds:
            _conflict(
                [(r["sequence"], r["round_key"], r["label"]) for r in sorted(rounds, key=lambda r: r["sequence"])]
                != [(n, f"round-{n}", f"Round {n}") for n in FIRST_HALF_ROUNDS],
                "existing logical round structure conflicts; expected exactly rounds 1 through 9",
            )
        else:
            for number in FIRST_HALF_ROUNDS:
                conn.execute(
                    "INSERT INTO bbbffl_round VALUES (?, ?, ?, ?, ?, ?)",
                    (_id(), competition_id, f"round-{number}", f"Round {number}", number, now),
                )

        entry_ids: dict[int, str] = {}
        for item in config.entries:
            coach = conn.execute("SELECT * FROM coach WHERE lower(email)=lower(?)", (item.coach_email,)).fetchone()
            same_name = conn.execute(
                "SELECT * FROM coach WHERE lower(display_name)=lower(?)", (item.coach_display_name,)
            ).fetchall()
            if coach:
                _conflict(
                    coach["display_name"] != item.coach_display_name, f"coach identity conflict for {item.coach_email}"
                )
                coach_id = coach["coach_id"]
            else:
                _conflict(
                    bool(same_name), f"coach name {item.coach_display_name!r} already belongs to another identity"
                )
                coach_id = _id()
                conn.execute(
                    "INSERT INTO coach VALUES (?, ?, ?, NULL, NULL, ?, ?)",
                    (coach_id, item.coach_display_name, item.coach_email, now, now),
                )
            row = conn.execute(
                "SELECT e.season_entry_id, n.team_name, h.coach_id FROM season_entry e "
                "JOIN season_entry_team_name_history n ON n.season_entry_id=e.season_entry_id AND n.ended_at IS NULL "
                "JOIN season_entry_coach_history h ON h.season_entry_id=e.season_entry_id AND h.ended_at IS NULL "
                "WHERE e.season_id=? AND e.licence_key=?",
                (season_id, item.licence_key),
            ).fetchone()
            if row:
                _conflict(
                    row["team_name"] != item.team_name or row["coach_id"] != coach_id,
                    f"existing entry {item.licence_key!r} conflicts with replay identity",
                )
                entry_id = row["season_entry_id"]
            else:
                entry_id, assignment_id, name_id = _id(), _id(), _id()
                conn.execute(
                    "INSERT INTO season_entry VALUES (?, ?, ?, ?)", (entry_id, season_id, item.licence_key, now)
                )
                conn.execute(
                    "INSERT INTO season_entry_coach_history VALUES (?, ?, ?, ?, NULL, ?)",
                    (assignment_id, entry_id, coach_id, now, "2026 first-half replay bootstrap"),
                )
                conn.execute(
                    "INSERT INTO season_entry_team_name_history VALUES (?, ?, ?, ?, NULL, ?)",
                    (name_id, entry_id, item.team_name, now, "2026 first-half replay bootstrap"),
                )
            entry_ids[item.draft_position] = entry_id
        all_entries = conn.execute(
            "SELECT season_entry_id FROM season_entry WHERE season_id=?", (season_id,)
        ).fetchall()
        _conflict(len(all_entries) != TEAM_COUNT, "season contains entries outside the configured ten-team replay")

        squad = conn.execute(
            "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?", (season_id,)
        ).fetchone()
        _conflict(bool(squad and squad["squad_limit"] != config.squad_limit), "existing squad limit conflicts")
        if not squad:
            conn.execute(
                "INSERT INTO season_squad_configuration VALUES (?, ?, ?)", (season_id, config.squad_limit, now)
            )

        existing_players = conn.execute("SELECT * FROM season_player_pool WHERE season_id=?", (season_id,)).fetchall()
        expected_players = {p.canonical_player_id: p for p in config.players}
        _conflict(
            bool(existing_players) and {p["canonical_player_id"] for p in existing_players} != set(expected_players),
            "existing player pool canonical identities conflict with captured source",
        )
        for row in existing_players:
            player = expected_players[row["canonical_player_id"]]
            _conflict(
                row["display_name"] != player.display_name
                or row["afl_team_id"] != player.afl_team_id
                or row["afl_team_name"] != player.afl_team_name
                or bool(row["eligible"]) != player.eligible
                or row["source_provider"] != config.source_provider,
                f"existing player {player.canonical_player_id} conflicts with captured provider facts",
            )
        if not existing_players:
            for player in config.players:
                conn.execute(
                    "INSERT INTO season_player_pool VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _id(),
                        season_id,
                        player.canonical_player_id,
                        player.display_name,
                        player.afl_team_id,
                        player.afl_team_name,
                        player.eligible,
                        config.source_provider,
                        now,
                        player.source_updated_at,
                        now,
                        now,
                    ),
                )

        draft = conn.execute("SELECT * FROM season_draft WHERE season_id=?", (season_id,)).fetchone()
        ordered_ids = [entry_ids[n] for n in range(1, TEAM_COUNT + 1)]
        if draft:
            draft_id = draft["draft_id"]
            order = conn.execute(
                "SELECT position, season_entry_id FROM draft_order_position WHERE draft_id=? ORDER BY position",
                (draft_id,),
            ).fetchall()
            _conflict(
                draft["target_squad_size"] != config.squad_limit
                or [(r["position"], r["season_entry_id"]) for r in order] != list(enumerate(ordered_ids, 1)),
                "existing accepted draft order conflicts with replay configuration",
            )
            _conflict(
                draft["paused_at"] is not None or draft["finalized_at"] is not None,
                "existing draft operational state conflicts: draft must be unpaused and unfinalized for Pick 1",
            )
        else:
            draft_id = _id()
            conn.execute(
                "INSERT INTO season_draft VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL)",
                (draft_id, season_id, config.squad_limit, now),
            )
            for position, entry_id in enumerate(ordered_ids, 1):
                conn.execute(
                    "INSERT INTO draft_order_position VALUES (?, ?, ?, ?)", (draft_id, season_id, position, entry_id)
                )
            overall = 0
            for draft_round in range(1, config.squad_limit + 1):
                allocation = ordered_ids if draft_round % 2 else list(reversed(ordered_ids))
                for round_position, entry_id in enumerate(allocation, 1):
                    overall += 1
                    conn.execute(
                        "INSERT INTO draft_pick VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                        (_id(), draft_id, season_id, overall, draft_round, round_position, entry_id, entry_id),
                    )
            append_event(
                conn,
                actor=ActorContext("anonymous_operator", "replay-bootstrap", "admin"),
                action="replay.first_half.bootstrapped",
                entity_type="season",
                entity_id=season_id,
                after_state={"year": config.year, "entries": TEAM_COUNT, "rounds": list(FIRST_HALF_ROUNDS)},
            )

        completed_picks = conn.execute(
            "SELECT COUNT(*) n FROM draft_pick WHERE draft_id=? AND superseded_by_draft_pick_id IS NULL "
            "AND completed_at IS NOT NULL",
            (draft_id,),
        ).fetchone()["n"]
        _reconcile_opening_round_rules(database, conn, season_id, competition_id, config, completed_picks)
    return replay_readiness(database, config)


def provision_replay_operator(database, config: ReplayConfig, password: str) -> None:
    """Provision the one session-native Administrator needed by the runbook."""
    coach = IdentityRepository(database).get_coach_by_email(config.operator_email)
    if coach is None:
        raise ReplayBootstrapError("configured operator_email does not identify a bootstrapped coach")
    actor = ActorContext("anonymous_operator", "replay-bootstrap", "admin")
    CredentialRepository(database).set_password(
        coach.coach_id, password, actor=actor, reason="2026 replay operator credential provisioning"
    )
    grants = RoleGrantRepository(database)
    if not grants.is_role_granted(coach.coach_id, "admin"):
        grants.grant(coach.coach_id, "admin", actor=actor, reason="2026 replay browser operator")


def _opening_round_status(database, config: ReplayConfig, season_id: str, rounds: list[BBBFFLRound]) -> dict:
    """Structured Opening Round readiness (issue #126): how many of the
    configured 2026 rules are accepted *and* exactly match configuration,
    the resulting R2/R3/R4 target distribution, and the current (always
    expected to be zero pre-draft) nomination count. Never a prerequisite
    on any nomination or completed draft pick."""
    accepted_rules = OpeningRoundRuleRepository(database).list_accepted_for_season(season_id)
    accepted_by_club = {rule.afl_club_id: rule for rule in accepted_rules}
    round_id_by_sequence = {round_.sequence: round_.bbbffl_round_id for round_ in rounds}
    expected_rules = config.opening_round.rules
    expected_by_club = {rule_cfg.afl_club_id: rule_cfg for rule_cfg in expected_rules}
    verified = 0
    targets: dict[int, int] = {}
    for rule_cfg in expected_rules:
        existing = accepted_by_club.get(rule_cfg.afl_club_id)
        if existing is None:
            continue
        bbbffl_round_id = round_id_by_sequence.get(rule_cfg.bbbffl_round_number)
        if _opening_round_rule_conflicts(existing, rule_cfg, config, bbbffl_round_id):
            continue
        verified += 1
        targets[rule_cfg.bbbffl_round_number] = targets.get(rule_cfg.bbbffl_round_number, 0) + 1
    unexpected_extra = [rule for rule in accepted_rules if rule.afl_club_id not in expected_by_club]
    nomination_count = database.execute(
        "SELECT COUNT(*) n FROM opening_round_nomination WHERE season_id=?", (season_id,)
    ).fetchone()["n"]
    return {
        "expected_rule_count": len(expected_rules),
        "accepted_rule_count": verified,
        "complete": verified == len(expected_rules) and not unexpected_extra,
        "opening_round_id": expected_rules[0].afl_opening_round_id if expected_rules else None,
        "targets": {str(number): count for number, count in sorted(targets.items())},
        "nomination_count": nomination_count,
        "nominations_required_pre_draft": False,
    }


def replay_readiness(database, config: ReplayConfig) -> dict:
    """Derive an operator report from persisted state and Draft Board checks."""
    seasons = SeasonRepository(database)
    season = seasons.get_season_by_year(config.year)
    if not season:
        return {"season": config.year, "overall": "NOT READY", "messages": ["target season does not exist"]}
    identities = IdentityRepository(database)
    draft = DraftRepository(database)
    pool = PlayerPoolRepository(database)
    competitions = seasons.list_competitions(season.season_id)
    ordinary = [c for c in competitions if c.stream_type == "ordinary"]
    rounds = seasons.list_rounds(ordinary[0].competition_id) if len(ordinary) == 1 else []
    entries = identities.list_entries(season.season_id)
    order = draft.order(season.season_id)
    status = draft.status(season.season_id)
    squad = database.execute(
        "SELECT squad_limit FROM season_squad_configuration WHERE season_id=?", (season.season_id,)
    ).fetchone()
    completed = status.completed_picks if status else 0
    board = draft_board_readiness(database, identities, draft, pool, season.season_id)
    rules = seasons.list_rules_versions(season.season_id)
    rules_valid = (
        len(rules) == 1
        and rules[0].rules_key == config.rules_key
        and rules[0].version_number == config.rules_version
        and rules[0].name == config.rules_name
    )
    operator = identities.get_coach_by_email(config.operator_email)
    credential = (
        database.execute("SELECT 1 FROM coach_credential WHERE coach_id=?", (operator.coach_id,)).fetchone()
        if operator
        else None
    )
    operator_access = bool(
        operator and credential and RoleGrantRepository(database).is_role_granted(operator.coach_id, "admin")
    )
    opening_round_status = _opening_round_status(database, config, season.season_id, rounds)
    messages = []
    checks = {
        "one_ordinary_competition": len(competitions) == 1 and len(ordinary) == 1,
        "logical_rounds_1_to_9": [r.sequence for r in rounds] == list(FIRST_HALF_ROUNDS),
        "ten_entries": len(entries) == TEAM_COUNT,
        "ten_accepted_order_positions": len(order) == TEAM_COUNT,
        "eligible_player_pool": len(pool.list_selectable(season.season_id)) > 0,
        "zero_completed_picks": completed == 0,
        "draft_board_prerequisites": board["ready"],
        "exact_rules_version": rules_valid,
        "operator_authentication_provisioned": operator_access,
        "opening_round_configuration_complete": opening_round_status["complete"],
    }
    messages.extend(label.replace("_", " ") for label, ready in checks.items() if not ready)
    return {
        "season": asdict(season),
        "rules_version": asdict(rules[0]) if rules_valid else None,
        "competition": asdict(ordinary[0]) if len(ordinary) == 1 else None,
        "logical_rounds": [r.sequence for r in rounds],
        "season_entry_count": len(entries),
        "accepted_draft_order_count": len(order),
        "player_pool_count": len(pool.list_selectable(season.season_id)),
        "squad_size_limit": squad["squad_limit"] if squad else None,
        "completed_draft_picks_exist": completed > 0,
        "operator_email": config.operator_email,
        "operator_authentication_provisioned": operator_access,
        "next_human_action": "Pick 1" if board["ready"] and operator_access and completed == 0 else None,
        "draft_board": board,
        "opening_round": opening_round_status,
        "checks": checks,
        "messages": messages,
        "overall": "READY" if all(checks.values()) else "NOT READY",
    }
