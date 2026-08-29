"""Checkpoint report primitives for the #67 Rounds 1-9 replay-validation
suite (staged/early lockout, ordinary bye/availability, missing-submission/
carry-forward/proxy, and Opening Round deferred/compensating-bye
scenarios).

This module owns **only** deterministic evidence/report assembly -- exactly
`app/replay.py`'s existing "replay-only code should primarily provide
deterministic evidence, controlled time, scenario orchestration, checkpoint
reporting" boundary (see docs/replay-harness.md). It contains no sporting
rule, no scoring, no lockout, no carry-forward, no Opening Round logic of
its own: every scenario in `tests/test_replay_checkpoint.py` drives the same
production repositories/services #66 and #69 already implement
(`app.lockouts`, `app.lineup_validation`, `app.carry_forward`,
`app.lineup_proxy`, `app.opening_round`, `app.calculations`,
`app.round_review`, `app.ladder`) and passes their *results* in here purely
to be shaped into one comparable, deterministic report.

## Outcome states

Reusing `app.replay.EvidenceClass`'s existing four-way evidence
classification, a scenario's overall outcome is one of `PASS`, `FAIL`, or
`UNRESOLVED` (`ScenarioOutcome`). `build_checkpoint_scenario` computes this
automatically unless the caller overrides it: any `unresolved_questions`
entry makes the scenario `UNRESOLVED` (an unresolved scorer question must
never read as a silent pass -- issue #67 requirement 6); otherwise any
`discrepancies` entry makes it `FAIL`; otherwise `PASS`. An `UNRESOLVED`
scenario is a valid, deliberate replay finding (issue #67 requirement 4,
"a missing answer is a valid replay finding"), never treated as a defect to
be hidden or a pass to be assumed.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

REQUIRED_SCENARIO_SECTIONS = (
    "scenario_id",
    "description",
    "historical_or_synthetic",
    "evidence_sources",
    "evidence_classification",
    "starting_lineup",
    "lineup_provenance",
    "clocks",
    "afl_match_state",
    "lockout",
    "validation_warnings",
    "availability_warnings",
    "deferred_source",
    "carry_forward",
    "proxy_entry",
    "unresolved_questions",
    "calculated_result",
    "official_result",
    "expected_vs_actual",
    "ladder_effect",
    "discrepancies",
)


class CheckpointReportError(ValueError):
    """A checkpoint scenario or suite report is incomplete or malformed."""


class ScenarioOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNRESOLVED = "UNRESOLVED"


class HistoricalStatus:
    """`historical_or_synthetic` is always exactly one of these two literal
    values -- never a free-text description -- so a report can never blur
    an observed 2026 fact with a labelled synthetic scenario (issue #67
    requirement 1: "Never represent synthetic data as an observed 2026
    fact")."""

    HISTORICAL = "historical"
    SYNTHETIC = "synthetic"


def _compute_outcome(unresolved_questions: list, discrepancies: list) -> ScenarioOutcome:
    if unresolved_questions:
        return ScenarioOutcome.UNRESOLVED
    if discrepancies:
        return ScenarioOutcome.FAIL
    return ScenarioOutcome.PASS


def build_checkpoint_scenario(
    scenario_id: str,
    description: str,
    *,
    historical_or_synthetic: str,
    evidence_sources: list[str],
    evidence_classification: list[dict[str, Any]],
    clocks: dict[str, str],
    starting_lineup: list[dict[str, Any]] | None = None,
    lineup_provenance: list[dict[str, Any]] | None = None,
    afl_match_state: list[dict[str, Any]] | None = None,
    lockout: list[dict[str, Any]] | None = None,
    validation_warnings: list[dict[str, Any]] | None = None,
    availability_warnings: list[dict[str, Any]] | None = None,
    deferred_source: dict[str, Any] | None = None,
    carry_forward: dict[str, Any] | None = None,
    proxy_entry: dict[str, Any] | None = None,
    unresolved_questions: list[dict[str, Any]] | None = None,
    calculated_result: dict[str, Any] | None = None,
    official_result: dict[str, Any] | None = None,
    expected_vs_actual: dict[str, Any] | None = None,
    ladder_effect: list[dict[str, Any]] | None = None,
    discrepancies: list[dict[str, Any]] | None = None,
    outcome: ScenarioOutcome | None = None,
) -> dict[str, Any]:
    """Assemble one deterministic, human-readable checkpoint scenario
    report (issue #67 requirement 6). A read-model/diagnostics function
    only: every argument is a value the caller already computed by driving
    the real production services -- nothing here infers, recomputes, or
    guesses any of it.

    `outcome` is computed from `unresolved_questions`/`discrepancies` unless
    the caller supplies an explicit override (used only for a scenario
    whose "result" *is* the absence of one, such as Round 1's deliberate
    no-prior-lineup case)."""
    if historical_or_synthetic not in (HistoricalStatus.HISTORICAL, HistoricalStatus.SYNTHETIC):
        raise CheckpointReportError(
            f"historical_or_synthetic must be 'historical' or 'synthetic', got {historical_or_synthetic!r}"
        )
    unresolved_questions = list(unresolved_questions or [])
    discrepancies = list(discrepancies or [])
    resolved_outcome = outcome or _compute_outcome(unresolved_questions, discrepancies)
    return {
        "scenario_id": scenario_id,
        "description": description,
        "historical_or_synthetic": historical_or_synthetic,
        "evidence_sources": list(evidence_sources),
        "evidence_classification": list(evidence_classification),
        "starting_lineup": list(starting_lineup or []),
        "lineup_provenance": list(lineup_provenance or []),
        "clocks": dict(clocks),
        "afl_match_state": list(afl_match_state or []),
        "lockout": list(lockout or []),
        "validation_warnings": list(validation_warnings or []),
        "availability_warnings": list(availability_warnings or []),
        "deferred_source": deferred_source,
        "carry_forward": carry_forward,
        "proxy_entry": proxy_entry,
        "unresolved_questions": unresolved_questions,
        "calculated_result": calculated_result,
        "official_result": official_result,
        "expected_vs_actual": expected_vs_actual,
        "ladder_effect": list(ladder_effect or []),
        "discrepancies": discrepancies,
        "outcome": resolved_outcome.value,
    }


def _validate_scenario(scenario: dict[str, Any]) -> None:
    missing = [key for key in REQUIRED_SCENARIO_SECTIONS if key not in scenario]
    if missing:
        raise CheckpointReportError(f"checkpoint scenario is incomplete; missing sections: {', '.join(missing)}")
    if scenario["outcome"] not in {member.value for member in ScenarioOutcome}:
        raise CheckpointReportError(f"unknown scenario outcome: {scenario['outcome']!r}")
    if scenario["unresolved_questions"] and scenario["outcome"] != ScenarioOutcome.UNRESOLVED.value:
        raise CheckpointReportError(
            f"scenario {scenario['scenario_id']!r} has unresolved questions but outcome "
            f"{scenario['outcome']!r} != UNRESOLVED -- an unknown must never read as PASS"
        )


def build_checkpoint_suite(run_id: str, scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble the full #67 checkpoint suite from its scenario reports.
    Deliberately a thin wrapper: ordering/content of `scenarios` is exactly
    what the caller (the test suite) produced from real replay runs."""
    for scenario in scenarios:
        _validate_scenario(scenario)
    counts = {member.value: 0 for member in ScenarioOutcome}
    for scenario in scenarios:
        counts[scenario["outcome"]] += 1
    return {
        "run_id": run_id,
        "scenarios": list(scenarios),
        "outcome_counts": counts,
        "suite_resolved": counts[ScenarioOutcome.FAIL.value] == 0 and counts[ScenarioOutcome.UNRESOLVED.value] == 0,
    }


def write_checkpoint_suite_report(suite: dict[str, Any], json_path: str | Path, summary_path: str | Path) -> None:
    """Write stable, deterministic JSON plus a concise operator-readable
    text summary -- the #67 counterpart to `app.replay.write_replay_report`."""
    if "scenarios" not in suite or "outcome_counts" not in suite:
        raise CheckpointReportError("checkpoint suite report is incomplete; missing scenarios/outcome_counts")
    Path(json_path).write_text(json.dumps(suite, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [f"BBBFFL replay checkpoint {suite['run_id']}", ""]
    for scenario in suite["scenarios"]:
        lines.append(f"[{scenario['outcome']}] {scenario['scenario_id']} ({scenario['historical_or_synthetic']})")
        lines.append(f"    {scenario['description']}")
        if scenario["unresolved_questions"]:
            for question in scenario["unresolved_questions"]:
                lines.append(f"    UNRESOLVED: {question}")
        if scenario["discrepancies"]:
            for discrepancy in scenario["discrepancies"]:
                lines.append(f"    DISCREPANCY: {discrepancy}")
    lines.append("")
    lines.append(f"Outcome counts: {suite['outcome_counts']}")
    lines.append(f"Suite resolved (no FAIL/UNRESOLVED): {suite['suite_resolved']}")
    Path(summary_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
