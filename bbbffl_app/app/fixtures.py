"""Persist the historical BBBFFL fixture draw; this is not a generic scheduler."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4, uuid5

from app.audit import ActorContext, append_event
from app.db import _for_update_suffix, transaction

ROTATION_VERSION = "bbbffl-workbook-2026-v1"
BASE_ROTATION = (
    ((1, 2), (3, 4), (5, 6), (7, 8), (9, 10)),
    ((1, 3), (2, 8), (9, 6), (7, 4), (10, 5)),
    ((1, 4), (2, 6), (3, 5), (8, 10), (7, 9)),
    ((1, 6), (2, 4), (5, 7), (3, 10), (8, 9)),
    ((1, 5), (2, 7), (4, 10), (3, 9), (6, 8)),
    ((1, 7), (2, 9), (4, 5), (6, 10), (3, 8)),
    ((1, 8), (2, 10), (4, 6), (3, 7), (5, 9)),
    ((1, 9), (2, 5), (4, 8), (3, 6), (7, 10)),
    ((1, 10), (2, 3), (4, 9), (5, 8), (6, 7)),
)


def fixture_number_rotation():
    """Return the exact persisted 20-round workbook pattern."""
    first = BASE_ROTATION
    reversed_half = tuple(tuple((away, home) for home, away in rnd) for rnd in first)
    return first + reversed_half + first[:2]


@dataclass(frozen=True)
class FixtureDraw:
    fixture_draw_id: str
    season_id: str
    rotation_version: str
    state: str
    version: int
    created_at: str
    updated_at: str
    frozen_at: str | None


@dataclass(frozen=True)
class FixtureMatchup:
    fixture_matchup_id: str
    fixture_draw_id: str
    season_id: str
    bbbffl_round_number: int
    matchup_order: int
    home_season_entry_id: str
    away_season_entry_id: str


def _now():
    return datetime.now(timezone.utc).isoformat()


class FixtureRepository:
    def __init__(self, database):
        self.database = database

    def save_draft(self, season_id, entries_by_fixture_number, *, actor=ActorContext.anonymous_operator("admin"), reason=None):
        entries = list(entries_by_fixture_number)
        if len(entries) != 10 or len(set(entries)) != 10:
            raise ValueError("fixture draw requires ten distinct season entries")
        with transaction(self.database) as conn:
            rows = conn.execute("SELECT season_entry_id FROM season_entry WHERE season_id=?", (season_id,)).fetchall()
            season_entries = {row["season_entry_id"] for row in rows}
            if len(season_entries) != 10 or set(entries) != season_entries:
                raise ValueError("fixture draw must assign all ten entries from exactly one season")
            existing = conn.execute("SELECT * FROM season_fixture_draw WHERE season_id=?" + _for_update_suffix(self.database), (season_id,)).fetchone()
            if existing and existing["state"] == "frozen":
                raise ValueError("frozen fixture draw is immutable")
            now = _now()
            if existing:
                draw_id = existing["fixture_draw_id"]
                before = self._number_map(conn, draw_id)
                version = existing["version"] + 1
                conn.execute("DELETE FROM season_fixture_matchup WHERE fixture_draw_id=?", (draw_id,))
                conn.execute("DELETE FROM season_fixture_number WHERE fixture_draw_id=?", (draw_id,))
                conn.execute("UPDATE season_fixture_draw SET version=?, updated_at=? WHERE fixture_draw_id=?", (version, now, draw_id))
                action = "fixture.draw.corrected"
            else:
                draw_id = str(uuid4())
                before = None
                version = 1
                conn.execute("INSERT INTO season_fixture_draw VALUES (?, ?, ?, 'draft', ?, ?, ?, NULL)", (draw_id, season_id, ROTATION_VERSION, version, now, now))
                action = "fixture.draw.created"
            for number, entry_id in enumerate(entries, 1):
                conn.execute("INSERT INTO season_fixture_number VALUES (?, ?, ?, ?)", (draw_id, season_id, number, entry_id))
            for round_number, pairings in enumerate(fixture_number_rotation(), 1):
                for order, (home, away) in enumerate(pairings, 1):
                    matchup_id = str(uuid5(UUID(draw_id), f"round:{round_number}:match:{order}"))
                    conn.execute("INSERT INTO season_fixture_matchup VALUES (?, ?, ?, ?, ?, ?, ?)", (matchup_id, draw_id, season_id, round_number, order, entries[home - 1], entries[away - 1]))
            after = {str(i): entry for i, entry in enumerate(entries, 1)}
            append_event(conn, actor=actor, action=action, entity_type="fixture_draw", entity_id=draw_id, entity_version=str(version), reason=reason, before_state=before, after_state=after, payload={"rotation_version": ROTATION_VERSION})
        return self.get_draw(season_id)

    @staticmethod
    def _number_map(conn, draw_id):
        rows = conn.execute("SELECT fixture_number, season_entry_id FROM season_fixture_number WHERE fixture_draw_id=? ORDER BY fixture_number", (draw_id,)).fetchall()
        return {str(row["fixture_number"]): row["season_entry_id"] for row in rows}

    def freeze(self, season_id, *, actor=ActorContext.anonymous_operator("admin"), reason=None):
        with transaction(self.database) as conn:
            row = conn.execute("SELECT * FROM season_fixture_draw WHERE season_id=?" + _for_update_suffix(self.database), (season_id,)).fetchone()
            if not row:
                raise KeyError(season_id)
            if row["state"] == "frozen":
                raise ValueError("fixture draw is already frozen")
            counts = conn.execute("SELECT (SELECT COUNT(*) FROM season_fixture_number WHERE fixture_draw_id=?) assignments, (SELECT COUNT(*) FROM season_fixture_matchup WHERE fixture_draw_id=?) matchups", (row["fixture_draw_id"], row["fixture_draw_id"])).fetchone()
            if counts["assignments"] != 10 or counts["matchups"] != 100:
                raise ValueError("fixture draw is incomplete")
            now, version = _now(), row["version"] + 1
            conn.execute("UPDATE season_fixture_draw SET state='frozen', version=?, updated_at=?, frozen_at=? WHERE fixture_draw_id=?", (version, now, now, row["fixture_draw_id"]))
            append_event(conn, actor=actor, action="fixture.draw.frozen", entity_type="fixture_draw", entity_id=row["fixture_draw_id"], entity_version=str(version), reason=reason, before_state={"state": "draft"}, after_state={"state": "frozen"})
        return self.get_draw(season_id)

    def get_draw(self, season_id):
        row = self.database.execute("SELECT * FROM season_fixture_draw WHERE season_id=?", (season_id,)).fetchone()
        return FixtureDraw(**dict(row)) if row else None

    def fixture_numbers(self, season_id):
        row = self.get_draw(season_id)
        return self._number_map(self.database, row.fixture_draw_id) if row else {}

    def list_matchups(self, season_id, round_number=None):
        sql = "SELECT m.* FROM season_fixture_matchup m JOIN season_fixture_draw d ON d.fixture_draw_id=m.fixture_draw_id WHERE d.season_id=?"
        params = [season_id]
        if round_number is not None:
            sql += " AND m.bbbffl_round_number=?"
            params.append(round_number)
        sql += " ORDER BY m.bbbffl_round_number, m.matchup_order"
        return [FixtureMatchup(**dict(row)) for row in self.database.execute(sql, tuple(params)).fetchall()]
