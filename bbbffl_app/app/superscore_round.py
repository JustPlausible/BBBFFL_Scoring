"""Issue #192: SuperScore roster, eligibility and round lifecycle setup --
the third of six #170 follow-ups, built on #197's `create_non_ordinary_round`
primitive (merged in 0029/0030/#200) exactly as `app.finals` (#190) is.

Read `docs/2026-finals-superscore-design.md`'s "SuperScore design" section
first. This module owns:

- Creating the `superscore`-typed `competition_stream` and its four
  independent rounds (SS1-SS4), for all ten coaches, concurrent with finals
  -- no bracket, no pairing, no matchup (`app.competition_lifecycle.
  CompetitionLifecycleRepository.create_stream_matchup` is finals-only by
  design; SuperScore never calls it).
- The durable per-entry `superscore_entry_review_state` row set: round
  setup creates one row (`review_version=0`) for every one of the ten
  eligible entries *before* any lineup, ruling or calculation exists, and
  fails the whole setup atomically if the complete set of ten cannot be
  created or verified. This row -- never a field on the optional,
  derived entry-scoped calculation row #193 will add -- is the durable
  lock/CAS target for the round's lifetime; a round must not open until the
  complete set exists (`open_round` below).
- Confirming the SS1-SS4 AFL-round mapping against real `afl-api` evidence
  via the existing `app.round_mapping.RoundMappingRepository`/
  `AflApiReferenceValidator` boundary -- the same mapping machinery every
  other stream (ordinary, finals) already uses, never a bespoke check.

This module deliberately does **not** implement SuperScore scoring,
publication or leaderboard results (#193's job, which locks/compares/
records against the review-state rows created here but must never advance
them), a SuperScore-specific draft/roster (there is none -- the same ten
`season_entry` rows and `player_ownership_period` ledger as the ordinary
competition), or cumulative/aggregate standings across the four rounds.
"""

from dataclasses import dataclass

from app.audit import ActorContext, append_event
from app.competition_lifecycle import CompetitionLifecycleRepository
from app.db import _for_update_suffix, transaction
from app.round_mapping import AflReferenceValidator, RoundMapping, RoundMappingRepository
from app.season import SeasonRepository, _now

# Confirmed historical rule (docs/2026-finals-superscore-design.md's
# "SuperScore design"): all ten coaches participate in every SuperScore
# round, including the five teams eliminated from or never qualified for
# the finals. There is no SuperScore-specific eligibility narrower than
# "is a season entry of this season" -- see `app.superscore_participation.
# require_superscore_entry_eligible`, the explicit per-request check this
# module's setup count is the durable, round-scoped counterpart of.
EXPECTED_ENTRY_COUNT = 10

STREAM_TYPE = "superscore"
ROUND_LABELS = {1: "SS1", 2: "SS2", 3: "SS3", 4: "SS4"}


class SuperScoreRoundError(ValueError):
    """Base class for this module's domain errors."""


class IncompleteReviewStateError(SuperScoreRoundError):
    """Round setup could not create/verify the complete set of ten durable
    `superscore_entry_review_state` rows -- the whole setup transaction is
    rolled back, and (see `open_round`) the round must not transition to
    `open` while this remains true."""


@dataclass(frozen=True)
class SuperScoreStream:
    competition_id: str
    season_id: str
    ordinary_competition_id: str


def eligible_entries(database, season_id: str) -> list[str]:
    """Every season entry eligible for SuperScore -- always all ten
    (docs/2026-finals-superscore-design.md's confirmed rule), sorted for a
    deterministic lock order wherever this is used to acquire multiple
    `superscore_entry_review_state` row locks at once (mirroring the
    finals bracket's deterministic `matchup_id`-ordered locking)."""
    rows = database.execute(
        "SELECT season_entry_id FROM season_entry WHERE season_id=? ORDER BY season_entry_id", (season_id,)
    ).fetchall()
    return [row["season_entry_id"] for row in rows]


def get_stream(database, season_id: str) -> SuperScoreStream | None:
    row = database.execute(
        "SELECT competition_id, season_id, ordinary_competition_id FROM superscore_stream WHERE season_id=?",
        (season_id,),
    ).fetchone()
    return SuperScoreStream(**dict(row)) if row else None


