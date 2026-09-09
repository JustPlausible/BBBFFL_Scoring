"""Repositories for explicit, season-scoped BBBFFL parent identities."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import inspect

from app.audit import ActorContext, append_event
from app.db import DatabaseConnection, _for_update_suffix, transaction

SEASON_LIFECYCLE_CHANGED = "season.lifecycle.changed"
RULES_VERSION_CREATED = "season.rules_version.created"
LEGAL_TRANSITIONS = {
    "setup": {"active"},
    "active": {"completed"},
    "completed": set(),
}


def _id() -> str:
    return str(uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Season:
    season_id: str
    year: int
    label: str
    lifecycle_state: str
    created_at: str
    updated_at: str
    version: int
    regular_season_round_count: int
    # Issue #164: the BBBFFL round after which a mid-season draft occurs.
    # `None` means the season has not configured one (most seasons, and
    # every season predating this column).
    midseason_draft_trigger_round: int | None = None


@dataclass(frozen=True)
class RulesVersion:
    rules_version_id: str
    season_id: str
    rules_key: str
    version_number: int
    name: str
    notes: str | None
    created_at: str
    created_by: str | None
    scoring_rules: dict | None = None

    @property
    def display_label(self) -> str:
        """Issue #151: the human-readable rules name/version a Scorer/Admin
        surface should show in place of a bare `rules_version_id` -- the
        `name`/`version_number` this table already records, never a new
        field or a schema change."""
        return f"{self.name} (v{self.version_number})"


@dataclass(frozen=True)
class CompetitionStream:
    competition_id: str
    season_id: str
    rules_version_id: str
    stream_key: str
    label: str
    stream_type: str
    created_at: str


@dataclass(frozen=True)
class BBBFFLRound:
    bbbffl_round_id: str
    competition_id: str
    round_key: str
    label: str
    sequence: int
    created_at: str


class SeasonRepository:
    def __init__(self, database: DatabaseConnection):
        self.database = database

    def _has_round_count(self) -> bool:
        return "regular_season_round_count" in {
            column["name"] for column in inspect(self.database.engine).get_columns("bbbffl_season")
        }

    def _has_midseason_trigger_round(self) -> bool:
        return "midseason_draft_trigger_round" in {
            column["name"] for column in inspect(self.database.engine).get_columns("bbbffl_season")
        }

    @staticmethod
    def _season_from_row(row: Mapping[str, Any]) -> Season:
        values = dict(row)
        values.setdefault("regular_season_round_count", 20)
        values.setdefault("midseason_draft_trigger_round", None)
        return Season(**values)

    def create_season(
        self,
        year: int,
        label: str,
        *,
        lifecycle_state: str = "setup",
        regular_season_round_count: int = 20,
        midseason_draft_trigger_round: int | None = None,
    ) -> Season:
        if lifecycle_state not in LEGAL_TRANSITIONS:
            raise ValueError("invalid season lifecycle state")
        if regular_season_round_count < 1:
            raise ValueError("regular season round count must be positive")
        if midseason_draft_trigger_round is not None and midseason_draft_trigger_round < 1:
            raise ValueError("mid-season draft trigger round must be positive")
        now = _now()
        item = Season(
            _id(),
            year,
            label,
            lifecycle_state,
            now,
            now,
            1,
            regular_season_round_count,
            midseason_draft_trigger_round,
        )
        with transaction(self.database) as connection:
            has_round_count = self._has_round_count()
            has_trigger_round = self._has_midseason_trigger_round()
            if has_round_count and has_trigger_round:
                connection.execute(
                    "INSERT INTO bbbffl_season "
                    "(season_id, year, label, lifecycle_state, created_at, updated_at, version, "
                    "regular_season_round_count, midseason_draft_trigger_round) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(item.__dict__.values()),
                )
            elif has_round_count:
                # Migration/CI compatibility while deliberately exercising a
                # pre-0027 schema.
                connection.execute(
                    "INSERT INTO bbbffl_season VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(item.__dict__.values())[:-1],
                )
            else:
                # Migration/CI compatibility while deliberately exercising a
                # pre-0009 schema; its implicit historical value is 20.
                connection.execute(
                    "INSERT INTO bbbffl_season VALUES (?, ?, ?, ?, ?, ?, ?)",
                    tuple(item.__dict__.values())[:-2],
                )
        return item

    def set_midseason_draft_trigger_round(
        self,
        season_id: str,
        trigger_round: int,
        *,
        actor: ActorContext = ActorContext.anonymous_operator("admin"),
        reason: str | None = None,
    ) -> Season:
        """Record (or change) the BBBFFL round after which the mid-season
        draft occurs -- issue #164's "the season setup must explicitly
        identify the BBBFFL round". Separate from `create_season` so an
        already-established season (e.g. the 2026 replay, bootstrapped long
        before this column existed) can still configure it. Refused once a
        mid-season draft already exists for the season: the trigger round
        that draft was confirmed against must remain traceable, exactly like
        `regular_season_round_count`'s frozen-once-the-fixture-draw-exists
        rule (migration 0009)."""
        if trigger_round < 1:
            raise ValueError("mid-season draft trigger round must be positive")
        with transaction(self.database) as connection:
            row = connection.execute(
                "SELECT * FROM bbbffl_season WHERE season_id=?" + _for_update_suffix(self.database), (season_id,)
            ).fetchone()
            if not row:
                raise KeyError(season_id)
            if connection.execute("SELECT 1 FROM midseason_draft WHERE season_id=?", (season_id,)).fetchone():
                raise ValueError("mid-season draft trigger round cannot change once a mid-season draft exists")
            before = row["midseason_draft_trigger_round"] if "midseason_draft_trigger_round" in row.keys() else None
            updated_at = _now()
            connection.execute(
                "UPDATE bbbffl_season SET midseason_draft_trigger_round=?, updated_at=? WHERE season_id=?",
                (trigger_round, updated_at, season_id),
            )
            append_event(
                connection,
                actor=actor,
                action="season.midseason_draft_trigger_round.set",
                entity_type="season",
                entity_id=season_id,
                reason=reason,
                before_state={"midseason_draft_trigger_round": before},
                after_state={"midseason_draft_trigger_round": trigger_round},
            )
            # Construct the result from the locked row and exact values
            # written above, matching `transition_lifecycle`'s convention --
            # an unlocked post-commit read could observe a later change.
            result = Season(
                season_id=season_id,
                year=row["year"],
                label=row["label"],
                lifecycle_state=row["lifecycle_state"],
                created_at=row["created_at"],
                updated_at=updated_at,
                version=row["version"],
                regular_season_round_count=(
                    row["regular_season_round_count"] if "regular_season_round_count" in row.keys() else 20
                ),
                midseason_draft_trigger_round=trigger_round,
            )
        return result

    def get_season(self, season_id: str) -> Season | None:
        row = self.database.execute("SELECT * FROM bbbffl_season WHERE season_id = ?", (season_id,)).fetchone()
        return self._season_from_row(row) if row else None

    def get_season_by_year(self, year: int) -> Season | None:
        row = self.database.execute("SELECT * FROM bbbffl_season WHERE year = ?", (year,)).fetchone()
        return self._season_from_row(row) if row else None

    def list_seasons(self) -> list[Season]:
        """Every season, most recent year first -- lets an operator surface
        (the Season Centre, issue #100) pick a season without a caller
        needing to already know its UUID, and keeps distinct seasons (e.g.
        the 2026 replay and 2027) explicitly separated rather than any one
        of them becoming an implicit default."""
        rows = self.database.execute("SELECT * FROM bbbffl_season ORDER BY year DESC").fetchall()
        return [self._season_from_row(row) for row in rows]

    def transition_lifecycle(
        self,
        season_id: str,
        target: str,
        *,
        actor: ActorContext = ActorContext.anonymous_operator("admin"),
        reason: str | None = None,
    ) -> Season:
        with transaction(self.database) as connection:
            row = connection.execute(
                "SELECT * FROM bbbffl_season WHERE season_id = ?" + _for_update_suffix(self.database),
                (season_id,),
            ).fetchone()
            if not row:
                raise KeyError(season_id)

            old_state = row["lifecycle_state"]
            if target not in LEGAL_TRANSITIONS[old_state]:
                raise ValueError(f"illegal lifecycle transition: {old_state} -> {target}")

            updated_at = _now()
            version = row["version"] + 1
            connection.execute(
                "UPDATE bbbffl_season SET lifecycle_state=?, updated_at=?, version=? WHERE season_id=?",
                (target, updated_at, version, season_id),
            )
            append_event(
                connection,
                actor=actor,
                action=SEASON_LIFECYCLE_CHANGED,
                entity_type="season",
                entity_id=season_id,
                entity_version=str(version),
                reason=reason,
                before_state={"lifecycle_state": old_state},
                after_state={"lifecycle_state": target},
            )

            # Construct the command result from the locked row and exact values
            # written above.  An unlocked post-commit read could observe a later
            # transition by another PostgreSQL transaction.
            result = Season(
                season_id=season_id,
                year=row["year"],
                label=row["label"],
                lifecycle_state=target,
                created_at=row["created_at"],
                updated_at=updated_at,
                version=version,
                regular_season_round_count=(
                    row["regular_season_round_count"] if "regular_season_round_count" in row.keys() else 20
                ),
                midseason_draft_trigger_round=(
                    row["midseason_draft_trigger_round"] if "midseason_draft_trigger_round" in row.keys() else None
                ),
            )
        return result

    def create_rules_version(
        self,
        season_id: str,
        rules_key: str,
        version_number: int,
        name: str,
        *,
        notes: str | None = None,
        scoring_rules: dict | None = None,
        actor: ActorContext = ActorContext.anonymous_operator("admin"),
    ) -> RulesVersion:
        item = RulesVersion(
            _id(),
            season_id,
            rules_key,
            version_number,
            name,
            notes,
            _now(),
            actor.actor_id,
            scoring_rules,
        )
        with transaction(self.database) as connection:
            columns = {column["name"] for column in inspect(self.database.engine).get_columns("season_rules_version")}
            if "scoring_rules" in columns:
                connection.execute(
                    "INSERT INTO season_rules_version (rules_version_id, season_id, rules_key, version_number, name, notes, created_at, created_by, scoring_rules) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        *tuple(item.__dict__.values())[:-1],
                        json.dumps(scoring_rules, sort_keys=True) if scoring_rules is not None else None,
                    ),
                )
            else:
                connection.execute(
                    "INSERT INTO season_rules_version VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(item.__dict__.values())[:-1],
                )
            append_event(
                connection,
                actor=actor,
                action=RULES_VERSION_CREATED,
                entity_type="season.rules_version",
                entity_id=item.rules_version_id,
                entity_version=str(version_number),
                after_state={
                    "season_id": season_id,
                    "rules_key": rules_key,
                    "version_number": version_number,
                },
            )
        return item

    def get_rules_version(self, rules_version_id: str) -> RulesVersion | None:
        """Single-row lookup by id -- issue #151's human-readable rules
        metadata for Scorer/Admin surfaces (`app.round_review`'s
        `MatchupReview.rules_version_label`) that otherwise only carry a
        `rules_version_id`. Returns `None` for an unknown/removed id rather
        than raising, matching `IdentityRepository.get_public_team`'s
        display-only "may be absent" convention."""
        row = self.database.execute(
            "SELECT * FROM season_rules_version WHERE rules_version_id=?",
            (rules_version_id,),
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        values["scoring_rules"] = json.loads(values["scoring_rules"]) if values.get("scoring_rules") else None
        return RulesVersion(**values)

    def list_rules_versions(self, season_id: str) -> list[RulesVersion]:
        rows = self.database.execute(
            "SELECT * FROM season_rules_version WHERE season_id=? ORDER BY rules_key, version_number",
            (season_id,),
        ).fetchall()
        result = []
        for row in rows:
            values = dict(row)
            values["scoring_rules"] = json.loads(values["scoring_rules"]) if values.get("scoring_rules") else None
            result.append(RulesVersion(**values))
        return result

    def create_competition(
        self,
        season_id: str,
        rules_version_id: str,
        stream_key: str,
        label: str,
        stream_type: str,
    ) -> CompetitionStream:
        rules = self.database.execute(
            "SELECT season_id FROM season_rules_version WHERE rules_version_id=?",
            (rules_version_id,),
        ).fetchone()
        if not rules or rules["season_id"] != season_id:
            raise ValueError("rules version must belong to competition season")

        item = CompetitionStream(_id(), season_id, rules_version_id, stream_key, label, stream_type, _now())
        with transaction(self.database) as connection:
            connection.execute(
                "INSERT INTO competition_stream VALUES (?, ?, ?, ?, ?, ?, ?)",
                tuple(item.__dict__.values()),
            )
        return item

    def list_competitions(self, season_id: str) -> list[CompetitionStream]:
        rows = self.database.execute(
            "SELECT * FROM competition_stream WHERE season_id=? ORDER BY stream_key",
            (season_id,),
        ).fetchall()
        return [CompetitionStream(**dict(row)) for row in rows]

    def create_round(self, competition_id: str, round_key: str, label: str, sequence: int) -> BBBFFLRound:
        item = BBBFFLRound(_id(), competition_id, round_key, label, sequence, _now())
        with transaction(self.database) as connection:
            connection.execute(
                "INSERT INTO bbbffl_round VALUES (?, ?, ?, ?, ?, ?)",
                tuple(item.__dict__.values()),
            )
        return item

    def list_rounds(self, competition_id: str) -> list[BBBFFLRound]:
        rows = self.database.execute(
            "SELECT * FROM bbbffl_round WHERE competition_id=? ORDER BY sequence",
            (competition_id,),
        ).fetchall()
        return [BBBFFLRound(**dict(row)) for row in rows]
