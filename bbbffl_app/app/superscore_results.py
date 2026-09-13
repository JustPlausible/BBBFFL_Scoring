"""Season-model SuperScore calculation and atomic leaderboard publication.

Unlike ordinary/finals scoring this aggregate is round/entry keyed and never
creates or consumes a matchup.  The always-present review-state rows are both
the review CAS authority and the mutex covering every evidence read through
calculation persistence.
"""

import hashlib
import json
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from types import SimpleNamespace

from app.audit import ActorContext, append_event, new_correlation_id
from app.calculations import ENGINE_VERSION, MatchupCalculationService, _RoundFacts
from app.db import _for_update_suffix, transaction
from app.round_review import _side_review
from app.scoring import ScoringRules
from app.season import SeasonCompletedError, SeasonRepository, _now


class SuperScoreResultError(RuntimeError):
    pass


class StaleSuperScoreCalculationError(SuperScoreResultError):
    pass


class StaleSuperScorePublicationError(SuperScoreResultError):
    pass


class StaleSuperScoreEvidenceError(SuperScoreResultError):
    pass


# Issue #195: this used to be a bespoke, locally-defined check (`if
# season["lifecycle_state"] == "completed": raise CompletedSeasonError(...)`,
# a `SELECT ... FOR UPDATE` on `bbbffl_season` with no shared contract with
# any other module). It is now a plain alias for the one shared completed-
# season write-fence error every other result-changing path raises
# (`app.season.SeasonRepository.guard_writable`) -- kept under this name so
# `app/routes/superscore_results.py`'s existing `except CompletedSeasonError`
# -> HTTP 423 mapping, and any other importer of this name, need no change.
CompletedSeasonError = SeasonCompletedError


class MissingCorrectionReasonError(SuperScoreResultError):
    pass


@dataclass(frozen=True)
class CalculatedEntry:
    bbbffl_round_id: str
    season_entry_id: str
    revision: int
    input_fingerprint: str
    computed_as_of_review_version: int
    total_score: float
    snapshot: dict


def _effective_entry(conn, round_id, entry_id, raw, identities=None):
    dnp = conn.execute(
        "SELECT * FROM superscore_entry_slot_ruling WHERE bbbffl_round_id=? AND season_entry_id=?",
        (round_id, entry_id),
    ).fetchall()
    interchange = conn.execute(
        "SELECT * FROM superscore_entry_interchange_ruling WHERE bbbffl_round_id=? AND season_entry_id=?",
        (round_id, entry_id),
    ).fetchone()
    overrides = conn.execute(
        "SELECT * FROM superscore_entry_override WHERE bbbffl_round_id=? AND season_entry_id=?",
        (round_id, entry_id),
    ).fetchall()
    side, blockers = _side_review(
        entry_id,
        raw,
        {(r["season_entry_id"], r["slot"]): SimpleNamespace(dnp=bool(r["dnp"])) for r in dnp},
        ({entry_id: SimpleNamespace(target_position=interchange["target_position"])} if interchange else {}),
        {
            (r["season_entry_id"], r["position"]): SimpleNamespace(
                override_score=float(r["override_score"]), reason=r["reason"]
            )
            for r in overrides
        },
        identities,
    )
    return (
        side,
        blockers,
        {
            "dnp": [dict(r) for r in dnp],
            "interchange": dict(interchange) if interchange else None,
            "overrides": [dict(r) for r in overrides],
        },
    )