def ensure_stream(
    database,
    season_id: str,
    rules_version_id: str,
    ordinary_competition_id: str,
    *,
    stream_key: str = "superscore",
    label: str = "SuperScore",
    actor: ActorContext = ActorContext.anonymous_operator("admin"),
    reason: str | None = None,
) -> SuperScoreStream:
    """Idempotently create (or return the already-created) `superscore`
    competition stream for this season, recording which ordinary
    competition its SS1 cross-stream carry-forward fallback resolves
    against (`app.superscore_participation.resolve_cross_stream_fallback_
    source`) -- the SuperScore-stream counterpart of `finals_bracket.
    ordinary_competition_id`.

    Validates `ordinary_competition_id` up front, mirroring `app.finals.
    FinalsBracketRepository._resolve_seed`'s identical check for
    `finals_bracket.ordinary_competition_id`: `superscore_stream.
    ordinary_competition_id` carries the same foreign key, so an unchecked
    bogus id would otherwise only fail *after* `create_competition` below
    has already committed its own `competition_stream` row in its own
    transaction (`app.db.transaction` never nests), leaving an orphan that
    then blocks a retry on `(season_id, stream_key)` uniqueness. Failing
    here instead means nothing is created at all on bad input."""
    existing = get_stream(database, season_id)
    if existing is not None:
        if existing.ordinary_competition_id != ordinary_competition_id:
            raise SuperScoreRoundError(
                f"a SuperScore stream already exists for season {season_id} scoped to ordinary competition "
                f"{existing.ordinary_competition_id!r}, not {ordinary_competition_id!r}"
            )
        return existing
    ordinary = database.execute(
        "SELECT season_id, stream_type FROM competition_stream WHERE competition_id=?",
        (ordinary_competition_id,),
    ).fetchone()
    if ordinary is None or ordinary["season_id"] != season_id or ordinary["stream_type"] != "ordinary":
        raise SuperScoreRoundError(
            f"ordinary_competition_id {ordinary_competition_id!r} must name this season's own ordinary "
            "home-and-away competition"
        )
    season_repo = SeasonRepository(database)
    created = season_repo.create_competition(season_id, rules_version_id, stream_key, label, STREAM_TYPE)
    now = _now()
    with transaction(database) as conn:
        conn.execute(
            "INSERT INTO superscore_stream VALUES (?, ?, ?, ?)",
            (created.competition_id, season_id, ordinary_competition_id, now),
        )
        append_event(
            conn,
            actor=actor,
            action="superscore.stream.created",
            entity_type="superscore.stream",
            entity_id=created.competition_id,
            entity_version="1",
            reason=reason,
            after_state={"season_id": season_id, "ordinary_competition_id": ordinary_competition_id},
        )
    return SuperScoreStream(created.competition_id, season_id, ordinary_competition_id)


