"""Finals-only review/publication adapter with variable matchup counts."""

import json
from contextlib import nullcontext

from app.audit import append_event, new_correlation_id
from app.calculations import MatchupCalculationService
from app.db import _for_update_suffix, transaction
from app.finals import (
    _MAX_REWIND_RETRIES,
    FinalsBracketAdvanceStateError,
    FinalsBracketRepository,
    _StaleMatchupDuringDownstreamCheck,
)
from app.round_review import SignoffValidationError, _freeze_matchup_inputs, build_matchup_review
from app.season import SeasonRepository, _now

EXPECTED_MATCH_COUNTS = {1: 2, 2: 2, 3: 1, 4: 1}


class StaleFinalsSnapshotError(RuntimeError):
    pass


def _mathematical_wooden_spoon(conn, bracket_id):
    """Resolve rank 10 from the bracket's frozen mathematical provenance."""
    bracket = conn.execute("SELECT * FROM finals_bracket WHERE bracket_id=?", (bracket_id,)).fetchone()
    if bracket is None:
        raise KeyError(bracket_id)
    if bracket["seed_source"] == "snapshot":
        row = conn.execute(
            "SELECT mr.season_entry_id, s.snapshot_id, s.through_round "
            "FROM finals_seeding_snapshot s JOIN finals_seeding_snapshot_mathematical_row mr "
            "ON mr.snapshot_id=s.snapshot_id WHERE s.snapshot_id=? AND mr.rank=10",
            (bracket["finals_seeding_snapshot_id"],),
        ).fetchone()
        if row is None:
            raise ValueError("snapshot-backed bracket has no frozen mathematical rank 10")
        return row["season_entry_id"], {
            "seed_source": "snapshot",
            "finals_seeding_snapshot_id": row["snapshot_id"],
            "through_round": row["through_round"],
        }
    if bracket["seed_source"] == "ladder":
        # For the ladder path `_resolve_seed` freezes the mathematical order
        # itself in finals_bracket_seed (there is no historical override),
        # while result references preserve every input version that produced
        # it. Reading that frozen row is not a live ladder recomputation.
        row = conn.execute(
            "SELECT season_entry_id FROM finals_bracket_seed WHERE bracket_id=? AND seed_position=10",
            (bracket_id,),
        ).fetchone()
        refs = conn.execute(
            "SELECT matchup_id,official_version FROM finals_bracket_result_reference "
            "WHERE bracket_id=? ORDER BY matchup_id",
            (bracket_id,),
        ).fetchall()
        if row is None or not refs:
            raise ValueError("ladder-backed bracket has insufficient frozen mathematical provenance")
        return row["season_entry_id"], {
            "seed_source": "ladder",
            "through_round": bracket["through_round"],
            "latest_included_round": bracket["latest_included_round"],
            "result_references": [dict(ref) for ref in refs],
        }
    raise ValueError("unknown finals bracket seed source")


def _premier(conn, bracket_id, review):
    if review.home.effective_score != review.away.effective_score:
        return (
            review.home.season_entry_id
            if review.home.effective_score > review.away.effective_score
            else review.away.season_entry_id
        )
    ranks = conn.execute(
        "SELECT season_entry_id,seed_position FROM finals_bracket_seed WHERE bracket_id=? AND season_entry_id IN (?,?)",
        (bracket_id, review.home.season_entry_id, review.away.season_entry_id),
    ).fetchall()
    return min(ranks, key=lambda row: row["seed_position"])["season_entry_id"]


def _premier_for_scores(conn, bracket_id, home_id, away_id, home_score, away_score):
    if home_score != away_score:
        return home_id if home_score > away_score else away_id
    ranks = conn.execute(
        "SELECT season_entry_id,seed_position FROM finals_bracket_seed WHERE bracket_id=? AND season_entry_id IN (?,?)",
        (bracket_id, home_id, away_id),
    ).fetchall()
    return min(ranks, key=lambda row: row["seed_position"])["season_entry_id"]


