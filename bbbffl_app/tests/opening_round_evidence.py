"""Distilled AFL-side Opening Round facts for 2024/2025/2026, cross-checked
against the raw captures preserved in `docs/evidence/opening-round/` (issue
#69 -- see that directory's README for why the raw captures themselves are
not reshaped into `tests.afl_evidence`'s `afl-api` v1 fixture contract).

Every constant below is transcribed directly from a real
`rounds-<year>.json` capture -- never invented -- and is exactly what those
files establish: AFL round identity, which clubs played Opening Round that
year, and which later AFL round carried each participating club's
compensating bye. This module states no BBBFFL-side fact (a coach's
historical nomination, its target slot, or who made it): see
docs/opening-round-deferred-selection.md's evidence-boundary section. Any
nomination built from these constants in `tests/test_opening_round.py` is
an explicitly synthetic test scenario, not a claimed historical BBBFFL fact.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SeasonEvidence:
    year: int
    afl_season_id: int
    afl_opening_round_id: int
    # AFL club (team) IDs that took part in that year's Opening Round --
    # i.e. every club *not* listed in the Opening Round's own `byes` array.
    participating_clubs: dict[str, int]
    # club abbreviation -> the AFL round ID carrying that club's Opening
    # Round compensating bye.
    compensating_bye_round: dict[str, int]
    source_file: str


# 2024: docs/evidence/opening-round/rounds-2024.json
# Opening Round (id 954, roundNumber 0) byes: ADEL, NMFC, PORT, ESS, FRE,
# WCE, HAW, GEEL, STK, WB (10 clubs) -> 8 participants below.
# R2 (id 956) byes: BL, CARL. R3 (id 957) byes: GCFC, GWS.
# R5 (id 959) byes: COLL, SYD. R6 (id 960) byes: RICH, MELB.
EVIDENCE_2024 = SeasonEvidence(
    year=2024,
    afl_season_id=2024,
    afl_opening_round_id=954,
    participating_clubs={"BL": 2, "CARL": 5, "COLL": 3, "GCFC": 4, "GWS": 15, "MELB": 17, "RICH": 16, "SYD": 13},
    compensating_bye_round={
        "BL": 956,
        "CARL": 956,
        "GCFC": 957,
        "GWS": 957,
        "COLL": 959,
        "SYD": 959,
        "RICH": 960,
        "MELB": 960,
    },
    source_file="docs/evidence/opening-round/rounds-2024.json",
)

# 2025: docs/evidence/opening-round/rounds-2025.json
# Opening Round (id 1146, roundNumber 0) byes: 14 clubs -> only 4 participants.
# R2 (id 1148) byes: GCFC, GWS (GCFC did not play Opening Round -- ordinary bye).
# R3 (id 1149) byes: COLL, SYD. R4 (id 1150) byes: HAW, ESS (ESS ordinary bye).
EVIDENCE_2025 = SeasonEvidence(
    year=2025,
    afl_season_id=2025,
    afl_opening_round_id=1146,
    participating_clubs={"COLL": 3, "GWS": 15, "HAW": 9, "SYD": 13},
    compensating_bye_round={"GWS": 1148, "COLL": 1149, "SYD": 1149, "HAW": 1150},
    source_file="docs/evidence/opening-round/rounds-2025.json",
)

# 2026: docs/evidence/opening-round/rounds-2026.json
# Opening Round (id 1343, roundNumber 0) byes: ADEL, NMFC, PORT, ESS, FRE,
# RICH, MELB, WCE (8 clubs) -> 10 participants below.
# R2 (id 1345) byes: BL, COLL, CARL, GEEL. R3 (id 1346) byes: GCFC, WB, HAW, SYD.
# R4 (id 1347) byes: STK, GWS.
EVIDENCE_2026 = SeasonEvidence(
    year=2026,
    afl_season_id=2026,
    afl_opening_round_id=1343,
    participating_clubs={
        "BL": 2,
        "CARL": 5,
        "COLL": 3,
        "GCFC": 4,
        "GEEL": 10,
        "GWS": 15,
        "HAW": 9,
        "STK": 11,
        "SYD": 13,
        "WB": 8,
    },
    compensating_bye_round={
        "BL": 1345,
        "COLL": 1345,
        "CARL": 1345,
        "GEEL": 1345,
        "GCFC": 1346,
        "WB": 1346,
        "HAW": 1346,
        "SYD": 1346,
        "STK": 1347,
        "GWS": 1347,
    },
    source_file="docs/evidence/opening-round/rounds-2026.json",
)

ALL_SEASONS = (EVIDENCE_2024, EVIDENCE_2025, EVIDENCE_2026)