def initialize_structure(
    database,
    season_id: str,
    rules_version_id: str,
    ordinary_competition_id: str,
    *,
    stream_key: str = "superscore",
    label: str = "SuperScore",
    actor: ActorContext,
    reason: str | None = None,
) -> dict:
    """Issue #237's production-safe SuperScore initialization: the
    `superscore` competition stream *and* its four logical SS1-SS4 rounds,
    created in one transaction -- the atomic counterpart of calling
    `ensure_stream` then `ensure_round` four times (each of which commits on
    its own), with identical row shapes (`ssN`/`SSN`, sequence N) and the
    same `superscore.stream.created` audit event, so every existing reader
    (`app.finals_superscore_open`, the dashboards, `app.season_completion`)
    sees exactly what the 2026 tooling produced.

    Idempotent: an already-complete, exactly-matching structure returns
    `created=False` with nothing written. A stream created earlier by the
    2026 per-step tooling with only some SS rounds is completed with the
    missing ones (exactly what further `ensure_round` calls would do); any
    existing round that deviates from the expected shape, or a stream
    scoped to a different ordinary competition, fails closed. Refused for a
    completed season (`SeasonRepository.guard_writable`, which also takes
    the season row lock serializing concurrent initializations).

    Deliberately checks nothing about Finals: the "not before Finals exists"
    prerequisite is a Finals+SuperScore composition rule, owned by the
    caller (`app.season_setup`), never a dependency of this module on
    `app.finals`."""
    expected = [(number, label_.lower(), label_) for number, label_ in sorted(ROUND_LABELS.items())]
    season_repo = SeasonRepository(database)
    with transaction(database) as conn:
        if database.engine.dialect.name == "sqlite":
            conn.execute("UPDATE bbbffl_season SET updated_at=updated_at WHERE season_id=?", (season_id,))
        season_repo.guard_writable(conn, season_id)
        existing = conn.execute(
            "SELECT competition_id, ordinary_competition_id FROM superscore_stream WHERE season_id=?", (season_id,)
        ).fetchone()
        if existing is not None:
            if existing["ordinary_competition_id"] != ordinary_competition_id:
                raise SuperScoreRoundError(
                    f"a SuperScore stream already exists for season {season_id} scoped to ordinary competition "
                    f"{existing['ordinary_competition_id']!r}, not {ordinary_competition_id!r}"
                )
            competition_id = existing["competition_id"]
            stream_created = False
        else:
            ordinary = conn.execute(
                "SELECT season_id, stream_type FROM competition_stream WHERE competition_id=?",
                (ordinary_competition_id,),
            ).fetchone()
            if ordinary is None or ordinary["season_id"] != season_id or ordinary["stream_type"] != "ordinary":
                raise SuperScoreRoundError(
                    f"ordinary_competition_id {ordinary_competition_id!r} must name this season's own ordinary "
                    "home-and-away competition"
                )
            squatter = conn.execute(
                "SELECT stream_type FROM competition_stream WHERE season_id=? AND stream_key=?",
                (season_id, stream_key),
            ).fetchone()
            if squatter is not None:
                raise SuperScoreRoundError(
                    f"stream key {stream_key!r} is already used by a {squatter['stream_type']!r} stream with no "
                    "SuperScore stream record; refusing to guess whether it is SuperScore"
                )
            created = season_repo.create_competition_in_transaction(
                conn, season_id, rules_version_id, stream_key, label, STREAM_TYPE
            )
            competition_id = created.competition_id
            conn.execute(
                "INSERT INTO superscore_stream VALUES (?, ?, ?, ?)",
                (competition_id, season_id, ordinary_competition_id, _now()),
            )
            append_event(
                conn,
                actor=actor,
                action="superscore.stream.created",
                entity_type="superscore.stream",
                entity_id=competition_id,
                entity_version="1",
                reason=reason,
                after_state={"season_id": season_id, "ordinary_competition_id": ordinary_competition_id},
            )
            stream_created = True
        rows = conn.execute(
            "SELECT bbbffl_round_id, sequence, round_key, label FROM bbbffl_round WHERE competition_id=? "
            "ORDER BY sequence",
            (competition_id,),
        ).fetchall()
        by_key = {row["round_key"]: row for row in rows}
        unexpected = [
            row["round_key"] for row in rows if (row["sequence"], row["round_key"], row["label"]) not in expected
        ]
        if unexpected:
            raise SuperScoreRoundError(
                f"the SuperScore stream has unexpected or differently-shaped round(s) {unexpected}; "
                "refusing to modify a round structure this command did not create"
            )
        round_ids: dict[int, str] = {}
        created_rounds: list[str] = []
        for number, round_key, round_label in expected:
            if round_key in by_key:
                round_ids[number] = by_key[round_key]["bbbffl_round_id"]
                continue
            round_ids[number] = season_repo.create_round_in_transaction(
                conn, competition_id, round_key, round_label, number
            ).bbbffl_round_id
            created_rounds.append(round_label)
        if created_rounds:
            append_event(
                conn,
                actor=actor,
                action="superscore.rounds.initialized",
                entity_type="superscore.stream",
                entity_id=competition_id,
                reason=reason,
                after_state={"season_id": season_id, "created_rounds": created_rounds},
            )
    return {
        "created": stream_created or bool(created_rounds),
        "stream_created": stream_created,
        "created_rounds": created_rounds,
        "stream": SuperScoreStream(competition_id, season_id, ordinary_competition_id),
        "round_ids": round_ids,
    }


