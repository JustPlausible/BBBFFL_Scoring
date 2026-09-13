"""Finals-only review/publication adapter with variable matchup counts."""

import json
from contextlib import nullcontext

from app.audit import append_event, new_correlation_id
from app.calculations import MatchupCalculationService
from app.db import _for_update_suffix, transaction
from app.finals import FinalsBracketRepository
from app.round_review import SignoffValidationError, _freeze_matchup_inputs, build_matchup_review
from app.season import _now

EXPECTED_MATCH_COUNTS = {1: 2, 2: 2, 3: 1, 4: 1}


class StaleFinalsSnapshotError(RuntimeError):
    pass


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
            if grand.home.effective_score == grand.away.effective_score:
                ranks = conn.execute(
                    "SELECT season_entry_id,seed_position FROM finals_bracket_seed WHERE bracket_id=? "
                    "AND season_entry_id IN (?,?)",
                    (bracket["bracket_id"], grand.home.season_entry_id, grand.away.season_entry_id),
                ).fetchall()
                premier = min(ranks, key=lambda row: row["seed_position"])["season_entry_id"]
            else:
                premier = (
                    grand.home.season_entry_id
                    if grand.home.effective_score > grand.away.effective_score
                    else grand.away.season_entry_id
                )
            mathematical = conn.execute(
                "SELECT mr.season_entry_id, s.snapshot_id, s.through_round "
                "FROM finals_bracket b JOIN finals_seeding_snapshot s "
                "ON s.snapshot_id=b.finals_seeding_snapshot_id "
                "JOIN finals_seeding_snapshot_mathematical_row mr ON mr.snapshot_id=s.snapshot_id "
                "WHERE b.bracket_id=? AND mr.rank=10",
                (bracket["bracket_id"],),
            ).fetchone()
            if mathematical is None:
                raise ValueError("Grand Final cannot publish without a frozen mathematical ladder wooden-spoon fact")
            spoon = mathematical["season_entry_id"]
            for action, entry in (("finals.premier.recorded", premier), ("finals.wooden_spoon.recorded", spoon)):
                provenance = (
                    {
                        "finals_seeding_snapshot_id": mathematical["snapshot_id"],
                        "through_round": mathematical["through_round"],
                    }
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
        MatchupCalculationService(database, afl_client).calculate_matchup(matchup_id)
        fresh_fn = getattr(evidence, "is_evidence_fresh", None)
        fresh = fresh_fn() if callable(fresh_fn) else True
        matchup = lifecycle.get_matchup(matchup_id)
        review = build_matchup_review(lifecycle, review_repo, identities, matchup, evidence_fresh=fresh)
    if review.blockers:
        raise SignoffValidationError({matchup_id: review.blockers})
    snapshot = _freeze_matchup_inputs(review, actor)
    with transaction(database) as conn:
        locked = conn.execute(
            "SELECT m.*, c.revision calculation_revision, c.input_fingerprint calculation_fingerprint "
            "FROM bbbffl_matchup m JOIN bbbffl_matchup_calculation c ON c.matchup_id=m.matchup_id "
            "WHERE m.matchup_id=?" + _for_update_suffix(database),
            (matchup_id,),
        ).fetchone()
        if (
            locked["effective_official_version"] != old_version
            or locked["review_version"] != review.review_version
            or locked["calculation_revision"] != snapshot["calculation_revision"]
            or locked["calculation_fingerprint"] != snapshot["calculation_fingerprint"]
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
        bracket = FinalsBracketRepository(database)
        preview = bracket.rewind_bracket(
            context["bracket_id"], context["week_number"], actor=actor, reason=reason, apply=False
        )
        bracket.rewind_bracket(
            context["bracket_id"],
            context["week_number"],
            actor=actor,
            reason=reason,
            apply=True,
            expected_versions=preview["expected_versions"],
        )
    return lifecycle.effective_result(matchup_id)
