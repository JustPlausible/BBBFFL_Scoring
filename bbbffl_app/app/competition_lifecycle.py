"""Persisted competition lifecycle built on frozen fixtures and mappings.

Upstream statuses are observations only.  They are deliberately never consulted
by lifecycle transitions, and calculated snapshots never share storage with
versioned official results.

## Ordinary vs. finals/superscore rounds (issue #197)

`create_ordinary_round` is completely unchanged and remains the only way an
`ordinary`-streamed round gets a `bbbffl_round_lifecycle` row: it always
populates `fixture_draw_id`/`fixture_draw_version`/`fixture_round_number`
from a frozen `season_fixture_draw` and always creates exactly five
`bbbffl_matchup` rows from `season_fixture_matchup`. `create_non_ordinary_
round` is the schema-level counterpart for both `finals`/`superscore`
streams, which have no pre-drawn fixture to snapshot -- it persists a
lifecycle row with a null fixture context instead. `create_stream_matchup`
is `finals`-only (SuperScore is matchup-free by design; see its own
docstring), and persists a matchup row with no `fixture_matchup_id`,
since a finals pairing is derived from results, never a pre-drawn
fixture. `_validate_frozen_context` is the one place that treats these two
shapes differently (see its own docstring); every other method in this
module, and every other module that reads `bbbffl_round_lifecycle`/
`bbbffl_matchup` by `bbbffl_round_id`/`matchup_id` (`app.lineups`, `app.
lineup_adjudication`, `app.calculations`, `app.round_review`), works
identically for either shape without any code change -- see
docs/2026-finals-superscore-design.md's "A foundational schema fork" and
its issue #197 resolution note for the full reasoning and call-site audit.
This module still creates no finals bracket, no SuperScore round, and
applies no SuperScore scoring rule -- that remains #190/#192's job, built
on top of these two primitives.
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4, uuid5

from app.audit import ActorContext, append_event, new_correlation_id
from app.db import _for_update_suffix, transaction
from app.season import _now


class StaleRoundVersionError(RuntimeError):
    """A sign-off/correction attempt named a round or matchup review revision
    that is no longer current -- see `publish_results`/`correct_matchup_result`
    and `app.round_review` (issue #58 requirement 7). The caller must reload
    the round review and retry with the current revision; this is never
    silently resolved by overwriting the newer decision."""


LEGAL_TRANSITIONS = {
    "upcoming": {"open"},
    "open": {"live"},
    "live": {"review"},
    "review": set(),  # final is entered only by atomic result publication
    "final": set(),  # corrections version results; they never reopen history
}


@dataclass(frozen=True)
class CompetitionRound:
    bbbffl_round_id: str
    competition_id: str
    season_id: str
    # `None` for a non-ordinary (finals/superscore) round -- issue #197.
    # Always populated together for an ordinary round; see
    # `ck_lifecycle_fixture_context_all_or_none` (migrations/versions/
    # 0029_stream_lifecycle.py) and `create_non_ordinary_round`.
    fixture_draw_id: str | None
    fixture_draw_version: int | None
    fixture_round_number: int | None
    mapping_id: str
    mapping_revision: int
    provider: str
    afl_season_id: int
    afl_round_id: int
    state: str
    version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Matchup:
    matchup_id: str
    bbbffl_round_id: str
    # `None` for a finals matchup created via `create_stream_matchup` (issue
    # #197) -- finals pairings are derived from results, not a pre-drawn
    # fixture. Always populated for an ordinary matchup (`create_ordinary_round`).
    fixture_matchup_id: str | None
    matchup_order: int
    home_season_entry_id: str
    away_season_entry_id: str
    effective_official_version: int | None
    review_version: int


@dataclass(frozen=True)
class OfficialResult:
    matchup_id: str
    version: int
    home_score: Decimal
    away_score: Decimal
    published_at: str
    published_by: str | None
    reason: str | None
    # Frozen scoring inputs (rules version, lineup versions, calculated-result
    # revision, DNP/interchange rulings, overrides) this version was computed
    # from -- see app.round_review, roadmap package 28 / issue #58. None for
    # any result published before this existed, or by a caller that does not
    # supply one; never backfilled, since a pre-existing version's meaning
    # must not change after the fact.
    input_snapshot: dict | None = None


def _row_to_official_result(row) -> OfficialResult:
    values = dict(row)
    snapshot = values.pop("input_snapshot", None)
    return OfficialResult(**values, input_snapshot=json.loads(snapshot) if snapshot else None)


class CompetitionLifecycleRepository:
    """Command/query boundary for ordinary rounds; no scoring rules live here."""

    def __init__(self, database):
        self.database = database

    def create_ordinary_round(
        self,
        bbbffl_round_id,
        *,
        actor=ActorContext.anonymous_operator("admin"),
        reason=None,
    ):
        """Snapshot exactly one accepted mapping and five frozen fixture pairs."""
        with transaction(self.database) as conn:
            logical = conn.execute(
                "SELECT r.*, c.season_id, c.stream_type FROM bbbffl_round r "
                "JOIN competition_stream c ON c.competition_id=r.competition_id "
                "WHERE r.bbbffl_round_id=?" + _for_update_suffix(self.database),
                (bbbffl_round_id,),
            ).fetchone()
            if not logical:
                raise KeyError(bbbffl_round_id)
            if logical["stream_type"] != "ordinary":
                raise ValueError("persisted ordinary lifecycle requires an ordinary competition stream")
            if conn.execute(
                "SELECT 1 FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?",
                (bbbffl_round_id,),
            ).fetchone():
                raise ValueError("round lifecycle already exists")
            draw = conn.execute(
                "SELECT * FROM season_fixture_draw WHERE season_id=?" + _for_update_suffix(self.database),
                (logical["season_id"],),
            ).fetchone()
            if not draw or draw["state"] != "frozen":
                raise ValueError("round requires a valid frozen fixture context")
            pairs = conn.execute(
                "SELECT * FROM season_fixture_matchup WHERE fixture_draw_id=? AND bbbffl_round_number=? ORDER BY matchup_order",
                (draw["fixture_draw_id"], logical["sequence"]),
            ).fetchall()
            entries = [
                entry
                for pair in pairs
                for entry in (
                    pair["home_season_entry_id"],
                    pair["away_season_entry_id"],
                )
            ]
            if (
                len(pairs) != 5
                or len(entries) != 10
                or len(set(entries)) != 10
                or [p["matchup_order"] for p in pairs] != list(range(1, 6))
            ):
                raise ValueError("ordinary round requires five non-overlapping fixture matchups")
            mapping = conn.execute(
                "SELECT m.mapping_id, m.current_revision, r.* FROM round_afl_mapping m "
                "JOIN round_afl_mapping_revision r ON r.mapping_id=m.mapping_id AND r.revision=m.current_revision "
                "WHERE m.bbbffl_round_id=?" + _for_update_suffix(self.database),
                (bbbffl_round_id,),
            ).fetchone()
            if (
                not mapping
                or mapping["state"] != "accepted"
                or mapping["afl_season_id"] is None
                or mapping["afl_round_id"] is None
            ):
                raise ValueError("round requires one accepted, unambiguous AFL mapping")
            now = _now()
            conn.execute(
                "INSERT INTO bbbffl_round_lifecycle VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'upcoming', 1, ?, ?)",
                (
                    bbbffl_round_id,
                    logical["competition_id"],
                    logical["season_id"],
                    draw["fixture_draw_id"],
                    draw["version"],
                    logical["sequence"],
                    mapping["mapping_id"],
                    mapping["revision"],
                    mapping["provider"],
                    mapping["afl_season_id"],
                    mapping["afl_round_id"],
                    now,
                    now,
                ),
            )
            for pair in pairs:
                matchup_id = str(uuid5(UUID(bbbffl_round_id), f"fixture:{pair['fixture_matchup_id']}"))
                conn.execute(
                    "INSERT INTO bbbffl_matchup VALUES (?, ?, ?, ?, ?, ?, NULL, 1)",
                    (
                        matchup_id,
                        bbbffl_round_id,
                        pair["fixture_matchup_id"],
                        pair["matchup_order"],
                        pair["home_season_entry_id"],
                        pair["away_season_entry_id"],
                    ),
                )
            append_event(
                conn,
                actor=actor,
                action="competition.round.created",
                entity_type="competition.round",
                entity_id=bbbffl_round_id,
                entity_version="1",
                reason=reason,
                after_state={"state": "upcoming"},
                payload={
                    "fixture_draw_id": draw["fixture_draw_id"],
                    "mapping_id": mapping["mapping_id"],
                    "mapping_revision": mapping["revision"],
                },
            )
        return self.get_round(bbbffl_round_id)

    def create_non_ordinary_round(
        self,
        bbbffl_round_id,
        *,
        actor=ActorContext.anonymous_operator("admin"),
        reason=None,
    ):
        """Give a finals/superscore-typed round a `bbbffl_round_lifecycle`
        row with no frozen fixture-draw context (issue #197) -- the
        schema-level counterpart to `create_ordinary_round` for the two
        stream types that have no pre-drawn fixture to snapshot (finals
        pairings are derived from results; SuperScore has no pairing at
        all). This is deliberately the *only* thing this method does: it
        creates no matchup (see `create_stream_matchup`), computes no
        bracket/pairing and applies no SuperScore scoring rule -- those are
        #190/#192's job, built on top of this primitive.

        An accepted, unambiguous AFL mapping is still required, exactly as
        for an ordinary round (docs/2026-finals-superscore-design.md's
        "the mapping-revision validation ... stays meaningful regardless of
        stream type") -- only the fixture-draw linkage is skipped.
        `fixture_draw_id`/`fixture_draw_version`/`fixture_round_number` are
        persisted as `NULL` together (`ck_lifecycle_fixture_context_all_or_
        none`), so `_validate_frozen_context` below can later tell this
        round apart from an ordinary one purely from its own row, without
        consulting `competition_stream.stream_type` again."""
        with transaction(self.database) as conn:
            logical = conn.execute(
                "SELECT r.*, c.season_id, c.stream_type FROM bbbffl_round r "
                "JOIN competition_stream c ON c.competition_id=r.competition_id "
                "WHERE r.bbbffl_round_id=?" + _for_update_suffix(self.database),
                (bbbffl_round_id,),
            ).fetchone()
            if not logical:
                raise KeyError(bbbffl_round_id)
            if logical["stream_type"] == "ordinary":
                raise ValueError("create_non_ordinary_round requires a non-ordinary competition stream")
            if conn.execute(
                "SELECT 1 FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?",
                (bbbffl_round_id,),
            ).fetchone():
                raise ValueError("round lifecycle already exists")
            mapping = conn.execute(
                "SELECT m.mapping_id, m.current_revision, r.* FROM round_afl_mapping m "
                "JOIN round_afl_mapping_revision r ON r.mapping_id=m.mapping_id AND r.revision=m.current_revision "
                "WHERE m.bbbffl_round_id=?" + _for_update_suffix(self.database),
                (bbbffl_round_id,),
            ).fetchone()
            if (
                not mapping
                or mapping["state"] != "accepted"
                or mapping["afl_season_id"] is None
                or mapping["afl_round_id"] is None
            ):
                raise ValueError("round requires one accepted, unambiguous AFL mapping")
            now = _now()
            conn.execute(
                "INSERT INTO bbbffl_round_lifecycle VALUES "
                "(?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, 'upcoming', 1, ?, ?)",
                (
                    bbbffl_round_id,
                    logical["competition_id"],
                    logical["season_id"],
                    mapping["mapping_id"],
                    mapping["revision"],
                    mapping["provider"],
                    mapping["afl_season_id"],
                    mapping["afl_round_id"],
                    now,
                    now,
                ),
            )
            append_event(
                conn,
                actor=actor,
                action="competition.round.created",
                entity_type="competition.round",
                entity_id=bbbffl_round_id,
                entity_version="1",
                reason=reason,
                after_state={"state": "upcoming"},
                payload={
                    "stream_type": logical["stream_type"],
                    "mapping_id": mapping["mapping_id"],
                    "mapping_revision": mapping["revision"],
                },
            )
        return self.get_round(bbbffl_round_id)

    def create_stream_matchup(
        self,
        bbbffl_round_id,
        matchup_order,
        home_season_entry_id,
        away_season_entry_id,
        *,
        actor=ActorContext.anonymous_operator("admin"),
        reason=None,
    ):
        """Persist one finals matchup with no `fixture_matchup_id` (issue
        #197): the schema-level primitive #190's finals bracket module
        pairs against, once it has actually computed which two entries meet
        in a given week -- this method itself makes no pairing/bracket
        decision. Only usable for a round already in a `finals`-typed
        stream's lifecycle (`create_non_ordinary_round`); `superscore` is
        deliberately excluded (per docs/2026-finals-superscore-design.md,
        SuperScore is matchup-free by design -- a matchup created for one
        would silently be processed by the generic matchup-keyed
        calculation/review queries as though it were a real head-to-head).
        An ordinary round's matchups are only ever created by `create_
        ordinary_round`'s fixture-derived path, so ordinary-round matchup
        integrity is never put at risk by this more permissive method."""
        if home_season_entry_id == away_season_entry_id:
            raise ValueError("a matchup requires two distinct entries")
        with transaction(self.database) as conn:
            round_row = conn.execute(
                "SELECT l.*, c.stream_type FROM bbbffl_round_lifecycle l "
                "JOIN competition_stream c ON c.competition_id=l.competition_id "
                "WHERE l.bbbffl_round_id=?" + _for_update_suffix(self.database),
                (bbbffl_round_id,),
            ).fetchone()
            if not round_row:
                raise KeyError(bbbffl_round_id)
            if round_row["stream_type"] != "finals":
                raise ValueError("create_stream_matchup requires a finals competition stream")
            entries = conn.execute(
                "SELECT season_entry_id FROM season_entry WHERE season_id=? AND season_entry_id IN (?, ?)",
                (round_row["season_id"], home_season_entry_id, away_season_entry_id),
            ).fetchall()
            if {row["season_entry_id"] for row in entries} != {home_season_entry_id, away_season_entry_id}:
                raise ValueError("both entries must belong to the round's own season")
            matchup_id = str(uuid4())
            conn.execute(
                "INSERT INTO bbbffl_matchup VALUES (?, ?, NULL, ?, ?, ?, NULL, 1)",
                (
                    matchup_id,
                    bbbffl_round_id,
                    matchup_order,
                    home_season_entry_id,
                    away_season_entry_id,
                ),
            )
            append_event(
                conn,
                actor=actor,
                action="competition.matchup.created",
                entity_type="competition.matchup",
                entity_id=matchup_id,
                entity_version="1",
                reason=reason,
                after_state={
                    "bbbffl_round_id": bbbffl_round_id,
                    "matchup_order": matchup_order,
                    "home_season_entry_id": home_season_entry_id,
                    "away_season_entry_id": away_season_entry_id,
                },
            )
        return self.get_matchup(matchup_id)

    def transition(
        self,
        round_id,
        target,
        *,
        actor=ActorContext.anonymous_operator("scorer"),
        reason=None,
    ):
        with transaction(self.database) as conn:
            row = self._locked_round(conn, round_id)
            if target not in LEGAL_TRANSITIONS[row["state"]]:
                raise ValueError(f"illegal lifecycle transition: {row['state']} -> {target}")
            if row["state"] == "upcoming":
                self._validate_frozen_context(conn, row)
            now, version = _now(), row["version"] + 1
            conn.execute(
                "UPDATE bbbffl_round_lifecycle SET state=?, version=?, updated_at=? WHERE bbbffl_round_id=?",
                (target, version, now, round_id),
            )
            append_event(
                conn,
                actor=actor,
                action="competition.round.transitioned",
                entity_type="competition.round",
                entity_id=round_id,
                entity_version=str(version),
                reason=reason,
                before_state={"state": row["state"]},
                after_state={"state": target},
            )
        return self.get_round(round_id)

    def publish_results(
        self,
        round_id,
        results,
        *,
        actor=ActorContext.anonymous_operator("scorer"),
        reason=None,
        failure_hook=None,
        input_snapshots=None,
        expected_round_version=None,
        expected_review_versions=None,
    ):
        """Publish all five version-1 results and final state in one transaction.

        `input_snapshots` (optional, `{matchup_id: dict}`) freezes the exact
        scoring inputs each result was computed from -- see
        `OfficialResult.input_snapshot`. `expected_round_version` and
        `expected_review_versions` (optional, `{matchup_id: int}`) are the
        compare-and-swap guards `app.round_review` uses so a sign-off based
        on a stale round/review revision fails closed instead of silently
        overwriting a decision made after it was read (issue #58
        requirement 7) -- both are re-checked against the row locked here,
        inside this same transaction, so the check and the write can never
        race. Omitting them (every pre-existing caller) keeps prior
        behaviour exactly.
        """
        with transaction(self.database) as conn:
            row = self._locked_round(conn, round_id)
            if expected_round_version is not None and row["version"] != expected_round_version:
                raise StaleRoundVersionError(
                    f"round {round_id} is at version {row['version']}, not the expected {expected_round_version}"
                )
            if row["state"] != "review":
                raise ValueError("only a review round can be finalised")
            matchups = self._locked_matchups(conn, round_id)
            self._validate_result_set(matchups, results)
            if expected_review_versions is not None:
                self._validate_review_versions(matchups, expected_review_versions)
            correlation = new_correlation_id()
            now = _now()
            for index, matchup in enumerate(matchups):
                home, away = results[matchup["matchup_id"]]
                snapshot = (
                    json.dumps(input_snapshots[matchup["matchup_id"]], sort_keys=True, default=str)
                    if input_snapshots and matchup["matchup_id"] in input_snapshots
                    else None
                )
                conn.execute(
                    "INSERT INTO bbbffl_official_result VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
                    (matchup["matchup_id"], home, away, now, actor.actor_id, reason, snapshot),
                )
                conn.execute(
                    "UPDATE bbbffl_matchup SET effective_official_version=1 WHERE matchup_id=?",
                    (matchup["matchup_id"],),
                )
                append_event(
                    conn,
                    actor=actor,
                    action="competition.result.published",
                    entity_type="competition.matchup",
                    entity_id=matchup["matchup_id"],
                    entity_version="1",
                    correlation_id=correlation,
                    reason=reason,
                    after_state={"official_version": 1},
                )
                if failure_hook:
                    failure_hook(index + 1)
            version = row["version"] + 1
            conn.execute(
                "UPDATE bbbffl_round_lifecycle SET state='final', version=?, updated_at=? WHERE bbbffl_round_id=?",
                (version, now, round_id),
            )
            append_event(
                conn,
                actor=actor,
                action="competition.round.finalized",
                entity_type="competition.round",
                entity_id=round_id,
                entity_version=str(version),
                correlation_id=correlation,
                reason=reason,
                before_state={"state": "review"},
                after_state={"state": "final"},
                payload={"matchup_count": 5},
            )
        return self.get_round(round_id)

    def correct_results(
        self,
        round_id,
        results,
        *,
        reason,
        actor=ActorContext.anonymous_operator("admin"),
        input_snapshots=None,
        expected_round_version=None,
        expected_review_versions=None,
    ):
        """Atomically append one new official version for every ordinary matchup.

        See `publish_results` for `input_snapshots`/`expected_round_version`/
        `expected_review_versions` -- the same freeze/compare-and-swap
        guards, reused here so a round-wide correction is exactly as
        stale-safe and reproducible as first publication."""
        if not reason:
            raise ValueError("an authorised post-final correction requires a reason")
        with transaction(self.database) as conn:
            row = self._locked_round(conn, round_id)
            if expected_round_version is not None and row["version"] != expected_round_version:
                raise StaleRoundVersionError(
                    f"round {round_id} is at version {row['version']}, not the expected {expected_round_version}"
                )
            if row["state"] != "final":
                raise ValueError("only final results can be corrected")
            matchups = self._locked_matchups(conn, round_id)
            self._validate_result_set(matchups, results)
            if expected_review_versions is not None:
                self._validate_review_versions(matchups, expected_review_versions)
            correlation, now = new_correlation_id(), _now()
            for matchup in matchups:
                old = matchup["effective_official_version"]
                if old is None:
                    raise ValueError("final round has incomplete official results")
                version = old + 1
                home, away = results[matchup["matchup_id"]]
                snapshot = (
                    json.dumps(input_snapshots[matchup["matchup_id"]], sort_keys=True, default=str)
                    if input_snapshots and matchup["matchup_id"] in input_snapshots
                    else None
                )
                conn.execute(
                    "INSERT INTO bbbffl_official_result VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        matchup["matchup_id"],
                        version,
                        home,
                        away,
                        now,
                        actor.actor_id,
                        reason,
                        snapshot,
                    ),
                )
                conn.execute(
                    "UPDATE bbbffl_matchup SET effective_official_version=?, review_version=review_version+1 "
                    "WHERE matchup_id=?",
                    (version, matchup["matchup_id"]),
                )
                append_event(
                    conn,
                    actor=actor,
                    action="competition.result.corrected",
                    entity_type="competition.matchup",
                    entity_id=matchup["matchup_id"],
                    entity_version=str(version),
                    correlation_id=correlation,
                    reason=reason,
                    before_state={"official_version": old},
                    after_state={"official_version": version},
                )
            round_version = row["version"] + 1
            conn.execute(
                "UPDATE bbbffl_round_lifecycle SET version=?, updated_at=? WHERE bbbffl_round_id=?",
                (round_version, now, round_id),
            )
            append_event(
                conn,
                actor=actor,
                action="competition.round.corrected",
                entity_type="competition.round",
                entity_id=round_id,
                entity_version=str(round_version),
                correlation_id=correlation,
                reason=reason,
                before_state={"state": "final"},
                after_state={"state": "final"},
                payload={"matchup_count": 5},
            )
        return self.get_round(round_id)

    def correct_matchup_result(
        self,
        matchup_id,
        home_score,
        away_score,
        *,
        reason,
        actor=ActorContext.anonymous_operator("admin"),
        input_snapshot=None,
        expected_review_version=None,
    ):
        """Correct exactly one already-final matchup's official result,
        atomically: the previous official version is preserved unchanged,
        a new version becomes effective, and both changes commit together
        with the matchup's audit trail (issue #58 requirement 9). Unlike
        `correct_results`, this does not require every matchup in the round
        to be resubmitted -- "reopen[ing] the round" (requirement 9) is
        represented explicitly by widening exactly this one matchup's
        official-result history, without ever moving `bbbffl_round_lifecycle.
        state` out of `final` (so no other matchup's effective version, and
        no round-level fact, is ever put at risk by one matchup's correction)."""
        if not reason:
            raise ValueError("an authorised correction requires a reason")
        with transaction(self.database) as conn:
            matchup = conn.execute(
                "SELECT * FROM bbbffl_matchup WHERE matchup_id=?" + _for_update_suffix(self.database),
                (matchup_id,),
            ).fetchone()
            if not matchup:
                raise KeyError(matchup_id)
            if expected_review_version is not None and matchup["review_version"] != expected_review_version:
                raise StaleRoundVersionError(
                    f"matchup {matchup_id} review is at version {matchup['review_version']}, "
                    f"not the expected {expected_review_version}"
                )
            round_row = conn.execute(
                "SELECT state FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?" + _for_update_suffix(self.database),
                (matchup["bbbffl_round_id"],),
            ).fetchone()
            if not round_row or round_row["state"] != "final":
                raise ValueError("only a matchup in a final round can be corrected")
            old = matchup["effective_official_version"]
            if old is None:
                raise ValueError("matchup has no effective official result to correct")
            version = old + 1
            now = _now()
            correlation = new_correlation_id()
            snapshot = json.dumps(input_snapshot, sort_keys=True, default=str) if input_snapshot is not None else None
            conn.execute(
                "INSERT INTO bbbffl_official_result VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (matchup_id, version, home_score, away_score, now, actor.actor_id, reason, snapshot),
            )
            conn.execute(
                "UPDATE bbbffl_matchup SET effective_official_version=?, review_version=review_version+1 "
                "WHERE matchup_id=?",
                (version, matchup_id),
            )
            append_event(
                conn,
                actor=actor,
                action="competition.result.corrected",
                entity_type="competition.matchup",
                entity_id=matchup_id,
                entity_version=str(version),
                correlation_id=correlation,
                reason=reason,
                before_state={"official_version": old},
                after_state={"official_version": version},
            )
        return self.effective_result(matchup_id)

    def get_calculation(self, matchup_id):
        row = self.database.execute(
            "SELECT * FROM bbbffl_matchup_calculation WHERE matchup_id=?", (matchup_id,)
        ).fetchone()
        if not row:
            return None
        from app.calculations import CalculatedMatchup

        return CalculatedMatchup(
            matchup_id=row["matchup_id"],
            revision=row["revision"],
            input_fingerprint=row["input_fingerprint"],
            snapshot=json.loads(row["snapshot"]),
        )

    def save_calculation(self, matchup_id, snapshot):
        """Legacy snapshot attachment; season scoring uses calculations service."""
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        with transaction(self.database) as conn:
            current = conn.execute(
                "SELECT revision FROM bbbffl_matchup_calculation WHERE matchup_id=?"
                + _for_update_suffix(self.database),
                (matchup_id,),
            ).fetchone()
            if current:
                revision = current["revision"] + 1
                conn.execute(
                    "UPDATE bbbffl_matchup_calculation SET revision=?, snapshot=?, updated_at=? WHERE matchup_id=?",
                    (revision, encoded, _now(), matchup_id),
                )
            else:
                # Both supported databases make the conflict branch atomic.
                # If two writers saw no row, one inserts revision 1 and the
                # other waits for it, then increments that committed row to 2.
                # This also avoids exposing an expected first-write race as a
                # primary-key error to callers.
                created = conn.execute(
                    "INSERT INTO bbbffl_matchup_calculation "
                    "(matchup_id, revision, snapshot, updated_at) VALUES (?, 1, ?, ?) "
                    "ON CONFLICT (matchup_id) DO UPDATE SET "
                    "revision=bbbffl_matchup_calculation.revision + 1, "
                    "snapshot=excluded.snapshot, updated_at=excluded.updated_at "
                    "RETURNING revision",
                    (matchup_id, encoded, _now()),
                ).fetchone()
                revision = created["revision"]
        return revision

    def record_upstream_fact(self, round_id, provider_status, payload=None):
        with transaction(self.database) as conn:
            conn.execute(
                "INSERT INTO bbbffl_round_upstream_fact VALUES (?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    round_id,
                    provider_status,
                    (json.dumps(payload, sort_keys=True) if payload is not None else None),
                    _now(),
                ),
            )

    def get_round(self, round_id):
        row = self.database.execute(
            "SELECT * FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?", (round_id,)
        ).fetchone()
        return CompetitionRound(**dict(row)) if row else None

    def list_ordinary_rounds(self):
        """Return persisted ordinary rounds for scorer presentation layers."""
        rows = self.database.execute(
            """
            SELECT l.* FROM bbbffl_round_lifecycle l
            JOIN competition_stream c ON c.competition_id=l.competition_id
            WHERE c.stream_type='ordinary'
            ORDER BY l.afl_season_id DESC, l.fixture_round_number DESC
            """
        ).fetchall()
        return [CompetitionRound(**dict(row)) for row in rows]

    def list_matchups(self, round_id):
        rows = self.database.execute(
            "SELECT * FROM bbbffl_matchup WHERE bbbffl_round_id=? ORDER BY matchup_order",
            (round_id,),
        ).fetchall()
        return [Matchup(**dict(row)) for row in rows]

    def get_matchup(self, matchup_id):
        row = self.database.execute("SELECT * FROM bbbffl_matchup WHERE matchup_id=?", (matchup_id,)).fetchone()
        return Matchup(**dict(row)) if row else None

    def result_history(self, matchup_id):
        rows = self.database.execute(
            "SELECT * FROM bbbffl_official_result WHERE matchup_id=? ORDER BY version",
            (matchup_id,),
        ).fetchall()
        return [_row_to_official_result(row) for row in rows]

    def effective_result(self, matchup_id):
        row = self.database.execute(
            "SELECT r.* FROM bbbffl_matchup m JOIN bbbffl_official_result r ON r.matchup_id=m.matchup_id AND r.version=m.effective_official_version WHERE m.matchup_id=?",
            (matchup_id,),
        ).fetchone()
        return _row_to_official_result(row) if row else None

    def _locked_round(self, conn, round_id):
        row = conn.execute(
            "SELECT * FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?" + _for_update_suffix(self.database),
            (round_id,),
        ).fetchone()
        if not row:
            raise KeyError(round_id)
        return row

    def _locked_matchups(self, conn, round_id):
        rows = conn.execute(
            "SELECT * FROM bbbffl_matchup WHERE bbbffl_round_id=? ORDER BY matchup_order"
            + _for_update_suffix(self.database),
            (round_id,),
        ).fetchall()
        if len(rows) != 5:
            raise ValueError("ordinary round must contain exactly five matchups")
        return rows

    @staticmethod
    def _validate_result_set(matchups, results):
        expected = {row["matchup_id"] for row in matchups}
        if set(results) != expected:
            raise ValueError("official publication requires results for exactly all five matchups")
        for scores in results.values():
            if not isinstance(scores, (tuple, list)) or len(scores) != 2:
                raise ValueError("each result requires home and away scores")

    @staticmethod
    def _validate_review_versions(matchups, expected_review_versions):
        """Re-checked under the same row lock as the write itself, so a
        ruling/override recorded after the caller last read the review
        cannot be silently published/corrected over -- see
        `StaleRoundVersionError`."""
        for matchup in matchups:
            expected = expected_review_versions.get(matchup["matchup_id"])
            if expected is not None and matchup["review_version"] != expected:
                raise StaleRoundVersionError(
                    f"matchup {matchup['matchup_id']} review is at version {matchup['review_version']}, "
                    f"not the expected {expected}"
                )

    def _validate_frozen_context(self, conn, row):
        """Issue #197: a `fixture_draw_id` of `NULL` means this round was
        created by `create_non_ordinary_round` (finals/superscore), which
        never had a frozen fixture-draw context to snapshot in the first
        place -- there is nothing to re-validate there, so that half of the
        check is skipped entirely rather than failing closed against a
        context that was never captured. The accepted-mapping check below
        is unconditional: it "stays meaningful regardless of stream type"
        (docs/2026-finals-superscore-design.md) and every round, ordinary
        or not, is created with one. An ordinary round's `fixture_draw_id`
        is always non-null (`ck_lifecycle_fixture_context_all_or_none`), so
        its existing fixture-draw validation is completely unchanged."""
        if row["fixture_draw_id"] is not None:
            draw = conn.execute(
                "SELECT state, version FROM season_fixture_draw WHERE fixture_draw_id=?",
                (row["fixture_draw_id"],),
            ).fetchone()
            if not draw or draw["state"] != "frozen" or draw["version"] != row["fixture_draw_version"]:
                raise ValueError("frozen fixture context changed; round remains closed")
        mapping = conn.execute(
            "SELECT current_revision FROM round_afl_mapping WHERE mapping_id=?" + _for_update_suffix(self.database),
            (row["mapping_id"],),
        ).fetchone()
        if not mapping or mapping["current_revision"] != row["mapping_revision"]:
            raise ValueError("accepted mapping changed; round remains closed pending explicit resolution")