def _require_superscore_competition(database, competition_id: str) -> None:
    """The `ensure_round`/`resolve_concurrent_finals_afl_mapping` sibling of
    `_require_superscore_round`: refuses a `competition_id` that does not
    belong to a `superscore`-typed `competition_stream`, *before* a
    round/mapping is created against it (Codex review, PR #207, round 5:
    `_require_superscore_round` alone runs too late -- `ensure_round` given
    a finals `competition_id` by mistake would already have created a
    bogus `ss1`-labelled round under it, and `resolve_concurrent_finals_
    afl_mapping` would then happily persist a mapping against it, before
    either ever reached `setup_round`/`open_round`)."""
    row = database.execute(
        "SELECT stream_type FROM competition_stream WHERE competition_id=?", (competition_id,)
    ).fetchone()
    if row is None:
        raise SuperScoreRoundError(f"unknown competition {competition_id}")
    if row["stream_type"] != STREAM_TYPE:
        raise SuperScoreRoundError(
            f"competition {competition_id} is a {row['stream_type']!r}-typed stream, not {STREAM_TYPE!r}"
        )


def ensure_round(database, competition_id: str, round_number: int, sequence: int) -> str:
    """Idempotently create (or return the already-created) logical
    `bbbffl_round` row (`app.season.SeasonRepository.create_round`'s generic
    primitive -- unchanged, no SuperScore-specific schema) for one of
    SS1-SS4. Returns `bbbffl_round_id`."""
    _require_superscore_competition(database, competition_id)
    if round_number not in ROUND_LABELS:
        raise SuperScoreRoundError(f"unknown SuperScore round number: {round_number}")
    label = ROUND_LABELS[round_number]
    round_key = label.lower()
    existing = database.execute(
        "SELECT bbbffl_round_id FROM bbbffl_round WHERE competition_id=? AND round_key=?",
        (competition_id, round_key),
    ).fetchone()
    if existing is not None:
        return existing["bbbffl_round_id"]
    created = SeasonRepository(database).create_round(competition_id, round_key, label, sequence)
    return created.bbbffl_round_id


def confirm_afl_mapping(
    database,
    validator: AflReferenceValidator,
    bbbffl_round_id: str,
    afl_season_id: int,
    afl_round_id: int,
    *,
    actor: ActorContext = ActorContext.anonymous_operator("admin"),
    reason: str,
) -> RoundMapping:
    """Confirm (accept, or correct if a diverging one already exists) the
    AFL-round mapping for one SuperScore round against real `afl-api`
    evidence, via the same `app.round_mapping` boundary every stream uses --
    never a bespoke SuperScore mapping check. `validator` is anything
    satisfying `app.round_mapping.AflReferenceValidator` (production callers
    pass `app.round_mapping.AflApiReferenceValidator(afl_client)`, exactly
    as `app.round_preflight` does for the ordinary/ finals case). Idempotent:
    re-confirming the identical `(afl_season_id, afl_round_id)` is a no-op.

    Per docs/2026-finals-superscore-design.md's confirmed rule, SS1-SS4 run
    across the *same* four AFL rounds as the four finals weeks -- callers
    should source `afl_season_id`/`afl_round_id` from the corresponding
    finals week's own accepted mapping (`app.round_mapping.
    RoundMappingRepository.resolve`) rather than re-deriving them, so the
    evidence for both streams' concurrency is the identical accepted AFL
    round reference, not merely an assumption of equal round numbers."""
    repo = RoundMappingRepository(database)
    existing = repo.resolve(bbbffl_round_id)
    if existing is not None and existing.afl_season_id == afl_season_id and existing.afl_round_id == afl_round_id:
        return existing
    if existing is None:
        return repo.accept(bbbffl_round_id, afl_season_id, afl_round_id, validator, actor=actor, reason=reason)
    return repo.correct(bbbffl_round_id, afl_season_id, afl_round_id, validator, actor=actor, reason=reason)


_ROUND_KEY_TO_WEEK = {label.lower(): number for number, label in ROUND_LABELS.items()}


