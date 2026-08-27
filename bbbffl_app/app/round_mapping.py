"""Season/stream-scoped, versioned BBBFFL-to-AFL round mapping boundary."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.audit import ActorContext, ConnectionLike, append_event
from app.db import DatabaseConnection, _for_update_suffix, transaction
from app.season import _id, _now

MAPPING_REVISED = "round_mapping.revised"
MAPPING_ACCEPTED = "round_mapping.accepted"
MAPPING_CORRECTED = "round_mapping.corrected"


class AflReferenceValidator(Protocol):
    def round_exists(self, season_id: int, round_id: int) -> bool: ...


class AflApiReferenceValidator:
    """Validate through afl-api's public versioned season-round listing."""

    def __init__(self, client: Any):
        self.client = client

    def round_exists(self, season_id: int, round_id: int) -> bool:
        return any(item.round_id == round_id for item in self.client.get_rounds(season_id))


@dataclass(frozen=True)
class RoundMapping:
    mapping_id: str
    bbbffl_round_id: str
    revision: int
    state: str
    provider: str
    afl_season_id: int | None
    afl_round_id: int | None
    created_at: str
    created_by: str | None
    reason: str | None


def _mapping(row: Mapping[str, Any]) -> RoundMapping:
    return RoundMapping(
        row["mapping_id"],
        row["bbbffl_round_id"],
        row["revision"],
        row["state"],
        row["provider"],
        row["afl_season_id"],
        row["afl_round_id"],
        row["created_at"],
        row["created_by"],
        row["reason"],
    )