def _finals_matchup_context(database, matchup_id):
    row = database.execute(
        "SELECT m.*, c.stream_type, c.season_id, w.bracket_id, w.week_number "
        "FROM bbbffl_matchup m JOIN bbbffl_round r ON r.bbbffl_round_id=m.bbbffl_round_id "
        "JOIN competition_stream c ON c.competition_id=r.competition_id "
        "JOIN finals_bracket_week w ON w.bbbffl_round_id=r.bbbffl_round_id "
        "JOIN finals_bracket_pairing p ON p.bracket_id=w.bracket_id AND p.week_number=w.week_number "
        "AND p.matchup_id=m.matchup_id AND p.status='active' "
        "WHERE m.matchup_id=? AND c.stream_type='finals'",
        (matchup_id,),
    ).fetchone()
    if row is None:
        raise ValueError("matchup is not an active finals matchup")
    return row


def build_finals_round_review(lifecycle, review_repo, identities, round_id, *, evidence_fresh=True):
    context = lifecycle.get_round(round_id)
    if context is None:
        raise KeyError(round_id)
    row = review_repo.database.execute(
        "SELECT w.week_number FROM finals_bracket_week w WHERE w.bbbffl_round_id=?", (round_id,)
    ).fetchone()
    if row is None:
        raise ValueError("round is not a finals bracket week")
    matchups = lifecycle.list_matchups(round_id)
    expected = EXPECTED_MATCH_COUNTS[row["week_number"]]
    reviews = [
        build_matchup_review(lifecycle, review_repo, identities, m, evidence_fresh=evidence_fresh) for m in matchups
    ]
    blockers = [] if len(reviews) == expected else [f"finals week requires exactly {expected} matchups"]
    return {
        "round": context,
        "week_number": row["week_number"],
        "matchups": reviews,
        "blockers": blockers,
        "ready": not blockers and all(r.eligible_for_signoff for r in reviews),
    }