def resolve_concurrent_finals_afl_mapping(database, bbbffl_round_id: str) -> RoundMapping:
    """Resolve the accepted AFL-round mapping of the *exact* finals week that
    must run concurrently with the given SuperScore round (SS1 <-> finals
    week 1, ..., SS4 <-> finals week 4), per `confirm_afl_mapping`'s own
    documented rule -- verifying the finals week actually belongs to the
    same season *and* is the matching week number, not merely that some
    finals round happens to carry an accepted mapping (Codex review, PR
    #207, round 2: an operator-suppliable "which finals round" parameter
    cannot be trusted to be the *correct* one -- deriving both the season
    and the week number from `bbbffl_round_id` itself, with no
    operator-suppliable substitute, closes that off by construction).

    Raises `SuperScoreRoundError` if `bbbffl_round_id` does not belong to a
    `superscore`-typed stream, is not one of SS1-SS4, its season has no
    finals bracket yet, that bracket has no matching week yet, or that
    week's round has not been opened yet (its AFL-round mapping is frozen
    onto `bbbffl_round_lifecycle` only once `open_finals_week` creates that
    row -- see the note below on why this reads the frozen row rather than
    the mapping's own current head)."""
    round_row = database.execute(
        "SELECT r.round_key, c.season_id, c.stream_type FROM bbbffl_round r "
        "JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (bbbffl_round_id,),
    ).fetchone()
    if round_row is None:
        raise SuperScoreRoundError(f"unknown round {bbbffl_round_id}")
    if round_row["stream_type"] != STREAM_TYPE:
        raise SuperScoreRoundError(
            f"round {bbbffl_round_id} belongs to a {round_row['stream_type']!r}-typed stream, not {STREAM_TYPE!r}"
        )
    week_number = _ROUND_KEY_TO_WEEK.get(round_row["round_key"])
    if week_number is None:
        raise SuperScoreRoundError(f"round {bbbffl_round_id} (round_key={round_row['round_key']!r}) is not SS1-SS4")
    season_id = round_row["season_id"]

    bracket = database.execute("SELECT bracket_id FROM finals_bracket WHERE season_id=?", (season_id,)).fetchone()
    if bracket is None:
        raise SuperScoreRoundError(
            f"season {season_id} has no finals bracket yet; cannot derive "
            f"{ROUND_LABELS[week_number]}'s concurrent AFL-round mapping"
        )
    week = database.execute(
        "SELECT bbbffl_round_id FROM finals_bracket_week WHERE bracket_id=? AND week_number=?",
        (bracket["bracket_id"], week_number),
    ).fetchone()
    if week is None:
        raise SuperScoreRoundError(f"finals bracket {bracket['bracket_id']} has no week {week_number} round yet")

    # Codex review, PR #207, round 7: reading `RoundMappingRepository.
    # resolve()` here returns the finals week's *current* accepted mapping
    # head, not necessarily what that week's own round is actually using.
    # `app.competition_lifecycle.CompetitionLifecycleRepository.
    # create_non_ordinary_round` freezes `mapping_id`/`mapping_revision`/
    # `afl_season_id`/`afl_round_id` onto the finals round's own
    # `bbbffl_round_lifecycle` row at *round-creation* time (when
    # `open_finals_week` first creates it), and every later read of that
    # round (`app.calculations._round_context`'s `l.afl_round_id`, `l.*`)
    # uses that frozen snapshot, never a fresh `resolve()`. A correction
    # to the finals week's mapping after its round was created (e.g. via
    # `app.round_mapping.RoundMappingRepository.correct`, which has no
    # dependency on lifecycle state at all) advances the mapping head
    # without updating that already-frozen lifecycle row -- so deriving
    # SuperScore's mapping from the live head, as this function used to,
    # could accept SS1-SS4 against a *different* real AFL round than the
    # one finals week actually calculates against, silently violating the
    # shared-round invariant. Reading the finals week's own frozen
    # lifecycle row instead guarantees SuperScore always derives the
    # exact same AFL round finals calculations already committed to.
    lifecycle_row = database.execute(
        "SELECT mapping_id, mapping_revision, provider, afl_season_id, afl_round_id, created_at "
        "FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?",
        (week["bbbffl_round_id"],),
    ).fetchone()
    if lifecycle_row is None:
        raise SuperScoreRoundError(
            f"finals week {week_number} (round {week['bbbffl_round_id']}) has not been opened yet; "
            "open it first so its AFL-round mapping is frozen"
        )
    return RoundMapping(
        mapping_id=lifecycle_row["mapping_id"],
        bbbffl_round_id=week["bbbffl_round_id"],
        revision=lifecycle_row["mapping_revision"],
        state="accepted",
        provider=lifecycle_row["provider"],
        afl_season_id=lifecycle_row["afl_season_id"],
        afl_round_id=lifecycle_row["afl_round_id"],
        created_at=lifecycle_row["created_at"],
        created_by=None,
        reason=None,
    )


