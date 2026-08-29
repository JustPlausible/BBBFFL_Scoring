"""Season-aware live calculation over persisted fixtures and submissions.

This module derives replaceable calculated state only.  It neither reads nor
writes ``bbbffl_official_result`` and makes AFL requests solely through the
public ``AflApiClient`` interface.

## Per-slot scoring-source resolution (issue #69)

Every slot in an ordinary lineup draws its AFL facts from the same round's
mapped AFL matches (`context["afl_round_id"]`). A slot with an active
Opening Round deferred nomination (`app.opening_round`) is the one
exception: `_deferred_positions` looks up that slot's nomination (if any)
and, when present, resolves its match/stats from the nomination's own AFL
Opening Round instead -- via `_RoundFacts`, a small per-round match/stat
cache keyed by AFL round ID so a round with mixed sources fetches each
distinct AFL round's matches/stats at most once. The *same* `score_position`
formula is then applied to whichever facts were resolved; there is no
separate Opening Round scoring engine (see docs/opening-round-deferred-
selection.md). A deferred slot's participation is deliberately assessed with
`bye_team_ids=None` -- the player's club is, by construction, on its
ordinary bye in the *target* round, which must never be misreported as an
"ordinary bye" now that real Opening Round statistics are available for it.
"""

import dataclasses
import hashlib
import json
from dataclasses import asdict, dataclass

from app.afl_client import AflApiError
from app.db import transaction
from app.participation import assess_participation
from app.scoring import PlayerStats, ScoringRules, score_position
from app.season import _now

ENGINE_VERSION = "bbbffl-core-v1"
POSITION_MAP = {
    "F1": "Forward1",
    "F2": "Forward2",
    "F3": "Forward3",
    "M1": "Midfield1",
    "M2": "Midfield2",
    "M3": "Midfield3",
    "Ruck": "Ruck",
    "Tackler": "Tackler",
}


@dataclass(frozen=True)
class CalculatedMatchup:
    matchup_id: str
    revision: int
    input_fingerprint: str
    snapshot: dict


class _RoundFacts:
    """Per-AFL-round match/stat cache, keyed by AFL round ID. Ordinary slots
    resolve against `default_afl_round_id`; a slot with an active Opening
    Round deferred nomination resolves against its own nomination's AFL
    Opening Round instead (`matches(afl_round_id=...)`). Each distinct AFL
    round's matches/stats are fetched from `afl_client` at most once,
    however many slots/matchups in this round need them."""

    def __init__(self, afl_client, default_afl_round_id):
        self._afl_client = afl_client
        self.default_afl_round_id = default_afl_round_id
        self._matches_by_round: dict[int, list] = {}
        self._stats_by_match: dict[int, dict] = {}

    def matches(self, afl_round_id=None):
        afl_round_id = self.default_afl_round_id if afl_round_id is None else afl_round_id
        if afl_round_id not in self._matches_by_round:
            self._matches_by_round[afl_round_id] = self._afl_client.get_matches(afl_round_id)
        return self._matches_by_round[afl_round_id]

    def stats_for(self, match_id):
        if match_id not in self._stats_by_match:
            self._stats_by_match[match_id] = self._afl_client.get_match_player_stats(match_id)
        return self._stats_by_match[match_id]