class SuperScoreCalculationService(MatchupCalculationService):
    """A sibling scoring service which only reuses the ordinary pure entry
    evidence/scoring implementation; it deliberately does not invoke the
    matchup calculation or persistence path."""

    def calculate_entry(self, round_id, season_entry_id):
        return self._calculate_entries(round_id, [season_entry_id])[0]

    def calculate_round(self, round_id):
        entry_ids = [
            row["season_entry_id"]
            for row in self.database.execute(
                "SELECT season_entry_id FROM superscore_entry_review_state WHERE bbbffl_round_id=? ORDER BY season_entry_id",
                (round_id,),
            ).fetchall()
        ]
        if len(entry_ids) != 10:
            raise SuperScoreResultError("a SuperScore calculation requires exactly ten review-state rows")
        return self._calculate_entries(round_id, entry_ids)

    def _calculate_entries(self, round_id, entry_ids):
        ordered = sorted(entry_ids)
        with transaction(self.database) as conn:
            states = {}
            # All locks precede context, lineup, ruling, or AFL evidence reads.
            for entry_id in ordered:
                row = conn.execute(
                    "SELECT review_version FROM superscore_entry_review_state "
                    "WHERE bbbffl_round_id=? AND season_entry_id=?" + _for_update_suffix(self.database),
                    (round_id, entry_id),
                ).fetchone()
                if row is None:
                    raise SuperScoreResultError(f"missing SuperScore review state for entry {entry_id}")
                states[entry_id] = row["review_version"]
            context = self._round_context(conn, round_id)
            stream = conn.execute(
                "SELECT stream_type FROM competition_stream WHERE competition_id=?", (context["competition_id"],)
            ).fetchone()
            if stream is None or stream["stream_type"] != "superscore":
                raise SuperScoreResultError("round is not a SuperScore round")
            # Constructing the shared cache only after every deterministic lock
            # is intentional: cached old facts can never overwrite a newer
            # single-entry calculation.
            facts = (_RoundFacts(self.afl_client, context["afl_round_id"]), self._bye_team_ids(context))
            rules = ScoringRules.from_dict(json.loads(context["scoring_rules"]) if context["scoring_rules"] else None)
            results = []
            for entry_id in entry_ids:
                raw = self._entry(conn, entry_id, context, facts, rules)
                effective, blockers, rulings = _effective_entry(conn, round_id, entry_id, raw)
                review_version = states[entry_id]
                snapshot = {
                    "engine_version": ENGINE_VERSION,
                    "season_id": context["season_id"],
                    "rules_version_id": context["rules_version_id"],
                    "bbbffl_round_id": round_id,
                    "season_entry_id": entry_id,
                    "computed_as_of_review_version": review_version,
                    "entry": raw,
                    "effective_entry": asdict(effective),
                    "review_blockers": blockers,
                    "rulings": rulings,
                    "upstream": {
                        "provider": context["provider"],
                        "afl_season_id": context["afl_season_id"],
                        "afl_round_id": context["afl_round_id"],
                    },
                }
                fingerprint = hashlib.sha256(
                    json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()
                ).hexdigest()
                current = conn.execute(
                    "SELECT review_version FROM superscore_entry_review_state WHERE bbbffl_round_id=? AND season_entry_id=?",
                    (round_id, entry_id),
                ).fetchone()
                if current is None or current["review_version"] != review_version:
                    raise StaleSuperScoreCalculationError("SuperScore inputs changed during calculation")
                encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
                row = conn.execute(
                    "INSERT INTO superscore_entry_calculation "
                    "(bbbffl_round_id,season_entry_id,revision,input_fingerprint,computed_as_of_review_version,total_score,snapshot,updated_at) "
                    "VALUES (?,?,1,?,?,?,?,?) ON CONFLICT (bbbffl_round_id,season_entry_id) DO UPDATE SET "
                    "revision=CASE WHEN superscore_entry_calculation.input_fingerprint=excluded.input_fingerprint "
                    "THEN superscore_entry_calculation.revision ELSE superscore_entry_calculation.revision+1 END, "
                    "input_fingerprint=excluded.input_fingerprint, computed_as_of_review_version=excluded.computed_as_of_review_version, "
                    "total_score=excluded.total_score, snapshot=excluded.snapshot, updated_at=excluded.updated_at RETURNING revision",
                    (round_id, entry_id, fingerprint, review_version, effective.effective_score, encoded, _now()),
                ).fetchone()
                results.append(
                    CalculatedEntry(
                        round_id,
                        entry_id,
                        row["revision"],
                        fingerprint,
                        review_version,
                        effective.effective_score,
                        snapshot,
                    )
                )
            return results