def _create_review_state_rows(database, season_id: str, bbbffl_round_id: str, *, actor: ActorContext, reason):
    """The core of gap #4: create an always-present `superscore_entry_
    review_state` row (`review_version=0`) for every one of the ten
    eligible entries, atomically -- raising (and rolling back every row
    this call itself inserted) if the complete set cannot be created or
    verified. Idempotent against a partially- or fully-completed prior
    attempt (`ON CONFLICT ... DO NOTHING`), so calling this again after an
    earlier failure -- or simply re-running setup -- never duplicates or
    disturbs an already-advanced row's `review_version`."""
    entries = eligible_entries(database, season_id)
    if len(entries) != EXPECTED_ENTRY_COUNT:
        raise IncompleteReviewStateError(
            f"season {season_id} has {len(entries)} entries, not the expected {EXPECTED_ENTRY_COUNT}; "
            "cannot create the complete SuperScore review-state row set"
        )
    now = _now()
    with transaction(database) as conn:
        for season_entry_id in entries:
            conn.execute(
                "INSERT INTO superscore_entry_review_state "
                "(bbbffl_round_id, season_entry_id, review_version, created_at, updated_at) "
                "VALUES (?, ?, 0, ?, ?) ON CONFLICT (bbbffl_round_id, season_entry_id) DO NOTHING",
                (bbbffl_round_id, season_entry_id, now, now),
            )
        # Issue #194: lock and count the individual rows in Python, rather
        # than `SELECT COUNT(*) ... FOR UPDATE` -- real PostgreSQL rejects
        # `FOR UPDATE` combined with an aggregate function ("FOR UPDATE is
        # not allowed with aggregate functions"), which made this method
        # fail outright against Postgres (confirmed on the unmodified base
        # branch; SQLite's tests never caught it because SQLite silently
        # tolerates `FOR UPDATE`). This still locks every matching row
        # before the count is trusted, exactly as the aggregate query
        # intended, and is the only change -- the completeness check and
        # its rollback-on-mismatch behaviour are unchanged.
        locked_rows = conn.execute(
            "SELECT season_entry_id FROM superscore_entry_review_state WHERE bbbffl_round_id=?"
            + _for_update_suffix(database),
            (bbbffl_round_id,),
        ).fetchall()
        count = len(locked_rows)
        if count != EXPECTED_ENTRY_COUNT:
            # Raising here rolls back this entire transaction -- including
            # every row this call itself just inserted -- so setup fails
            # atomically rather than leaving a partial set committed.
            raise IncompleteReviewStateError(
                f"round {bbbffl_round_id} has {count} superscore_entry_review_state rows, not the expected "
                f"{EXPECTED_ENTRY_COUNT}; round setup failed atomically and nothing was committed"
            )
        append_event(
            conn,
            actor=actor,
            action="superscore.round.review_state_created",
            entity_type="superscore.round",
            entity_id=bbbffl_round_id,
            entity_version=str(count),
            reason=reason,
            after_state={"season_entry_ids": entries},
        )


def _require_superscore_round(database, bbbffl_round_id: str) -> None:
    """Refuse a round that does not belong to a `superscore`-typed
    `competition_stream`. `CompetitionLifecycleRepository.
    create_non_ordinary_round` (#197) permits both finals and SuperScore
    streams, so nothing before this stopped an operator mistake from
    running SuperScore's own round-setup/open lifecycle against a finals
    week's round, corrupting it outside its own proper
    `app.finals_preflight.open_finals_week` pathway (Codex review, PR
    #207, round 4)."""
    row = database.execute(
        "SELECT c.stream_type FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
        "WHERE r.bbbffl_round_id=?",
        (bbbffl_round_id,),
    ).fetchone()
    if row is None:
        raise SuperScoreRoundError(f"unknown round {bbbffl_round_id}")
    if row["stream_type"] != STREAM_TYPE:
        raise SuperScoreRoundError(
            f"round {bbbffl_round_id} belongs to a {row['stream_type']!r}-typed stream, not {STREAM_TYPE!r}; "
            "use that stream's own lifecycle boundary instead"
        )


