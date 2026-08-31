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
from app.player_pool import PlayerPoolRepository
from app.season import SeasonRepository

TEAM_COUNT = 10
FIRST_HALF_ROUNDS = tuple(range(1, 10))
REPLAY_YEAR = 2026


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


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReplayBootstrapError(f"{field} must be a non-empty string")
    return value.strip()


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
    )


def _id() -> str:
    return str(uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conflict(condition: bool, message: str) -> None:
    if condition:
        raise ReplayBootstrapError(message)


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
            order = conn.execute(
                "SELECT position, season_entry_id FROM draft_order_position WHERE draft_id=? ORDER BY position",
                (draft["draft_id"],),
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
        "checks": checks,
        "messages": messages,
        "overall": "READY" if all(checks.values()) else "NOT READY",
    }
