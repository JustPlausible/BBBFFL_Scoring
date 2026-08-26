"""Repositories for private people and public, season-specific team identity."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from app.audit import ActorContext, append_event
from app.db import _for_update_suffix, transaction


def _id():
    return str(uuid4())


def _now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Coach:
    coach_id: str
    display_name: str
    email: str | None
    phone: str | None
    profile_notes: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SeasonEntry:
    season_entry_id: str
    season_id: str
    licence_key: str
    created_at: str


@dataclass(frozen=True)
class PublicTeam:
    season_entry_id: str
    season_id: str
    licence_key: str
    team_name: str


@dataclass(frozen=True)
class CoachAssignment:
    assignment_id: str
    season_entry_id: str
    coach_id: str
    started_at: str
    ended_at: str | None
    reason: str | None


@dataclass(frozen=True)
class TeamName:
    team_name_id: str
    season_entry_id: str
    team_name: str
    started_at: str
    ended_at: str | None
    reason: str | None


class IdentityRepository:
    def __init__(self, database):
        self.database = database

    def create_coach(self, display_name, *, email=None, phone=None, profile_notes=None):
        now = _now()
        item = Coach(_id(), display_name, email, phone, profile_notes, now, now)
        with transaction(self.database) as conn:
            conn.execute("INSERT INTO coach VALUES (?, ?, ?, ?, ?, ?, ?)", tuple(item.__dict__.values()))
        return item

    def create_entry(self, season_id, licence_key, coach_id, team_name, *, actor=ActorContext.anonymous_operator("admin"), reason=None, effective_at=None):
        at = effective_at or _now()
        entry = SeasonEntry(_id(), season_id, licence_key, at)
        assignment = CoachAssignment(_id(), entry.season_entry_id, coach_id, at, None, reason)
        name = TeamName(_id(), entry.season_entry_id, team_name, at, None, reason)
        with transaction(self.database) as conn:
            conn.execute("INSERT INTO season_entry VALUES (?, ?, ?, ?)", tuple(entry.__dict__.values()))
            conn.execute("INSERT INTO season_entry_coach_history VALUES (?, ?, ?, ?, ?, ?)", tuple(assignment.__dict__.values()))
            conn.execute("INSERT INTO season_entry_team_name_history VALUES (?, ?, ?, ?, ?, ?)", tuple(name.__dict__.values()))
            append_event(conn, actor=actor, action="identity.season_entry.created", entity_type="season_entry", entity_id=entry.season_entry_id, reason=reason, after_state={"season_id": season_id, "licence_key": licence_key, "coach_id": coach_id, "team_name": team_name})
        return entry

    def rename_team(self, entry_id, team_name, *, actor=ActorContext.anonymous_operator("admin"), reason=None, effective_at=None):
        at = effective_at or _now()
        with transaction(self.database) as conn:
            old = conn.execute("SELECT * FROM season_entry_team_name_history WHERE season_entry_id=? AND ended_at IS NULL" + _for_update_suffix(self.database), (entry_id,)).fetchone()
            if not old:
                raise KeyError(entry_id)
            conn.execute("UPDATE season_entry_team_name_history SET ended_at=? WHERE team_name_id=?", (at, old["team_name_id"]))
            item = TeamName(_id(), entry_id, team_name, at, None, reason)
            conn.execute("INSERT INTO season_entry_team_name_history VALUES (?, ?, ?, ?, ?, ?)", tuple(item.__dict__.values()))
            append_event(conn, actor=actor, action="identity.team_name.changed", entity_type="season_entry", entity_id=entry_id, reason=reason, before_state={"team_name": old["team_name"]}, after_state={"team_name": team_name})
        return item

    def transfer_entry(self, entry_id, coach_id, *, actor=ActorContext.anonymous_operator("admin"), reason=None, effective_at=None):
        at = effective_at or _now()
        with transaction(self.database) as conn:
            old = conn.execute("SELECT * FROM season_entry_coach_history WHERE season_entry_id=? AND ended_at IS NULL" + _for_update_suffix(self.database), (entry_id,)).fetchone()
            if not old:
                raise KeyError(entry_id)
            conn.execute("UPDATE season_entry_coach_history SET ended_at=? WHERE assignment_id=?", (at, old["assignment_id"]))
            item = CoachAssignment(_id(), entry_id, coach_id, at, None, reason)
            conn.execute("INSERT INTO season_entry_coach_history VALUES (?, ?, ?, ?, ?, ?)", tuple(item.__dict__.values()))
            append_event(conn, actor=actor, action="identity.season_entry.coach_changed", entity_type="season_entry", entity_id=entry_id, reason=reason, before_state={"coach_id": old["coach_id"]}, after_state={"coach_id": coach_id})
        return item

    def get_public_team(self, entry_id):
        row = self.database.execute("SELECT e.season_entry_id, e.season_id, e.licence_key, n.team_name FROM season_entry e JOIN season_entry_team_name_history n ON n.season_entry_id=e.season_entry_id AND n.ended_at IS NULL WHERE e.season_entry_id=?", (entry_id,)).fetchone()
        return PublicTeam(**dict(row)) if row else None

    def list_assignments(self, entry_id):
        rows = self.database.execute("SELECT * FROM season_entry_coach_history WHERE season_entry_id=? ORDER BY started_at", (entry_id,)).fetchall()
        return [CoachAssignment(**dict(row)) for row in rows]

    def list_team_names(self, entry_id):
        rows = self.database.execute("SELECT * FROM season_entry_team_name_history WHERE season_entry_id=? ORDER BY started_at", (entry_id,)).fetchall()
        return [TeamName(**dict(row)) for row in rows]