class MatchupCalculationService:
    def __init__(self, database, afl_client):
        self.database = database
        self.afl_client = afl_client

    def calculate_round(self, round_id, *, upstream_revision=None, observed_at=None):
        context = self._round_context(round_id)
        round_facts = _RoundFacts(self.afl_client, context["afl_round_id"])
        facts = (round_facts, self._bye_team_ids(context))
        return [
            self._calculate(row, context, facts, upstream_revision, observed_at) for row in self._matchups(round_id)
        ]

    def calculate_matchup(self, matchup_id, *, upstream_revision=None, observed_at=None):
        row = self.database.execute("SELECT * FROM bbbffl_matchup WHERE matchup_id=?", (matchup_id,)).fetchone()
        if not row:
            raise KeyError(matchup_id)
        context = self._round_context(row["bbbffl_round_id"])
        round_facts = _RoundFacts(self.afl_client, context["afl_round_id"])
        facts = (round_facts, self._bye_team_ids(context))
        return self._calculate(row, context, facts, upstream_revision, observed_at)

    def _bye_team_ids(self, context):
        """The AFL clubs on an ordinary bye for this round, if the configured
        `afl_client` can report byes at all (`get_rounds` is optional --
        the `Facts` test double used throughout `tests/test_calculations.py`
        does not implement it, and omitting it must not fail calculation).
        Feeds `app.participation.assess_participation` so the scorer round
        review (issue #58) can tell a club-bye Interchange/starter apart
        from genuinely ambiguous evidence without a second afl-api round
        trip at review time."""
        get_rounds = getattr(self.afl_client, "get_rounds", None)
        if not callable(get_rounds):
            return None
        try:
            rounds = get_rounds(context["afl_season_id"])
        except (AflApiError, KeyError, TypeError, ValueError):
            # Bye evidence is advisory only (see the docstring above): an
            # afl-api failure here must degrade to "unknown byes", never
            # block or corrupt the authoritative calculated score.
            return None
        for round_ in rounds:
            if round_.round_id == context["afl_round_id"]:
                return None if round_.byes is None else frozenset(team.team_id for team in round_.byes)
        return None

    def _calculate(self, matchup, context, facts, upstream_revision, observed_at):
        rules = ScoringRules.from_dict(json.loads(context["scoring_rules"]) if context["scoring_rules"] else None)
        home = self._entry(matchup["home_season_entry_id"], context, facts, rules)
        away = self._entry(matchup["away_season_entry_id"], context, facts, rules)
        observed_at = observed_at or _now()
        snapshot = {
            "engine_version": ENGINE_VERSION,
            "season_id": context["season_id"],
            "rules_version_id": context["rules_version_id"],
            "bbbffl_round_id": context["bbbffl_round_id"],
            "matchup_id": matchup["matchup_id"],
            "upstream": {
                "provider": context["provider"],
                "afl_season_id": context["afl_season_id"],
                "afl_round_id": context["afl_round_id"],
                "revision": upstream_revision,
                "observed_at": observed_at,
            },
            "home": home,
            "away": away,
        }
        fingerprint_input = dict(snapshot)
        # Provider observation identifiers remain diagnostic provenance.  The
        # idempotency key is the actual authoritative facts, so a round-wide
        # revision label does not churn four unaffected matchups.
        fingerprint_input["upstream"] = {**snapshot["upstream"], "observed_at": None, "revision": None}
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_input, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        revision = self._persist(matchup, context, home, away, snapshot, fingerprint, upstream_revision, observed_at)
        return CalculatedMatchup(matchup["matchup_id"], revision, fingerprint, snapshot)

    def _deferred_positions(self, bbbffl_round_id, season_entry_id):
        """`{position: {"season_player_id": ..., "afl_opening_round_id": ...,
        "rule_id": ..., "source_afl_match_id": ...}}` for every current
        Opening Round deferred nomination targeting this round/entry
        (`app.opening_round`). A round/season that never configured an
        Opening Round rule -- the overwhelming common case -- has no rows
        here at all, so this adds one cheap, always-safe query rather than
        a required dependency.

        `season_player_id` is included specifically so `_entry()` can
        confirm the *actually submitted* player still matches the
        nomination before treating a slot as deferred -- a nomination
        naming a position is not, by itself, proof that the player
        currently occupying it is the nominated one (the nomination could
        have been corrected after submission, or the submission could have
        bypassed `app.opening_round.OpeningRoundSelectionGuard`)."""
        rows = self.database.execute(
            "SELECT n.position, n.season_player_id, n.source_afl_match_id, rev.afl_opening_round_id "
            "FROM opening_round_nomination n "
            "JOIN opening_round_rule r ON r.rule_id=n.rule_id "
            "JOIN opening_round_rule_revision rev ON rev.rule_id=r.rule_id AND rev.revision=r.current_revision "
            "WHERE n.bbbffl_round_id=? AND n.season_entry_id=? AND rev.state='accepted'",
            (bbbffl_round_id, season_entry_id),
        ).fetchall()
        return {row["position"]: dict(row) for row in rows}

    def _entry(self, entry_id, context, facts, rules):
        lineup = self.database.execute(
            "SELECT * FROM weekly_lineup WHERE season_id=? AND competition_id=? AND bbbffl_round_id=? AND season_entry_id=?",
            (context["season_id"], context["competition_id"], context["bbbffl_round_id"], entry_id),
        ).fetchone()
        if not lineup or lineup["effective_submission_version"] is None:
            raise ValueError(f"entry {entry_id} has no effective submitted lineup")
        version = lineup["effective_submission_version"]
        slots = self.database.execute(
            "SELECT s.position, s.season_player_id, p.canonical_player_id, p.afl_team_id FROM weekly_lineup_submission_slot s LEFT JOIN season_player_pool p ON p.season_player_id=s.season_player_id WHERE s.lineup_id=? AND s.version=? ORDER BY s.position",
            (lineup["lineup_id"], version),
        ).fetchall()
        evidence, total = [], 0
        round_facts, bye_team_ids = facts
        deferred_positions = self._deferred_positions(context["bbbffl_round_id"], entry_id)
        interchange_raw = None
        for slot in slots:
            nomination = deferred_positions.get(slot["position"])
            deferred = nomination is not None and nomination["season_player_id"] == slot["season_player_id"]
            mismatch = nomination is not None and not deferred
            if mismatch:
                # A nomination exists for this slot but names a different
                # player than is currently submitted -- never guess which
                # source applies (see `_deferred_positions`'s docstring).
                # Empty `matches` makes the resolution below fall through to
                # `match=None`/`stat=None`, so nothing is silently scored
                # from either source.
                matches, slot_bye_team_ids = [], None
            elif deferred:
                # Per-slot source override (issue #69): this slot's stats
                # come from the player's AFL Opening Round match, never from
                # the round's ordinarily-mapped AFL round -- see this
                # module's docstring. Every other slot is unaffected.
                matches = round_facts.matches(nomination["afl_opening_round_id"])
                slot_bye_team_ids = None
            else:
                matches = round_facts.matches()
                slot_bye_team_ids = bye_team_ids
            match = next(
                (m for m in matches if slot["afl_team_id"] is not None and m.involves_team(slot["afl_team_id"])), None
            )
            stat = (
                round_facts.stats_for(match.match_id).get(slot["canonical_player_id"])
                if match and slot["canonical_player_id"]
                else None
            )
            raw = asdict(stat) if stat else None
            score = None
            if slot["position"] != "Interchange":
                if stat is None:
                    score = 0
                else:
                    score = score_position(
                        POSITION_MAP[slot["position"]],
                        PlayerStats(**{key: value for key, value in raw.items() if key != "canonical_player_id"}),
                        rules,
                    )
                if score is not None:
                    total += score
            elif raw is not None:
                interchange_raw = raw
            participation = assess_participation(
                afl_team_id=slot["afl_team_id"],
                bye_team_ids=slot_bye_team_ids,
                match=match,
                stat_line=stat,
            )
            if mismatch:
                # The submission diverged from the locked nomination (see
                # above) -- always review_required, never scored from
                # either source, whatever afl-api evidence happens to say
                # about this player's own club this round.
                participation = dataclasses.replace(
                    participation,
                    source="opening-round-deferred-mismatch",
                    reason=(
                        "An Opening Round deferred nomination exists for this slot naming a different "
                        f"player ({nomination['season_player_id']}) than is currently submitted "
                        f"({slot['season_player_id']}); scorer review required before scoring."
                    ),
                )
            elif deferred:
                # Tag provenance (`source`) and, when the Opening Round
                # evidence itself could not resolve a match/stat line for
                # this player (`state == "unknown"`), replace the generic
                # reason with one naming the deferred nomination explicitly
                # -- issue #69's "missing/invalid/unresolved deferred
                # evidence requiring scorer review" must read as such, not
                # as an ordinary ambiguous-availability case.
                reason = participation.reason
                if participation.state.value == "unknown":
                    reason = (
                        "Opening Round deferred nomination is recorded, but AFL Opening Round evidence "
                        f"did not resolve a match/stat line for this player (AFL round "
                        f"{nomination['afl_opening_round_id']}); scorer review required."
                    )
                participation = dataclasses.replace(participation, source="opening-round-deferred", reason=reason)
            evidence.append(
                {
                    "position": slot["position"],
                    "season_player_id": slot["season_player_id"],
                    "canonical_player_id": slot["canonical_player_id"],
                    "afl_match_id": match.match_id if match else None,
                    "played": stat is not None,
                    "stats": raw,
                    "score": score,
                    "interchange_available": slot["position"] == "Interchange",
                    "scoring_source": (
                        "opening_round_deferred"
                        if deferred
                        else "opening_round_nomination_mismatch"
                        if mismatch
                        else "ordinary"
                    ),
                    "source_afl_round_id": (
                        nomination["afl_opening_round_id"] if deferred else context["afl_round_id"]
                    ),
                    "participation": {
                        "state": participation.state.value,
                        "dnp_recommendation": participation.dnp_recommendation.value,
                        "reason": participation.reason,
                    },
                }
            )
        # What the Interchange's *current* AFL stats would score at each
        # scorable position -- informational only, mirrors
        # app.service.InterchangePotentialScores for the Grand Final
        # vertical. Never added to `total`; a scorer must record an
        # explicit interchange ruling (see app.round_review) before this
        # replaces anything in an official result.
        interchange_potential_scores = (
            {
                slot_name: score_position(
                    target,
                    PlayerStats(**{k: v for k, v in interchange_raw.items() if k != "canonical_player_id"}),
                    rules,
                )
                for slot_name, target in POSITION_MAP.items()
            }
            if interchange_raw is not None
            else None
        )
        return {
            "season_entry_id": entry_id,
            "lineup_id": lineup["lineup_id"],
            "lineup_version": version,
            "score": total,
            "slots": evidence,
            "interchange_potential_scores": interchange_potential_scores,
        }

    def _persist(self, matchup, context, home, away, snapshot, fingerprint, upstream_revision, observed_at):
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        with transaction(self.database) as conn:
            # A missing row cannot be protected by SELECT FOR UPDATE.  This
            # upsert makes first-write creation and revision comparison one
            # PostgreSQL operation: identical contenders retain revision 1;
            # different facts serialize and advance it monotonically.
            row = conn.execute(
                "INSERT INTO bbbffl_matchup_calculation "
                "(matchup_id,revision,snapshot,input_fingerprint,season_id,rules_version_id,bbbffl_round_id,home_season_entry_id,away_season_entry_id,home_lineup_id,home_lineup_version,away_lineup_id,away_lineup_version,upstream_revision,upstream_observed_at,engine_version,updated_at) "
                "VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (matchup_id) DO UPDATE SET "
                "revision=CASE WHEN bbbffl_matchup_calculation.input_fingerprint=excluded.input_fingerprint THEN bbbffl_matchup_calculation.revision ELSE bbbffl_matchup_calculation.revision+1 END, "
                "snapshot=CASE WHEN bbbffl_matchup_calculation.input_fingerprint=excluded.input_fingerprint THEN bbbffl_matchup_calculation.snapshot ELSE excluded.snapshot END, "
                "input_fingerprint=excluded.input_fingerprint, season_id=excluded.season_id, rules_version_id=excluded.rules_version_id, bbbffl_round_id=excluded.bbbffl_round_id, "
                "home_season_entry_id=excluded.home_season_entry_id, away_season_entry_id=excluded.away_season_entry_id, home_lineup_id=excluded.home_lineup_id, home_lineup_version=excluded.home_lineup_version, "
                "away_lineup_id=excluded.away_lineup_id, away_lineup_version=excluded.away_lineup_version, upstream_revision=excluded.upstream_revision, upstream_observed_at=excluded.upstream_observed_at, engine_version=excluded.engine_version, updated_at=excluded.updated_at "
                "RETURNING revision",
                (
                    matchup["matchup_id"],
                    encoded,
                    fingerprint,
                    context["season_id"],
                    context["rules_version_id"],
                    context["bbbffl_round_id"],
                    home["season_entry_id"],
                    away["season_entry_id"],
                    home["lineup_id"],
                    home["lineup_version"],
                    away["lineup_id"],
                    away["lineup_version"],
                    upstream_revision,
                    observed_at,
                    ENGINE_VERSION,
                    _now(),
                ),
            ).fetchone()
        return row["revision"]

    def _round_context(self, round_id):
        row = self.database.execute(
            "SELECT l.*, c.rules_version_id, v.scoring_rules FROM bbbffl_round_lifecycle l JOIN competition_stream c ON c.competition_id=l.competition_id JOIN season_rules_version v ON v.rules_version_id=c.rules_version_id WHERE l.bbbffl_round_id=?",
            (round_id,),
        ).fetchone()
        if not row:
            raise KeyError(round_id)
        return row

    def _matchups(self, round_id):
        return self.database.execute(
            "SELECT * FROM bbbffl_matchup WHERE bbbffl_round_id=? ORDER BY matchup_order", (round_id,)
        ).fetchall()
