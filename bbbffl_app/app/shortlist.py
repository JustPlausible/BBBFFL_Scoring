"""Private coach draft shortlist/planning (issue #181).

A coach's own ordered list of preferred players, usable before and during
either the preseason or the mid-season draft. This module is deliberately
narrow:

- it never reserves a player -- adding someone to a shortlist changes
  nothing about `player_ownership_period`;
- it never validates or executes a selection -- `app.draft.DraftRepository`
  and `app.midseason_draft.MidseasonDraftRepository` remain the sole
  authority for turn/eligibility/ownership/capacity, exactly as they are
  for a manually-searched pick;
- it is private per `season_entry_id` -- every route that reads or writes
  through this repository must itself authorise the caller (coach owns the
  entry, or an authorised Scorer/Admin acting in that entry's represented
  context) before ever calling in here. This module trusts its caller for
  that; it is not itself an authorization boundary.

`suggestion` is the one piece of "intelligence" this module offers: the
highest-ranked shortlist entry that is *currently* unowned, recomputed
fresh from `player_ownership_period` on every call -- never cached, so a
player who becomes unavailable a moment after being suggested is simply
absent from the very next read. This is display-only: nothing here (or
anywhere else in v0.1) ever acts on a suggestion automatically. See the
module's docstring in the issue for why timed auto-pick is deliberately
out of scope here -- `rank` is a plain dense ordering specifically so a
future auto-pick feature can walk it safely without redesigning this
model.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from app.audit import ActorContext, append_event
from app.db import _for_update_suffix, transaction


def _id():
    return str(uuid4())


def _now():
    return datetime.now(timezone.utc).isoformat()


class ShortlistError(ValueError):
    pass


@dataclass(frozen=True)
class ShortlistItem:
    shortlist_item_id: str
    season_entry_id: str
    season_player_id: str
    rank: int
    created_at: str
    updated_at: str


class ShortlistRepository:
    def __init__(self, database):
        self.database = database

    def list_items(self, season_entry_id) -> list[ShortlistItem]:
        rows = self.database.execute(
            "SELECT * FROM coach_draft_shortlist WHERE season_entry_id=? ORDER BY rank", (season_entry_id,)
        ).fetchall()
        return [ShortlistItem(**dict(row)) for row in rows]

    def add_player(self, season_entry_id, season_player_id, *, actor: ActorContext, reason=None):
        with transaction(self.database) as conn:
            entry = conn.execute(
                "SELECT season_id FROM season_entry WHERE season_entry_id=?" + _for_update_suffix(self.database),
                (season_entry_id,),
            ).fetchone()
            if not entry:
                raise KeyError(season_entry_id)
            player = conn.execute(
                "SELECT season_id FROM season_player_pool WHERE season_player_id=?", (season_player_id,)
            ).fetchone()
            if not player or player["season_id"] != entry["season_id"]:
                raise ShortlistError("player must belong to the same season as this team")
            existing = conn.execute(
                "SELECT 1 FROM coach_draft_shortlist WHERE season_entry_id=? AND season_player_id=?",
                (season_entry_id, season_player_id),
            ).fetchone()
            if existing:
                raise ShortlistError("player is already on this shortlist")
            next_rank = conn.execute(
                "SELECT COALESCE(MAX(rank), 0) + 1 AS next_rank FROM coach_draft_shortlist "
                "WHERE season_entry_id=?" + _for_update_suffix(self.database),
                (season_entry_id,),
            ).fetchone()["next_rank"]
            now = _now()
            item_id = _id()
            conn.execute(
                "INSERT INTO coach_draft_shortlist VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, season_entry_id, season_player_id, next_rank, now, now),
            )
            append_event(
                conn,
                actor=actor,
                action="shortlist.player.added",
                entity_type="coach_shortlist",
                entity_id=season_entry_id,
                reason=reason,
                after_state={"season_player_id": season_player_id, "rank": next_rank},
            )
        return self.list_items(season_entry_id)

    def remove_player(self, season_entry_id, season_player_id, *, actor: ActorContext, reason=None):
        with transaction(self.database) as conn:
            rows = conn.execute(
                "SELECT * FROM coach_draft_shortlist WHERE season_entry_id=? ORDER BY rank"
                + _for_update_suffix(self.database),
                (season_entry_id,),
            ).fetchall()
            target = next((row for row in rows if row["season_player_id"] == season_player_id), None)
            if not target:
                raise KeyError(season_player_id)
            conn.execute("DELETE FROM coach_draft_shortlist WHERE shortlist_item_id=?", (target["shortlist_item_id"],))
            remaining = [row for row in rows if row["shortlist_item_id"] != target["shortlist_item_id"]]
            self._renumber(conn, season_entry_id, [row["shortlist_item_id"] for row in remaining])
            append_event(
                conn,
                actor=actor,
                action="shortlist.player.removed",
                entity_type="coach_shortlist",
                entity_id=season_entry_id,
                reason=reason,
                before_state={"season_player_id": season_player_id, "rank": target["rank"]},
            )
        return self.list_items(season_entry_id)

    def reorder(self, season_entry_id, ordered_season_player_ids, *, actor: ActorContext, reason=None):
        """`ordered_season_player_ids` must name exactly the shortlist's
        current members, in the desired new order -- a partial or foreign
        list is refused rather than silently dropping/ignoring entries."""
        ordered_season_player_ids = list(ordered_season_player_ids)
        with transaction(self.database) as conn:
            rows = conn.execute(
                "SELECT * FROM coach_draft_shortlist WHERE season_entry_id=?" + _for_update_suffix(self.database),
                (season_entry_id,),
            ).fetchall()
            by_player = {row["season_player_id"]: row for row in rows}
            if len(ordered_season_player_ids) != len(rows) or set(ordered_season_player_ids) != set(by_player):
                raise ShortlistError("reorder must include exactly the existing shortlist entries, once each")
            before = {row["season_player_id"]: row["rank"] for row in rows}
            item_ids_in_order = [by_player[player_id]["shortlist_item_id"] for player_id in ordered_season_player_ids]
            self._renumber(conn, season_entry_id, item_ids_in_order)
            append_event(
                conn,
                actor=actor,
                action="shortlist.reordered",
                entity_type="coach_shortlist",
                entity_id=season_entry_id,
                reason=reason,
                before_state={"order": [pid for pid, _ in sorted(before.items(), key=lambda kv: kv[1])]},
                after_state={"order": ordered_season_player_ids},
            )
        return self.list_items(season_entry_id)

    def _renumber(self, conn, season_entry_id, item_ids_in_order):
        """Reassign dense 1..N ranks in `item_ids_in_order`'s order. Two
        passes -- a large positive offset, then the final 1..N value -- so
        no intermediate UPDATE ever collides with `uq_shortlist_entry_rank`
        against a row this same call has not reached yet, while never
        violating `ck_shortlist_rank_positive` along the way."""
        now = _now()
        offset = len(item_ids_in_order) + 1_000_000
        for index, item_id in enumerate(item_ids_in_order, start=1):
            conn.execute("UPDATE coach_draft_shortlist SET rank=? WHERE shortlist_item_id=?", (offset + index, item_id))
        for index, item_id in enumerate(item_ids_in_order, start=1):
            conn.execute(
                "UPDATE coach_draft_shortlist SET rank=?, updated_at=? WHERE shortlist_item_id=?",
                (index, now, item_id),
            )

    def suggestion(self, season_entry_id) -> ShortlistItem | None:
        """The highest-ranked shortlist entry with no currently open
        ownership period -- recomputed fresh on every call, never cached.
        Display-only: never itself consulted by a draft repository."""
        for item in self.list_items(season_entry_id):
            owned = self.database.execute(
                "SELECT 1 FROM player_ownership_period WHERE season_player_id=? AND released_at IS NULL",
                (item.season_player_id,),
            ).fetchone()
            if not owned:
                return item
        return None
