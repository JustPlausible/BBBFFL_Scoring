"""Repositories for explicit, season-scoped BBBFFL parent identities."""
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from app.audit import ActorContext, append_event
from app.db import _for_update_suffix, transaction

SEASON_LIFECYCLE_CHANGED = "season.lifecycle.changed"
RULES_VERSION_CREATED = "season.rules_version.created"
LEGAL_TRANSITIONS = {"setup": {"active"}, "active": {"completed"}, "completed": set()}


def _id(): return str(uuid4())
def _now(): return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Season:
    season_id: str; year: int; label: str; lifecycle_state: str; created_at: str; updated_at: str; version: int


@dataclass(frozen=True)
class RulesVersion:
    rules_version_id: str; season_id: str; rules_key: str; version_number: int; name: str; notes: str | None; created_at: str; created_by: str | None


@dataclass(frozen=True)
class CompetitionStream:
    competition_id: str; season_id: str; rules_version_id: str; stream_key: str; label: str; stream_type: str; created_at: str


@dataclass(frozen=True)
class BBBFFLRound:
    bbbffl_round_id: str; competition_id: str; round_key: str; label: str; sequence: int; created_at: str


class SeasonRepository:
    def __init__(self, database): self.database = database

    def create_season(self, year: int, label: str, *, lifecycle_state: str = "setup") -> Season:
        if lifecycle_state not in LEGAL_TRANSITIONS: raise ValueError("invalid season lifecycle state")
        item = Season(_id(), year, label, lifecycle_state, _now(), _now(), 1)
        with transaction(self.database) as c:
            c.execute("INSERT INTO bbbffl_season VALUES (?, ?, ?, ?, ?, ?, ?)", tuple(item.__dict__.values()))
        return item

    def get_season(self, season_id: str) -> Season | None:
        row = self.database.execute("SELECT * FROM bbbffl_season WHERE season_id = ?", (season_id,)).fetchone()
        return Season(**dict(row)) if row else None

    def transition_lifecycle(self, season_id: str, target: str, *, actor: ActorContext = ActorContext.anonymous_operator("admin"), reason: str | None = None) -> Season:
        with transaction(self.database) as c:
            row = c.execute("SELECT * FROM bbbffl_season WHERE season_id = ?" + _for_update_suffix(self.database), (season_id,)).fetchone()
            if not row: raise KeyError(season_id)
            old = row["lifecycle_state"]
            if target not in LEGAL_TRANSITIONS[old]: raise ValueError(f"illegal lifecycle transition: {old} -> {target}")
            now, version = _now(), row["version"] + 1
            c.execute("UPDATE bbbffl_season SET lifecycle_state=?, updated_at=?, version=? WHERE season_id=?", (target, now, version, season_id))
            append_event(c, actor=actor, action=SEASON_LIFECYCLE_CHANGED, entity_type="season", entity_id=season_id, entity_version=str(version), reason=reason, before_state={"lifecycle_state": old}, after_state={"lifecycle_state": target})
        return self.get_season(season_id)

    def create_rules_version(self, season_id: str, rules_key: str, version_number: int, name: str, *, notes: str | None = None, actor: ActorContext = ActorContext.anonymous_operator("admin")) -> RulesVersion:
        item = RulesVersion(_id(), season_id, rules_key, version_number, name, notes, _now(), actor.actor_id)
        with transaction(self.database) as c:
            c.execute("INSERT INTO season_rules_version VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(item.__dict__.values()))
            append_event(c, actor=actor, action=RULES_VERSION_CREATED, entity_type="season.rules_version", entity_id=item.rules_version_id, entity_version=str(version_number), after_state={"season_id": season_id, "rules_key": rules_key, "version_number": version_number})
        return item

    def list_rules_versions(self, season_id: str) -> list[RulesVersion]:
        rows = self.database.execute("SELECT * FROM season_rules_version WHERE season_id=? ORDER BY rules_key, version_number", (season_id,)).fetchall()
        return [RulesVersion(**dict(r)) for r in rows]

    def create_competition(self, season_id: str, rules_version_id: str, stream_key: str, label: str, stream_type: str) -> CompetitionStream:
        rules = self.database.execute("SELECT season_id FROM season_rules_version WHERE rules_version_id=?", (rules_version_id,)).fetchone()
        if not rules or rules["season_id"] != season_id: raise ValueError("rules version must belong to competition season")
        item = CompetitionStream(_id(), season_id, rules_version_id, stream_key, label, stream_type, _now())
        with transaction(self.database) as c: c.execute("INSERT INTO competition_stream VALUES (?, ?, ?, ?, ?, ?, ?)", tuple(item.__dict__.values()))
        return item

    def list_competitions(self, season_id: str) -> list[CompetitionStream]:
        rows = self.database.execute("SELECT * FROM competition_stream WHERE season_id=? ORDER BY stream_key", (season_id,)).fetchall()
        return [CompetitionStream(**dict(r)) for r in rows]

    def create_round(self, competition_id: str, round_key: str, label: str, sequence: int) -> BBBFFLRound:
        item = BBBFFLRound(_id(), competition_id, round_key, label, sequence, _now())
        with transaction(self.database) as c: c.execute("INSERT INTO bbbffl_round VALUES (?, ?, ?, ?, ?, ?)", tuple(item.__dict__.values()))
        return item

    def list_rounds(self, competition_id: str) -> list[BBBFFLRound]:
        rows = self.database.execute("SELECT * FROM bbbffl_round WHERE competition_id=? ORDER BY sequence", (competition_id,)).fetchall()
        return [BBBFFLRound(**dict(r)) for r in rows]

    def map_afl_round(self, bbbffl_round_id: str, afl_season_id: int, afl_round_id: int, *, provider: str = "afl-api-v1") -> str:
        mapping_id = _id()
        with transaction(self.database) as c:
            c.execute("INSERT INTO bbbffl_round_afl_reference VALUES (?, ?, ?, ?, ?, ?)", (mapping_id, bbbffl_round_id, provider, afl_season_id, afl_round_id, _now()))
        return mapping_id

    def rounds_for_afl_reference(self, afl_season_id: int, afl_round_id: int, *, provider: str = "afl-api-v1") -> list[BBBFFLRound]:
        rows = self.database.execute("SELECT r.* FROM bbbffl_round r JOIN bbbffl_round_afl_reference m ON m.bbbffl_round_id=r.bbbffl_round_id WHERE m.provider=? AND m.afl_season_id=? AND m.afl_round_id=? ORDER BY r.bbbffl_round_id", (provider, afl_season_id, afl_round_id)).fetchall()
        return [BBBFFLRound(**dict(r)) for r in rows]