def publish_finals_round(database, afl_client, lifecycle, review_repo, identities, round_id, *, actor, reason=None):
    """Freshly recompute, freeze, CAS-revalidate and publish 1/2 matches."""
    batch_factory = getattr(afl_client, "evidence_batch", None)
    scope = batch_factory() if callable(batch_factory) else nullcontext(afl_client)
    with scope as evidence:
        MatchupCalculationService(database, afl_client).calculate_round(round_id)
        fresh_fn = getattr(evidence, "is_evidence_fresh", None)
        fresh = fresh_fn() if callable(fresh_fn) else True
        review = build_finals_round_review(lifecycle, review_repo, identities, round_id, evidence_fresh=fresh)
    if not review["ready"]:
        raise SignoffValidationError(
            {m.matchup_id: m.blockers for m in review["matchups"] if m.blockers}, review["blockers"]
        )
    snapshots = {m.matchup_id: _freeze_matchup_inputs(m, actor) for m in review["matchups"]}
    results = {m.matchup_id: (m.home.effective_score, m.away.effective_score) for m in review["matchups"]}
    with transaction(database) as conn:
        # Issue #195's shared completed-season write fence: lock the owning
        # season row first, ahead of the round/matchup locks below, so this
        # transaction and `app.season_completion.complete_season` can only
        # ever serialize through that one lock.
        season_row = conn.execute(
            "SELECT season_id FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?", (round_id,)
        ).fetchone()
        if season_row is None:
            raise KeyError(round_id)
        SeasonRepository(database).guard_writable(conn, season_row["season_id"])
        round_row = conn.execute(
            "SELECT * FROM bbbffl_round_lifecycle WHERE bbbffl_round_id=?" + _for_update_suffix(database),
            (round_id,),
        ).fetchone()
        if round_row is None or round_row["state"] != "review":
            raise StaleFinalsSnapshotError("finals round is no longer in review")
        locked = {}
        for matchup_id in sorted(snapshots):
            locked[matchup_id] = conn.execute(
                "SELECT m.*, c.revision calculation_revision, c.input_fingerprint calculation_fingerprint "
                "FROM bbbffl_matchup m JOIN bbbffl_matchup_calculation c ON c.matchup_id=m.matchup_id "
                "WHERE m.matchup_id=?" + _for_update_suffix(database),
                (matchup_id,),
            ).fetchone()
        for matchup_id, snapshot in snapshots.items():
            row = locked[matchup_id]
            if (
                row["review_version"]
                != next(m.review_version for m in review["matchups"] if m.matchup_id == matchup_id)
                or row["calculation_revision"] != snapshot["calculation_revision"]
                or row["calculation_fingerprint"] != snapshot["calculation_fingerprint"]
            ):
                raise StaleFinalsSnapshotError("finals calculation or review changed while publication was assembled")
        correlation, now = new_correlation_id(), _now()
        for matchup_id in sorted(results):
            home, away = results[matchup_id]
            conn.execute(
                "INSERT INTO bbbffl_official_result VALUES (?,1,?,?,?,?,?,?)",
                (
                    matchup_id,
                    home,
                    away,
                    now,
                    actor.actor_id,
                    reason,
                    json.dumps(snapshots[matchup_id], sort_keys=True, default=str),
                ),
            )
            conn.execute("UPDATE bbbffl_matchup SET effective_official_version=1 WHERE matchup_id=?", (matchup_id,))
            append_event(
                conn,
                actor=actor,
                action="finals.result.published",
                entity_type="competition.matchup",
                entity_id=matchup_id,
                entity_version="1",
                correlation_id=correlation,
                reason=reason,
            )
        conn.execute(
            "UPDATE bbbffl_round_lifecycle SET state='final',version=version+1,updated_at=? WHERE bbbffl_round_id=?",
            (now, round_id),
        )
        append_event(
            conn,
            actor=actor,
            action="finals.round.finalized",
            entity_type="competition.round",
            entity_id=round_id,
            correlation_id=correlation,
            reason=reason,
            payload={"matchup_count": len(results), "week_number": review["week_number"]},
        )
        if review["week_number"] == 4:
            grand = review["matchups"][0]
            bracket = conn.execute(
                "SELECT bracket_id FROM finals_bracket_week WHERE bbbffl_round_id=?", (round_id,)
            ).fetchone()
            premier = _premier(conn, bracket["bracket_id"], grand)
            spoon, spoon_provenance = _mathematical_wooden_spoon(conn, bracket["bracket_id"])
            for action, entry in (("finals.premier.recorded", premier), ("finals.wooden_spoon.recorded", spoon)):
                provenance = (
                    spoon_provenance
                    if action == "finals.wooden_spoon.recorded"
                    else {"grand_final_matchup_id": grand.matchup_id, "official_version": 1}
                )
                append_event(
                    conn,
                    actor=actor,
                    action=action,
                    entity_type="season.entry",
                    entity_id=entry,
                    correlation_id=correlation,
                    reason=reason,
                    payload={"round_id": round_id, **provenance},
                )
    return lifecycle.get_round(round_id)


def correct_finals_result(database, afl_client, lifecycle, review_repo, identities, matchup_id, *, actor, reason):
    """Append a corrected finals result, then apply #190's rewind policy."""
    if not reason or not reason.strip():
        raise ValueError("a finals result correction requires an explicit reason")
    context = _finals_matchup_context(database, matchup_id)
    old_version = context["effective_official_version"]
    if old_version is None:
        raise ValueError("finals matchup has no published result to correct")
    batch_factory = getattr(afl_client, "evidence_batch", None)
    scope = batch_factory() if callable(batch_factory) else nullcontext(afl_client)
    with scope as evidence:
        # One shared facts cache and every round matchup lock, matching
        # publication's freshness/serialization boundary. A correction may
        # drive both downstream Week-2 pairings, so a partial stale round
        # must never be reconciled from mixed evidence.
        MatchupCalculationService(database, afl_client).calculate_round(context["bbbffl_round_id"])
        fresh_fn = getattr(evidence, "is_evidence_fresh", None)
        fresh = fresh_fn() if callable(fresh_fn) else True
        matchup = lifecycle.get_matchup(matchup_id)
        review = build_matchup_review(lifecycle, review_repo, identities, matchup, evidence_fresh=fresh)
    if review.blockers:
        raise SignoffValidationError({matchup_id: review.blockers})
    snapshot = _freeze_matchup_inputs(review, actor)
    for _attempt in range(_MAX_REWIND_RETRIES):
        try:
            _correct_finals_result_transaction(database, context, review, snapshot, actor, reason)
            return lifecycle.effective_result(matchup_id)
        except _StaleMatchupDuringDownstreamCheck:
            # The transaction context has already rolled back the inserted
            # result version and audit event. Restart the *whole* correction
            # transaction so downstream pairing/matchup IDs and the complete
            # deterministic lock set are discovered again.
            continue
    raise FinalsBracketAdvanceStateError(
        f"finals correction could not complete after {_MAX_REWIND_RETRIES} attempts, each racing a concurrent "
        "downstream matchup materialisation; reload and retry"
    )