def setup_round(
    database,
    bbbffl_round_id: str,
    *,
    actor: ActorContext = ActorContext.anonymous_operator("admin"),
    reason: str | None = None,
):
    """Give a SuperScore round its `bbbffl_round_lifecycle` row (via #197's
    `create_non_ordinary_round`, idempotent here against an already-created
    round) and then create/verify its complete ten-row `superscore_entry_
    review_state` set (gap #4) -- the round must not open until both steps
    have succeeded (see `open_round`)."""
    _require_superscore_round(database, bbbffl_round_id)
    lifecycle = CompetitionLifecycleRepository(database)
    round_row = lifecycle.get_round(bbbffl_round_id)
    if round_row is None:
        round_row = lifecycle.create_non_ordinary_round(bbbffl_round_id, actor=actor, reason=reason)
    _create_review_state_rows(database, round_row.season_id, bbbffl_round_id, actor=actor, reason=reason)
    return round_row


def review_state_complete(database, bbbffl_round_id: str) -> bool:
    """Whether the round's durable review-state row set is complete -- the
    fail-closed gate `open_round` enforces before ever transitioning past
    `upcoming` (issue #192's acceptance criterion: "a round cannot open
    with an incomplete review-state set")."""
    count = database.execute(
        "SELECT COUNT(*) AS n FROM superscore_entry_review_state WHERE bbbffl_round_id=?", (bbbffl_round_id,)
    ).fetchone()["n"]
    return count == EXPECTED_ENTRY_COUNT


def open_round(
    database,
    bbbffl_round_id: str,
    *,
    actor: ActorContext = ActorContext.anonymous_operator("scorer"),
    reason: str | None = None,
):
    """The `upcoming -> open` transition for a SuperScore round, fenced by
    `review_state_complete` -- refuses (`IncompleteReviewStateError`,
    no mutation) rather than ever opening a round whose durable per-entry
    review-state rows are not all present. Nothing about #197's own
    `_validate_frozen_context`/mapping-revision check is duplicated or
    weakened here; this is purely an additive precondition in front of the
    existing, unmodified `CompetitionLifecycleRepository.transition`."""
    _require_superscore_round(database, bbbffl_round_id)
    if not review_state_complete(database, bbbffl_round_id):
        raise IncompleteReviewStateError(
            f"round {bbbffl_round_id} does not have a complete superscore_entry_review_state row set; "
            "it cannot open until setup_round() has succeeded"
        )
    return CompetitionLifecycleRepository(database).transition(bbbffl_round_id, "open", actor=actor, reason=reason)


def advance_round_to_review(
    database,
    bbbffl_round_id: str,
    *,
    actor: ActorContext = ActorContext.anonymous_operator("scorer"),
    reason: str | None = None,
):
    """Stream-aware equivalent of the generic `/rounds/{id}/transition`
    route (`app/routes/round_review.py`'s `transition_round_review`, which
    explicitly refuses a non-`ordinary` round and directs it to "its own
    stream-aware lifecycle module") -- this is that module's SuperScore
    half, the sibling of `app.finals.FinalsBracketRepository.
    advance_week_to_review`. `SuperScoreLeaderboardService._persist`
    requires the round to already be `review` or `final`
    (`app/superscore_results.py`), but nothing before this ever moved a
    SuperScore round past `open`; every SS round must pass through here
    (open -> live -> review) after lineups are submitted and before
    calculation/publication. Uses the same `CompetitionLifecycleRepository.
    transition`/`LEGAL_TRANSITIONS` every ordinary round already uses -- no
    new lifecycle mechanism. Idempotent against a round already at
    `review` or `final`."""
    _require_superscore_round(database, bbbffl_round_id)
    lifecycle = CompetitionLifecycleRepository(database)
    current = lifecycle.get_round(bbbffl_round_id)
    if current is None or current.state == "upcoming":
        raise SuperScoreRoundError(f"round {bbbffl_round_id} is not open yet; run open_round first")
    if current.state in ("review", "final"):
        return current
    if current.state == "open":
        current = lifecycle.transition(bbbffl_round_id, "live", actor=actor, reason=reason)
    return lifecycle.transition(bbbffl_round_id, "review", actor=actor, reason=reason)


def get_review_state(database, bbbffl_round_id: str, season_entry_id: str) -> int | None:
    row = database.execute(
        "SELECT review_version FROM superscore_entry_review_state WHERE bbbffl_round_id=? AND season_entry_id=?",
        (bbbffl_round_id, season_entry_id),
    ).fetchone()
    return row["review_version"] if row else None
