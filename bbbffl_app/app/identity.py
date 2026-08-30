"""Repositories for private people and public, season-specific team identity."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.audit import ActorContext, append_event
from app.db import DatabaseConnection, _for_update_suffix, transaction


class _UnsetType:
    """Sentinel type distinguishing "this keyword was not supplied" from an
    explicit ``None`` -- see `IdentityRepository.update_coach`'s docstring.
    A dedicated type (rather than a bare ``object()``) so a keyword's type
    hint can name it explicitly and stay checkable under this module's
    strict mypy gate (see [tool.mypy] in pyproject.toml)."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _UnsetType()


class CoachEmailConflictError(ValueError):
    """Another coach already has this email (case-insensitively) -- the
    unique index `migrations/versions/0021_coach_authentication.py` adds on
    `lower(coach.email)`."""


def _id() -> str:
    return str(uuid4())


def _now() -> str:
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


@dataclass(frozen=True)
class SeasonEntryOverview:
    """One season entry's current public/coach facts, joined for operator
    presentation layers (the Season Centre, issue #100) that need "team name
    + coach display name" without touching private coach contact data --
    deliberately narrower than `Coach`/`get_coach`, mirroring `PublicTeam`'s
    existing privacy boundary."""

    season_entry_id: str
    season_id: str
    licence_key: str
    created_at: str
    team_name: str
    coach_id: str
    coach_display_name: str


