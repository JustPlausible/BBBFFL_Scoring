"""Summarise (and optionally compare) pytest timing logs written by the
opt-in ``tests.ci_observability`` plugin (issue #218).

CI runs this after the test step -- including when that step failed or the
job was cancelled -- and appends the Markdown to the job summary:

    python -m scripts.ci_test_timing_report timings.jsonl >> "$GITHUB_STEP_SUMMARY"

To decide whether a slow run was slow *everywhere* (runner/environment) or in
*particular* tests (application/test related), compare it with a normal run's
log:

    python -m scripts.ci_test_timing_report slow.jsonl --baseline normal.jsonl

This script only reads timing data; it never affects a test outcome, and it
always exits 0 for a missing or partial log so it cannot turn a CI job red.
See docs/ci-quality-gates.md ("Python test runtime and slow CI runs").
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

PHASES = ("setup", "call", "teardown")


@dataclass
class TimingLog:
    tests: list[dict] = field(default_factory=list)
    collected: int | None = None
    finished: dict | None = None

    @property
    def complete(self) -> bool:
        return self.finished is not None

    def file_seconds(self) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for record in self.tests:
            totals[record["file"]] += record_seconds(record)
        return dict(totals)


def record_seconds(record: dict) -> float:
    return sum(float(record.get(phase, 0.0)) for phase in PHASES)


def load(path: Path) -> TimingLog:
    """Read a timing log, tolerating a truncated final line (a cancelled job
    can be killed mid-write)."""
    log = TimingLog()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event") == "collected":
                log.collected = record.get("count")
            elif record.get("event") == "finished":
                log.finished = record
            elif "nodeid" in record:
                log.tests.append(record)
    return log


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.1f} min"


def summarise(log: TimingLog, top: int = 15) -> str:
    lines = ["## Python test timing (issue #218 observability)", ""]
    outcomes: dict[str, int] = defaultdict(int)
    for record in log.tests:
        outcomes[record.get("outcome", "unknown")] += 1
    in_test = sum(record_seconds(record) for record in log.tests)
    wall = log.finished["elapsed"] if log.finished else (log.tests[-1]["elapsed"] if log.tests else 0.0)
    expected = log.collected if log.collected is not None else "?"
    if log.complete:
        status = f"completed (pytest exit status {log.finished['exitstatus']})"
    else:
        status = "**INCOMPLETE** -- pytest did not reach the end of the session (cancelled, killed or crashed)"
    lines += [
        f"- Session: {status}",
        f"- Tests recorded: {len(log.tests)} of {expected} collected ("
        + ", ".join(f"{count} {outcome}" for outcome, count in sorted(outcomes.items()))
        + ")",
        f"- Wall time in pytest session: {_minutes(wall)}; time inside tests: {_minutes(in_test)}",
    ]
    if log.tests and not log.complete:
        last = log.tests[-1]
        lines.append(f"- Last test to finish: `{last['nodeid']}` at +{_minutes(last['elapsed'])}")
    lines.append("")

    if not log.tests:
        lines.append("No per-test timings were recorded.")
        return "\n".join(lines) + "\n"

    ranked_files = sorted(log.file_seconds().items(), key=lambda item: item[1], reverse=True)
    counts: dict[str, int] = defaultdict(int)
    for record in log.tests:
        counts[record["file"]] += 1
    lines += [
        f"### Slowest {min(top, len(ranked_files))} test files",
        "",
        "| seconds | tests | file |",
        "|---:|---:|---|",
    ]
    lines += [f"| {seconds:.1f} | {counts[path]} | `{path}` |" for path, seconds in ranked_files[:top]]
    lines.append("")

    ranked_tests = sorted(log.tests, key=record_seconds, reverse=True)[:top]
    lines += [
        f"### Slowest {len(ranked_tests)} tests",
        "",
        "| total s | setup s | call s | teardown s | test |",
        "|---:|---:|---:|---:|---|",
    ]
    lines += [
        f"| {record_seconds(r):.2f} | {r['setup']:.2f} | {r['call']:.2f} | {r['teardown']:.2f} | `{r['nodeid']}` |"
        for r in ranked_tests
    ]
    lines.append("")
    return "\n".join(lines) + "\n"


def compare(
    current: TimingLog,
    baseline: TimingLog,
    min_seconds: float = 5.0,
    outlier_extra_seconds: float = 30.0,
    top: int = 10,
) -> str:
    """Per-file slowdown of ``current`` relative to ``baseline``.

    Only files that took at least ``min_seconds`` in the baseline are used for
    the ratio distribution; sub-second files are too noisy to compare. A
    narrow spread around one large median means everything slowed down
    together (points at the runner/environment); a few files far above the
    median points at those tests.
    """
    now, before = current.file_seconds(), baseline.file_seconds()
    ratios = sorted(
        (
            (path, now[path] / before[path], now[path], before[path])
            for path in now
            if before.get(path, 0) >= min_seconds
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    lines = ["## Comparison with baseline run", ""]
    if not ratios:
        lines.append(f"No common test files with at least {min_seconds:g}s in the baseline to compare.")
        return "\n".join(lines) + "\n"
    values = [ratio for _, ratio, _, _ in ratios]
    median = statistics.median(values)
    common_now = sum(now[path] for path in now if path in before)
    common_before = sum(before[path] for path in now if path in before)
    lines.append(f"- Files compared: {len(ratios)} (baseline >= {min_seconds:g}s)")
    if common_before:
        lines.append(f"- Overall slowdown on common files: x{common_now / common_before:.2f}")
    lines.append(f"- Per-file slowdown: median x{median:.2f}, min x{min(values):.2f}, max x{max(values):.2f}")
    # A file only stands out if it slowed down far more than everything else
    # *and* lost a material amount of time: in #218's evidence, runner-level
    # slowdowns alone left individual files anywhere from ~0.5x to ~2.3x of
    # that run's median slowdown, so smaller deviations are noise.
    outliers = [item for item in ratios if item[1] >= 3 * median and item[2] - item[3] >= outlier_extra_seconds]
    if median >= 1.5 and not outliers:
        lines.append(
            "- Pattern: **uniform slowdown** across files -- more consistent with runner/environment "
            "variability than with a specific test (confirm with the heartbeat's fsync/iowait/steal figures)."
        )
    elif outliers:
        lines.append(
            f"- Pattern: **{len(outliers)} file(s) slowed down at least 3x as much as the median** -- "
            "investigate these before blaming the runner."
        )
    else:
        lines.append("- Pattern: no material slowdown relative to the baseline.")
    lines += ["", "| slowdown | now s | baseline s | file |", "|---:|---:|---:|---|"]
    lines += [f"| x{ratio:.2f} | {n:.1f} | {b:.1f} | `{path}` |" for path, ratio, n, b in ratios[:top]]
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("log", type=Path, help="timing log written by --ci-timing-log")
    parser.add_argument("--baseline", type=Path, help="timing log from a normal run to compare against")
    parser.add_argument("--top", type=int, default=15, help="rows per table (default 15)")
    args = parser.parse_args(argv)

    if not args.log.exists():
        print(
            "## Python test timing (issue #218 observability)\n\n"
            f"No timing log at `{args.log}` -- pytest did not start or the plugin was not enabled.\n"
        )
        return 0
    current = load(args.log)
    sys.stdout.write(summarise(current, top=args.top))
    if args.baseline:
        if args.baseline.exists():
            sys.stdout.write(compare(current, load(args.baseline), top=args.top))
        else:
            print(f"Baseline log `{args.baseline}` not found; comparison skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