class SuperScoreLeaderboardService:
    def __init__(self, database, afl_client, identities=None):
        self.database = database
        self.afl_client = afl_client
        self.identities = identities
        self.calculations = SuperScoreCalculationService(database, afl_client)

    def publish(self, round_id, *, actor: ActorContext, reason: str | None = None):
        evidence_batch = getattr(self.afl_client, "evidence_batch", None)
        scope = evidence_batch() if callable(evidence_batch) else nullcontext(self.afl_client)
        with scope as evidence:
            calculations = self.calculations.calculate_round(round_id)
            fresh = getattr(evidence, "is_evidence_fresh", None)
            if callable(fresh) and not fresh():
                raise StaleSuperScoreEvidenceError("AFL evidence batch was not fresh; leaderboard was not published")
            assembled = [self._assemble_entry(round_id, calculation) for calculation in calculations]
            return self._persist(round_id, assembled, actor, reason)

    def _assemble_entry(self, round_id, calculation):
        entry_id = calculation.season_entry_id
        blockers = calculation.snapshot["review_blockers"]
        if blockers:
            raise SuperScoreResultError("; ".join(blockers))
        frozen = {
            "calculation_revision": calculation.revision,
            "calculation_fingerprint": calculation.input_fingerprint,
            "computed_as_of_review_version": calculation.computed_as_of_review_version,
            "rules_version_id": calculation.snapshot["rules_version_id"],
            "calculation": calculation.snapshot,
            "effective_entry": calculation.snapshot["effective_entry"],
            "rulings": calculation.snapshot["rulings"],
        }
        return {"entry_id": entry_id, "score": calculation.total_score, "calculation": calculation, "snapshot": frozen}

    def _persist(self, round_id, assembled, actor, reason):
        with transaction(self.database) as conn:
            # Issue #195's shared completed-season write fence: resolve the
            # owning season with an unlocked read (the round/competition/
            # season relationship is immutable) and lock/guard it *before*
            # this transaction locks the round/review-state rows below --
            # locking the season row first, ahead of every other lock here,
            # is what lets this transaction and `app.season_completion.
            # complete_season` serialize purely through that one lock.
            season_lookup = conn.execute(
                "SELECT c.season_id FROM bbbffl_round r JOIN competition_stream c ON c.competition_id=r.competition_id "
                "WHERE r.bbbffl_round_id=? AND c.stream_type='superscore'",
                (round_id,),
            ).fetchone()
            if season_lookup is None:
                raise SuperScoreResultError("round is not a SuperScore round")
            SeasonRepository(self.database).guard_writable(conn, season_lookup["season_id"])
            context = conn.execute(
                "SELECT r.competition_id,c.season_id,l.state FROM bbbffl_round r "
                "JOIN competition_stream c ON c.competition_id=r.competition_id "
                "JOIN bbbffl_round_lifecycle l ON l.bbbffl_round_id=r.bbbffl_round_id "
                "WHERE r.bbbffl_round_id=? AND c.stream_type='superscore'" + _for_update_suffix(self.database),
                (round_id,),
            ).fetchone()
            if context is None or context["state"] not in ("review", "final"):
                raise SuperScoreResultError("SuperScore round must be in review or final state")
            locked_states = {}
            for item in sorted(assembled, key=lambda i: i["entry_id"]):
                entry_id = item["entry_id"]
                locked_states[entry_id] = conn.execute(
                    "SELECT review_version FROM superscore_entry_review_state WHERE bbbffl_round_id=? AND season_entry_id=?"
                    + _for_update_suffix(self.database),
                    (round_id, entry_id),
                ).fetchone()
            if len(locked_states) != 10 or any(row is None for row in locked_states.values()):
                raise StaleSuperScorePublicationError("complete ten-entry review state is required")
            for item in assembled:
                calc = item["calculation"]
                current_calc = conn.execute(
                    "SELECT revision,input_fingerprint,computed_as_of_review_version FROM superscore_entry_calculation "
                    "WHERE bbbffl_round_id=? AND season_entry_id=?" + _for_update_suffix(self.database),
                    (round_id, item["entry_id"]),
                ).fetchone()
                state_version = locked_states[item["entry_id"]]["review_version"]
                if state_version != calc.computed_as_of_review_version:
                    raise StaleSuperScorePublicationError("review state changed after calculation")
                if (
                    current_calc is None
                    or current_calc["revision"] != calc.revision
                    or current_calc["input_fingerprint"] != calc.input_fingerprint
                ):
                    raise StaleSuperScorePublicationError("calculation changed while publication was assembled")
            version_row = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS version FROM superscore_leaderboard_revision WHERE bbbffl_round_id=?",
                (round_id,),
            ).fetchone()
            version, now = version_row["version"], _now()
            if version > 1 and (reason is None or not reason.strip()):
                raise MissingCorrectionReasonError("a non-empty reason is required to correct a leaderboard")
            conn.execute(
                "INSERT INTO superscore_leaderboard_revision VALUES (?,?,?,?,?,?,?)",
                (round_id, version, now, actor.actor_type, actor.actor_id, actor.actor_role, reason),
            )
            scores = [item["score"] for item in assembled]
            high = max(scores)
            joint = sum(item["score"] == high for item in assembled) > 1
            for item in assembled:
                # Standard competition ranking: 1, 1, 3 (the established
                # legacy SuperScore presentation rule), not dense ranking.
                rank = 1 + sum(score > item["score"] for score in scores)
                frozen = {
                    **item["snapshot"],
                    "publication": {
                        "leaderboard_version": version,
                        "published_at": now,
                        "published_by_type": actor.actor_type,
                        "published_by": actor.actor_id,
                        "published_by_role": actor.actor_role,
                        "reason": reason,
                    },
                }
                conn.execute(
                    "INSERT INTO superscore_official_result VALUES (?,?,?,?,?,?,?)",
                    (
                        round_id,
                        version,
                        item["entry_id"],
                        item["score"],
                        rank,
                        int(joint and item["score"] == high),
                        json.dumps(frozen, sort_keys=True, default=str),
                    ),
                )
            if context["state"] == "review":
                conn.execute(
                    "UPDATE bbbffl_round_lifecycle SET state='final',version=version+1,updated_at=? WHERE bbbffl_round_id=?",
                    (now, round_id),
                )
            append_event(
                conn,
                actor=actor,
                action="superscore.leaderboard.published" if version == 1 else "superscore.leaderboard.corrected",
                entity_type="superscore.leaderboard",
                entity_id=round_id,
                entity_version=str(version),
                correlation_id=new_correlation_id(),
                reason=reason,
                before_state={"version": version - 1 or None},
                after_state={"version": version, "entries": 10},
            )
        return self.leaderboard(round_id, version=version, include_inputs=True)

    def leaderboard(self, round_id, *, version=None, include_inputs=False):
        if version is None:
            row = self.database.execute(
                "SELECT MAX(version) AS version FROM superscore_leaderboard_revision WHERE bbbffl_round_id=?",
                (round_id,),
            ).fetchone()
            version = row["version"] if row else None
        if version is None:
            return None
        header = self.database.execute(
            "SELECT * FROM superscore_leaderboard_revision WHERE bbbffl_round_id=? AND version=?", (round_id, version)
        ).fetchone()
        if header is None:
            return None
        rows = self.database.execute(
            "SELECT * FROM superscore_official_result WHERE bbbffl_round_id=? AND leaderboard_version=? ORDER BY rank,season_entry_id",
            (round_id, version),
        ).fetchall()
        team_names = {}
        if self.identities is not None:
            for result in rows:
                team = self.identities.get_public_team(result["season_entry_id"])
                team_names[result["season_entry_id"]] = team.team_name if team else None
        return {
            "kind": "superscore_leaderboard",
            **dict(header),
            "entries": [
                {
                    "season_entry_id": r["season_entry_id"],
                    "team_name": team_names.get(r["season_entry_id"]),
                    "total_score": float(r["total_score"]),
                    "rank": r["rank"],
                    "is_joint_winner": bool(r["is_joint_winner"]),
                    **({"input_snapshot": json.loads(r["input_snapshot"])} if include_inputs else {}),
                }
                for r in rows
            ],
        }

    def history(self, round_id, *, include_inputs=False):
        versions = self.database.execute(
            "SELECT version FROM superscore_leaderboard_revision WHERE bbbffl_round_id=? ORDER BY version", (round_id,)
        ).fetchall()
        return [self.leaderboard(round_id, version=row["version"], include_inputs=include_inputs) for row in versions]