class IdentityRepository:
    def __init__(self, database: DatabaseConnection):
        self.database = database

    def create_coach(
        self,
        display_name: str,
        *,
        email: str | None = None,
        phone: str | None = None,
        profile_notes: str | None = None,
    ) -> Coach:
        now = _now()
        item = Coach(_id(), display_name, email, phone, profile_notes, now, now)
        try:
            with transaction(self.database) as conn:
                conn.execute("INSERT INTO coach VALUES (?, ?, ?, ?, ?, ?, ?)", tuple(item.__dict__.values()))
        except IntegrityError as exc:
            raise CoachEmailConflictError(f"another coach already uses the email {email!r}") from exc
        return item

    def create_entry(
        self,
        season_id: str,
        licence_key: str,
        coach_id: str,
        team_name: str,
        *,
        actor: ActorContext = ActorContext.anonymous_operator("admin"),
        reason: str | None = None,
        effective_at: str | None = None,
    ) -> SeasonEntry:
        at = effective_at or _now()
        entry = SeasonEntry(_id(), season_id, licence_key, at)
        assignment = CoachAssignment(_id(), entry.season_entry_id, coach_id, at, None, reason)
        name = TeamName(_id(), entry.season_entry_id, team_name, at, None, reason)
        with transaction(self.database) as conn:
            conn.execute("INSERT INTO season_entry VALUES (?, ?, ?, ?)", tuple(entry.__dict__.values()))
            conn.execute(
                "INSERT INTO season_entry_coach_history VALUES (?, ?, ?, ?, ?, ?)", tuple(assignment.__dict__.values())
            )
            conn.execute(
                "INSERT INTO season_entry_team_name_history VALUES (?, ?, ?, ?, ?, ?)", tuple(name.__dict__.values())
            )
            append_event(
                conn,
                actor=actor,
                action="identity.season_entry.created",
                entity_type="season_entry",
                entity_id=entry.season_entry_id,
                reason=reason,
                after_state={
                    "season_id": season_id,
                    "licence_key": licence_key,
                    "coach_id": coach_id,
                    "team_name": team_name,
                },
            )
        return entry

    def rename_team(
        self,
        entry_id: str,
        team_name: str,
        *,
        actor: ActorContext = ActorContext.anonymous_operator("admin"),
        reason: str | None = None,
        effective_at: str | None = None,
    ) -> TeamName:
        at = effective_at or _now()
        with transaction(self.database) as conn:
            entry = conn.execute(
                "SELECT season_entry_id FROM season_entry WHERE season_entry_id=?" + _for_update_suffix(self.database),
                (entry_id,),
            ).fetchone()
            if not entry:
                raise KeyError(entry_id)
            old = conn.execute(
                "SELECT * FROM season_entry_team_name_history WHERE season_entry_id=? AND ended_at IS NULL",
                (entry_id,),
            ).fetchone()
            if not old:
                raise KeyError(entry_id)
            conn.execute(
                "UPDATE season_entry_team_name_history SET ended_at=? WHERE team_name_id=?", (at, old["team_name_id"])
            )
            item = TeamName(_id(), entry_id, team_name, at, None, reason)
            conn.execute(
                "INSERT INTO season_entry_team_name_history VALUES (?, ?, ?, ?, ?, ?)", tuple(item.__dict__.values())
            )
            append_event(
                conn,
                actor=actor,
                action="identity.team_name.changed",
                entity_type="season_entry",
                entity_id=entry_id,
                reason=reason,
                before_state={"team_name": old["team_name"]},
                after_state={"team_name": team_name},
            )
        return item

    def transfer_entry(
        self,
        entry_id: str,
        coach_id: str,
        *,
        actor: ActorContext = ActorContext.anonymous_operator("admin"),
        reason: str | None = None,
        effective_at: str | None = None,
    ) -> CoachAssignment:
        at = effective_at or _now()
        with transaction(self.database) as conn:
            entry = conn.execute(
                "SELECT season_entry_id FROM season_entry WHERE season_entry_id=?" + _for_update_suffix(self.database),
                (entry_id,),
            ).fetchone()
            if not entry:
                raise KeyError(entry_id)
            old = conn.execute(
                "SELECT * FROM season_entry_coach_history WHERE season_entry_id=? AND ended_at IS NULL",
                (entry_id,),
            ).fetchone()
            if not old:
                raise KeyError(entry_id)
            conn.execute(
                "UPDATE season_entry_coach_history SET ended_at=? WHERE assignment_id=?", (at, old["assignment_id"])
            )
            item = CoachAssignment(_id(), entry_id, coach_id, at, None, reason)
            conn.execute(
                "INSERT INTO season_entry_coach_history VALUES (?, ?, ?, ?, ?, ?)", tuple(item.__dict__.values())
            )
            append_event(
                conn,
                actor=actor,
                action="identity.season_entry.coach_changed",
                entity_type="season_entry",
                entity_id=entry_id,
                reason=reason,
                before_state={"coach_id": old["coach_id"]},
                after_state={"coach_id": coach_id},
            )
        return item

    def get_coach(self, coach_id: str) -> Coach | None:
        row = self.database.execute("SELECT * FROM coach WHERE coach_id = ?", (coach_id,)).fetchone()
        return Coach(**dict(row)) if row else None

    def list_coaches(self) -> list[Coach]:
        """Every persistent coach record, for an operator's coach-picker/
        management view (issue #100's Season Centre) -- coaches are not
        season-scoped (see this module's docstring), so this is not filtered
        by season_id."""
        rows = self.database.execute("SELECT * FROM coach ORDER BY display_name").fetchall()
        return [Coach(**dict(row)) for row in rows]

    def update_coach(
        self,
        coach_id: str,
        *,
        display_name: str | None | _UnsetType = UNSET,
        email: str | None | _UnsetType = UNSET,
        phone: str | None | _UnsetType = UNSET,
        profile_notes: str | None | _UnsetType = UNSET,
        actor: ActorContext = ActorContext.anonymous_operator("admin"),
        reason: str | None = None,
    ) -> Coach:
        """Update a coach's own private record in place (issue #100's
        scorer/admin coach-editing workflow). Unlike team name/coach
        assignment, a coach's own display name/contact details have no
        separate history table -- there is exactly one current coach row,
        matching `create_coach`'s shape.

        Each keyword defaults to the `UNSET` sentinel, not ``None``: an
        *omitted* field keeps its current value, while an explicit ``None``
        clears it (e.g. removing a coach's email so it no longer resolves
        through `get_coach_by_email` for a future login). Passing a plain
        ``None`` default here would make "leave email alone" and "clear
        email" indistinguishable -- exactly the ambiguity this sentinel
        exists to avoid.

        The audit event never carries email/phone/profile_notes (see
        `test_identity.py`'s existing "audit events never carry private
        contact data" convention for season-entry renames -- the same
        discipline applies here)."""
        now = _now()
        with transaction(self.database) as conn:
            row = conn.execute(
                "SELECT * FROM coach WHERE coach_id=?" + _for_update_suffix(self.database), (coach_id,)
            ).fetchone()
            if not row:
                raise KeyError(coach_id)
            new_display_name: str | None = row["display_name"] if isinstance(display_name, _UnsetType) else display_name
            if not new_display_name or not new_display_name.strip():
                raise ValueError("coach display name must not be empty")
            new_email: str | None = row["email"] if isinstance(email, _UnsetType) else email
            new_phone: str | None = row["phone"] if isinstance(phone, _UnsetType) else phone
            new_notes: str | None = row["profile_notes"] if isinstance(profile_notes, _UnsetType) else profile_notes
            try:
                conn.execute(
                    "UPDATE coach SET display_name=?, email=?, phone=?, profile_notes=?, updated_at=? WHERE coach_id=?",
                    (new_display_name, new_email, new_phone, new_notes, now, coach_id),
                )
            except IntegrityError as exc:
                raise CoachEmailConflictError(f"another coach already uses the email {new_email!r}") from exc
            append_event(
                conn,
                actor=actor,
                action="identity.coach.updated",
                entity_type="coach",
                entity_id=coach_id,
                reason=reason,
                before_state={"display_name": row["display_name"]},
                after_state={"display_name": new_display_name},
            )
        return Coach(coach_id, new_display_name, new_email, new_phone, new_notes, row["created_at"], now)

    def get_coach_by_email(self, email: str) -> Coach | None:
        """Case-insensitive lookup by the coach's own private email --
        used by `app.auth` to resolve a login identifier to the existing
        persistent coach identity (roadmap package 19, issue #74). Never
        used as a durable foreign key elsewhere: `coach_id` remains the
        stable identity (see this module's docstring and #20's "do not
        make email address ... the primary identity" design constraint)."""
        row = self.database.execute(
            "SELECT * FROM coach WHERE email IS NOT NULL AND lower(email) = lower(?)", (email,)
        ).fetchone()
        return Coach(**dict(row)) if row else None

    def get_current_coach(self, entry_id: str) -> Coach | None:
        row = self.database.execute(
            "SELECT c.* FROM season_entry_coach_history h JOIN coach c ON c.coach_id = h.coach_id "
            "WHERE h.season_entry_id = ? AND h.ended_at IS NULL",
            (entry_id,),
        ).fetchone()
        return Coach(**dict(row)) if row else None

    def get_public_team(self, entry_id: str) -> PublicTeam | None:
        row = self.database.execute(
            "SELECT e.season_entry_id, e.season_id, e.licence_key, n.team_name FROM season_entry e JOIN season_entry_team_name_history n ON n.season_entry_id=e.season_entry_id AND n.ended_at IS NULL WHERE e.season_entry_id=?",
            (entry_id,),
        ).fetchone()
        return PublicTeam(**dict(row)) if row else None

    def coach_owns_entry(self, coach_id: str | None, entry_id: str) -> bool:
        """Whether the persistent coach currently represents this season entry."""
        if coach_id is None:
            return False
        row = self.database.execute(
            "SELECT 1 FROM season_entry_coach_history WHERE season_entry_id=? AND coach_id=? AND ended_at IS NULL",
            (entry_id, coach_id),
        ).fetchone()
        return row is not None

    def list_assignments(self, entry_id: str) -> list[CoachAssignment]:
        rows = self.database.execute(
            "SELECT * FROM season_entry_coach_history WHERE season_entry_id=? ORDER BY started_at", (entry_id,)
        ).fetchall()
        return [CoachAssignment(**dict(row)) for row in rows]

    def list_team_names(self, entry_id: str) -> list[TeamName]:
        rows = self.database.execute(
            "SELECT * FROM season_entry_team_name_history WHERE season_entry_id=? ORDER BY started_at", (entry_id,)
        ).fetchall()
        return [TeamName(**dict(row)) for row in rows]

    def list_entries(self, season_id: str) -> list[SeasonEntryOverview]:
        """Every season entry's current public team name and current coach
        display name for one season, in a single read -- the Season Centre's
        (issue #100) primary operator view. Deliberately excludes coach
        email/phone/profile_notes: see `SeasonEntryOverview`'s docstring."""
        rows = self.database.execute(
            "SELECT e.season_entry_id, e.season_id, e.licence_key, e.created_at, "
            "n.team_name, c.coach_id, c.display_name AS coach_display_name "
            "FROM season_entry e "
            "JOIN season_entry_team_name_history n ON n.season_entry_id=e.season_entry_id AND n.ended_at IS NULL "
            "JOIN season_entry_coach_history h ON h.season_entry_id=e.season_entry_id AND h.ended_at IS NULL "
            "JOIN coach c ON c.coach_id=h.coach_id "
            "WHERE e.season_id=? ORDER BY n.team_name",
            (season_id,),
        ).fetchall()
        return [SeasonEntryOverview(**dict(row)) for row in rows]