def _correct_finals_result_transaction(database, context, review, snapshot, actor, reason):
    matchup_id = context["matchup_id"]
    old_version = context["effective_official_version"]
    bracket_repo = FinalsBracketRepository(database)
    source_ids = (
        bracket_repo._source_matchup_ids(context["bracket_id"], context["week_number"])
        if context["week_number"] < 4
        else {matchup_id}
    )
    with transaction(database) as conn:
        # Issue #195's shared completed-season write fence -- locked first,
        # ahead of every matchup row below (see `publish_finals_round`'s
        # identical rationale).
        SeasonRepository(database).guard_writable(conn, context["season_id"])
        locked_matchups = {}
        for source_id in sorted(source_ids):
            locked_matchups[source_id] = conn.execute(
                "SELECT * FROM bbbffl_matchup WHERE matchup_id=?" + _for_update_suffix(database),
                (source_id,),
            ).fetchone()
        locked = locked_matchups[matchup_id]
        calculation = conn.execute(
            "SELECT revision,input_fingerprint FROM bbbffl_matchup_calculation WHERE matchup_id=?",
            (matchup_id,),
        ).fetchone()
        if (
            locked["effective_official_version"] != old_version
            or locked["review_version"] != review.review_version
            or calculation["revision"] != snapshot["calculation_revision"]
            or calculation["input_fingerprint"] != snapshot["calculation_fingerprint"]
        ):
            raise StaleFinalsSnapshotError("finals result, calculation, or review changed during correction")
        version, now, correlation = old_version + 1, _now(), new_correlation_id()
        conn.execute(
            "INSERT INTO bbbffl_official_result VALUES (?,?,?,?,?,?,?,?)",
            (
                matchup_id,
                version,
                review.home.effective_score,
                review.away.effective_score,
                now,
                actor.actor_id,
                reason,
                json.dumps(snapshot, sort_keys=True, default=str),
            ),
        )
        conn.execute(
            "UPDATE bbbffl_matchup SET effective_official_version=?,review_version=review_version+1 WHERE matchup_id=?",
            (version, matchup_id),
        )
        append_event(
            conn,
            actor=actor,
            action="finals.result.corrected",
            entity_type="competition.matchup",
            entity_id=matchup_id,
            entity_version=str(version),
            correlation_id=correlation,
            reason=reason,
            before_state={"official_version": old_version},
            after_state={"official_version": version},
        )
        if context["week_number"] < 4:
            expected_versions = {
                source_id: (version if source_id == matchup_id else row["effective_official_version"])
                for source_id, row in locked_matchups.items()
            }
            bracket_repo.rewind_bracket_in_transaction(
                conn,
                context["bracket_id"],
                context["week_number"],
                actor=actor,
                reason=reason,
                expected_versions=expected_versions,
            )
        else:
            old = conn.execute(
                "SELECT home_score,away_score FROM bbbffl_official_result WHERE matchup_id=? AND version=?",
                (matchup_id, old_version),
            ).fetchone()
            old_premier = _premier_for_scores(
                conn,
                context["bracket_id"],
                locked["home_season_entry_id"],
                locked["away_season_entry_id"],
                old["home_score"],
                old["away_score"],
            )
            new_premier = _premier(conn, context["bracket_id"], review)
            append_event(
                conn,
                actor=actor,
                action="finals.premier.recorded",
                entity_type="season.entry",
                entity_id=new_premier,
                entity_version=str(version),
                correlation_id=correlation,
                reason=reason,
                before_state={"season_entry_id": old_premier, "official_version": old_version},
                after_state={"season_entry_id": new_premier, "official_version": version},
                payload={"grand_final_matchup_id": matchup_id, "supersedes_official_version": old_version},
            )
