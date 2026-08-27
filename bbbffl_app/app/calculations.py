"""Season-aware live calculation over persisted fixtures and submissions.

This module derives replaceable calculated state only.  It neither reads nor
writes ``bbbffl_official_result`` and makes AFL requests solely through the
public ``AflApiClient`` interface.
"""

from dataclasses import asdict, dataclass
import hashlib
import json

from app.db import _for_update_suffix, transaction
from app.scoring import PlayerStats, ScoringRules, score_position
from app.season import _now

ENGINE_VERSION = "bbbffl-core-v1"
POSITION_MAP = {"F1": "Forward1", "F2": "Forward2", "F3": "Forward3", "M1": "Midfield1", "M2": "Midfield2", "M3": "Midfield3", "Ruck": "Ruck", "Tackler": "Tackler"}


@dataclass(frozen=True)
class CalculatedMatchup:
    matchup_id: str
    revision: int
    input_fingerprint: str
    snapshot: dict


class MatchupCalculationService:
    def __init__(self, database, afl_client):
        self.database = database
        self.afl_client = afl_client

    def calculate_round(self, round_id, *, upstream_revision=None, observed_at=None):
        context = self._round_context(round_id)
        matches = self.afl_client.get_matches(context["afl_round_id"])
        stats_by_match = {match.match_id: self.afl_client.get_match_player_stats(match.match_id) for match in matches}
        facts = (matches, stats_by_match)
        return [self._calculate(row, context, facts, upstream_revision, observed_at) for row in self._matchups(round_id)]

    def calculate_matchup(self, matchup_id, *, upstream_revision=None, observed_at=None):
        row = self.database.execute("SELECT * FROM bbbffl_matchup WHERE matchup_id=?", (matchup_id,)).fetchone()
        if not row:
            raise KeyError(matchup_id)
        context = self._round_context(row["bbbffl_round_id"])
        matches = self.afl_client.get_matches(context["afl_round_id"])
        facts = (matches, {match.match_id: self.afl_client.get_match_player_stats(match.match_id) for match in matches})
        return self._calculate(row, context, facts, upstream_revision, observed_at)

    def _calculate(self, matchup, context, facts, upstream_revision, observed_at):
        rules = ScoringRules.from_dict(json.loads(context["scoring_rules"]) if context["scoring_rules"] else None)
        home = self._entry(matchup["home_season_entry_id"], context, facts, rules)
        away = self._entry(matchup["away_season_entry_id"], context, facts, rules)
        observed_at = observed_at or _now()
        snapshot = {"engine_version": ENGINE_VERSION, "season_id": context["season_id"], "rules_version_id": context["rules_version_id"], "bbbffl_round_id": context["bbbffl_round_id"], "matchup_id": matchup["matchup_id"], "upstream": {"provider": context["provider"], "afl_season_id": context["afl_season_id"], "afl_round_id": context["afl_round_id"], "revision": upstream_revision, "observed_at": observed_at}, "home": home, "away": away}
        fingerprint_input = dict(snapshot)
        # Provider observation identifiers remain diagnostic provenance.  The
        # idempotency key is the actual authoritative facts, so a round-wide
        # revision label does not churn four unaffected matchups.
        fingerprint_input["upstream"] = {**snapshot["upstream"], "observed_at": None, "revision": None}
        fingerprint = hashlib.sha256(json.dumps(fingerprint_input, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        revision = self._persist(matchup, context, home, away, snapshot, fingerprint, upstream_revision, observed_at)
        return CalculatedMatchup(matchup["matchup_id"], revision, fingerprint, snapshot)

    def _entry(self, entry_id, context, facts, rules):
        lineup = self.database.execute("SELECT * FROM weekly_lineup WHERE season_id=? AND competition_id=? AND bbbffl_round_id=? AND season_entry_id=?", (context["season_id"], context["competition_id"], context["bbbffl_round_id"], entry_id)).fetchone()
        if not lineup or lineup["effective_submission_version"] is None:
            raise ValueError(f"entry {entry_id} has no effective submitted lineup")
        version = lineup["effective_submission_version"]
        slots = self.database.execute("SELECT s.position, s.season_player_id, p.canonical_player_id, p.afl_team_id FROM weekly_lineup_submission_slot s LEFT JOIN season_player_pool p ON p.season_player_id=s.season_player_id WHERE s.lineup_id=? AND s.version=? ORDER BY s.position", (lineup["lineup_id"], version)).fetchall()
        evidence, total = [], 0
        matches, stats_by_match = facts
        for slot in slots:
            match = next((m for m in matches if slot["afl_team_id"] is not None and m.involves_team(slot["afl_team_id"])), None)
            stat = stats_by_match.get(match.match_id, {}).get(slot["canonical_player_id"]) if match and slot["canonical_player_id"] else None
            raw = asdict(stat) if stat else None
            score = None
            if slot["position"] != "Interchange":
                if stat is None:
                    score = 0
                elif all(value is not None for key, value in raw.items() if key != "canonical_player_id"):
                    score = score_position(POSITION_MAP[slot["position"]], PlayerStats(**{key: value for key, value in raw.items() if key != "canonical_player_id"}), rules)
                if score is not None:
                    total += score
            evidence.append({"position": slot["position"], "season_player_id": slot["season_player_id"], "canonical_player_id": slot["canonical_player_id"], "afl_match_id": match.match_id if match else None, "played": stat is not None, "stats": raw, "score": score, "interchange_available": slot["position"] == "Interchange"})
        return {"season_entry_id": entry_id, "lineup_id": lineup["lineup_id"], "lineup_version": version, "score": total, "slots": evidence}

    def _persist(self, matchup, context, home, away, snapshot, fingerprint, upstream_revision, observed_at):
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        with transaction(self.database) as conn:
            current = conn.execute("SELECT * FROM bbbffl_matchup_calculation WHERE matchup_id=?" + _for_update_suffix(self.database), (matchup["matchup_id"],)).fetchone()
            if current and current["input_fingerprint"] == fingerprint:
                return current["revision"]
            revision = current["revision"] + 1 if current else 1
            values = (revision, encoded, fingerprint, context["season_id"], context["rules_version_id"], context["bbbffl_round_id"], home["season_entry_id"], away["season_entry_id"], home["lineup_id"], home["lineup_version"], away["lineup_id"], away["lineup_version"], upstream_revision, observed_at, ENGINE_VERSION, _now(), matchup["matchup_id"])
            if current:
                conn.execute("UPDATE bbbffl_matchup_calculation SET revision=?, snapshot=?, input_fingerprint=?, season_id=?, rules_version_id=?, bbbffl_round_id=?, home_season_entry_id=?, away_season_entry_id=?, home_lineup_id=?, home_lineup_version=?, away_lineup_id=?, away_lineup_version=?, upstream_revision=?, upstream_observed_at=?, engine_version=?, updated_at=? WHERE matchup_id=?", values)
            else:
                conn.execute("INSERT INTO bbbffl_matchup_calculation (revision,snapshot,input_fingerprint,season_id,rules_version_id,bbbffl_round_id,home_season_entry_id,away_season_entry_id,home_lineup_id,home_lineup_version,away_lineup_id,away_lineup_version,upstream_revision,upstream_observed_at,engine_version,updated_at,matchup_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
        return revision

    def _round_context(self, round_id):
        row = self.database.execute("SELECT l.*, c.rules_version_id, v.scoring_rules FROM bbbffl_round_lifecycle l JOIN competition_stream c ON c.competition_id=l.competition_id JOIN season_rules_version v ON v.rules_version_id=c.rules_version_id WHERE l.bbbffl_round_id=?", (round_id,)).fetchone()
        if not row:
            raise KeyError(round_id)
        return row

    def _matchups(self, round_id):
        return self.database.execute("SELECT * FROM bbbffl_matchup WHERE bbbffl_round_id=? ORDER BY matchup_order", (round_id,)).fetchall()