class RoundMappingRepository:
    def __init__(self, database: DatabaseConnection):
        self.database = database

    def propose(
        self,
        bbbffl_round_id: str,
        *,
        state: str = "unresolved",
        afl_season_id: int | None = None,
        afl_round_id: int | None = None,
        provider: str = "afl-api-v1",
        actor: ActorContext = ActorContext.anonymous_operator("admin"),
        reason: str | None = None,
    ) -> RoundMapping:
        if state not in {"unresolved", "ambiguous"}:
            raise ValueError("a proposal must be unresolved or ambiguous; use accept() to activate")
        if (afl_season_id is None) != (afl_round_id is None):
            raise ValueError("AFL season and round identities must be supplied together")
        with transaction(self.database) as conn:
            head = conn.execute(
                "SELECT * FROM round_afl_mapping WHERE bbbffl_round_id=?" + _for_update_suffix(self.database),
                (bbbffl_round_id,),
            ).fetchone()
            if head:
                current = self._current(conn, head["mapping_id"])
                if current["state"] == "accepted":
                    raise ValueError("accepted mapping requires authorised correction")
                mapping_id, revision = head["mapping_id"], head["current_revision"] + 1
                before = self._state(current)
                conn.execute(
                    "UPDATE round_afl_mapping SET current_revision=? WHERE mapping_id=?", (revision, mapping_id)
                )
            else:
                mapping_id, revision = _id(), 1
                before = None
                conn.execute(
                    "INSERT INTO round_afl_mapping VALUES (?, ?, ?, ?)", (mapping_id, bbbffl_round_id, revision, _now())
                )
            self._insert_revision(
                conn, mapping_id, revision, state, provider, afl_season_id, afl_round_id, actor, reason
            )
            append_event(
                conn,
                actor=actor,
                action=MAPPING_REVISED,
                entity_type="round.afl_mapping",
                entity_id=mapping_id,
                entity_version=str(revision),
                before_state=before,
                after_state={"state": state, "afl_season_id": afl_season_id, "afl_round_id": afl_round_id},
                reason=reason,
            )
            return self._get(conn, mapping_id)

    def accept(
        self,
        bbbffl_round_id: str,
        afl_season_id: int,
        afl_round_id: int,
        validator: AflReferenceValidator,
        *,
        provider: str = "afl-api-v1",
        actor: ActorContext = ActorContext.anonymous_operator("admin"),
        reason: str | None = None,
    ) -> RoundMapping:
        if not validator.round_exists(afl_season_id, afl_round_id):
            raise ValueError("AFL season/round reference does not exist")
        return self._activate(bbbffl_round_id, afl_season_id, afl_round_id, provider, actor, reason, False)

    def correct(
        self,
        bbbffl_round_id: str,
        afl_season_id: int,
        afl_round_id: int,
        validator: AflReferenceValidator,
        *,
        reason: str,
        actor: ActorContext = ActorContext.anonymous_operator("admin"),
        provider: str = "afl-api-v1",
    ) -> RoundMapping:
        if not reason:
            raise ValueError("an authorised correction requires a reason")
        if not validator.round_exists(afl_season_id, afl_round_id):
            raise ValueError("AFL season/round reference does not exist")
        return self._activate(bbbffl_round_id, afl_season_id, afl_round_id, provider, actor, reason, True)

    def resolve(self, bbbffl_round_id: str) -> RoundMapping | None:
        row = self.database.execute(
            self._select() + " WHERE m.bbbffl_round_id=? AND r.state='accepted'", (bbbffl_round_id,)
        ).fetchone()
        return _mapping(row) if row else None

    def history(self, bbbffl_round_id: str) -> list[RoundMapping]:
        rows = self.database.execute(
            self._select(False) + " WHERE m.bbbffl_round_id=? ORDER BY r.revision", (bbbffl_round_id,)
        ).fetchall()
        return [_mapping(row) for row in rows]

    def _activate(
        self,
        round_id: str,
        season: int,
        afl_round: int,
        provider: str,
        actor: ActorContext,
        reason: str | None,
        correction: bool,
    ) -> RoundMapping:
        with transaction(self.database) as conn:
            head = conn.execute(
                "SELECT * FROM round_afl_mapping WHERE bbbffl_round_id=?" + _for_update_suffix(self.database),
                (round_id,),
            ).fetchone()
            if not head:
                if correction:
                    raise ValueError("correction requires an accepted mapping")
                mapping_id, revision, before = _id(), 1, None
                conn.execute(
                    "INSERT INTO round_afl_mapping VALUES (?, ?, ?, ?)", (mapping_id, round_id, revision, _now())
                )
            else:
                mapping_id = head["mapping_id"]
                old = self._current(conn, mapping_id)
                if correction != (old["state"] == "accepted"):
                    raise ValueError(
                        "use correction for an accepted mapping"
                        if old["state"] == "accepted"
                        else "correction requires an accepted mapping"
                    )
                revision = head["current_revision"] + 1
                before = {
                    "state": old["state"],
                    "afl_season_id": old["afl_season_id"],
                    "afl_round_id": old["afl_round_id"],
                }
                conn.execute(
                    "UPDATE round_afl_mapping SET current_revision=? WHERE mapping_id=?", (revision, mapping_id)
                )
            self._insert_revision(conn, mapping_id, revision, "accepted", provider, season, afl_round, actor, reason)
            after = {"state": "accepted", "afl_season_id": season, "afl_round_id": afl_round}
            append_event(
                conn,
                actor=actor,
                action=MAPPING_CORRECTED if correction else MAPPING_ACCEPTED,
                entity_type="round.afl_mapping",
                entity_id=mapping_id,
                entity_version=str(revision),
                before_state=before,
                after_state=after,
                reason=reason,
            )
            return self._get(conn, mapping_id)

    @staticmethod
    def _insert_revision(
        conn: ConnectionLike,
        mapping_id: str,
        revision: int,
        state: str,
        provider: str,
        season: int | None,
        afl_round: int | None,
        actor: ActorContext,
        reason: str | None,
    ) -> None:
        conn.execute(
            "INSERT INTO round_afl_mapping_revision VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mapping_id, revision, state, provider, season, afl_round, _now(), actor.actor_id, reason),
        )

    @staticmethod
    def _current(conn: ConnectionLike, mapping_id: str) -> Mapping[str, Any]:
        return conn.execute(
            "SELECT r.* FROM round_afl_mapping m JOIN round_afl_mapping_revision r ON r.mapping_id=m.mapping_id AND r.revision=m.current_revision WHERE m.mapping_id=?",
            (mapping_id,),
        ).fetchone()

    @staticmethod
    def _state(row: Mapping[str, Any]) -> dict[str, Any]:
        return {"state": row["state"], "afl_season_id": row["afl_season_id"], "afl_round_id": row["afl_round_id"]}

    @staticmethod
    def _select(current: bool = True) -> str:
        join = " AND r.revision=m.current_revision" if current else ""
        return (
            "SELECT m.bbbffl_round_id, r.* FROM round_afl_mapping m JOIN round_afl_mapping_revision r ON r.mapping_id=m.mapping_id"
            + join
        )

    def _get(self, conn: ConnectionLike, mapping_id: str) -> RoundMapping:
        return _mapping(conn.execute(self._select() + " WHERE m.mapping_id=?", (mapping_id,)).fetchone())
