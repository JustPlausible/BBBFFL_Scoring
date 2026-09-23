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
# pytest.ExitCode values for a session that ran to its natural end: OK,
# TESTS_FAILED, NO_TESTS_COLLECTED. Anything else (2 INTERRUPTED, 3
# INTERNAL_ERROR, 4 USAGE_ERROR, ...) did not. Literal so this script needs
# no pytest import.
NATURAL_EXIT_STATUSES = {0, 1, 5}
EXIT_STATUS_NAMES = {2: "interrupted/cancelled", 3: "pytest internal error", 4: "usage error"}
# compare(): a file counts as materially slower at this ratio, and a
# "uniform"/"specific files" verdict needs at least this many comparable
# files -- with two or three, one regressed file can drag the median up and
# masquerade as a uniform slowdown.
SLOWDOWN = 1.5
MIN_FILES_FOR_PATTERN = 5


@dataclass
class TimingLog:
    tests: list[dict] = field(default_factory=list)
    collected: int | None = None
    finished: dict | None = None

    @property
    def complete(self) -> bool:
        """True only for a session that ran to its natural end.

        A cancelled job usually reaches pytest as SIGINT, and pytest still
        runs ``pytest_sessionfinish`` after an interruption or an internal
        error, so a ``finished`` record alone is not enough: the exit status
        must be a natural end and every collected test must be accounted for.
        """
        if self.finished is None or self.finished.get("exitstatus") not in NATURAL_EXIT_STATUSES:
            return False
        completed = self.finished.get("completed", len(self.tests))
        return self.collected is None or completed >= self.collected

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
    elif log.finished is not None:
        code = log.finished["exitstatus"]
        reason = EXIT_STATUS_NAMES.get(
            code, "e.g. stopped by --maxfail/-x" if code in NATURAL_EXIT_STATUSES else "abnormal"
        )
        status = f"**INCOMPLETE** -- pytest stopped early with exit status {code} ({reason})"
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
    running = (log.finished or {}).get("running")
    if running:
        lines.append(
            f"- Running when the session ended: `{running['nodeid']}` "
            f"({running['phase']}, {running['seconds']:.1f} s in that phase)"
        )
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
    median points at those tests. Separately, a file that was fast in the
    baseline but now costs at least ``outlier_extra_seconds`` more (and
    slowed far more than the median) is always reported: its ratio is noisy,
    but that absolute regression is not.
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
    fast_regressions = sorted(
        (
            (path, now[path] / before[path], now[path], before[path])
            for path in now
            if 0 < before.get(path, 0) < min_seconds and now[path] - before[path] >= outlier_extra_seconds
        ),
        key=lambda item: item[2] - item[3],
        reverse=True,
    )
    # Files with no baseline timing at all (usually tests added since the
    # baseline's commit) are listed but cannot be judged as a regression.
    new_files = sorted(
        ((path, float("inf"), now[path], 0.0) for path in now if path not in before and now[path] >= min_seconds),
        key=lambda item: item[2],
        reverse=True,
    )
    lines = ["## Comparison with baseline run", ""]
    if not ratios and not fast_regressions and not new_files:
        lines.append(f"No common test files with at least {min_seconds:g}s in the baseline to compare.")
        return "\n".join(lines) + "\n"
    common_now = sum(now[path] for path in now if path in before)
    common_before = sum(before[path] for path in now if path in before)
    lines.append(f"- Files compared: {len(ratios)} (baseline >= {min_seconds:g}s)")
    if common_before:
        lines.append(f"- Overall slowdown on common files: x{common_now / common_before:.2f}")
    outliers: list[tuple[str, float, float, float]] = []
    unjudged: list[tuple[str, float, float, float]] = []
    slower_share = 0.0
    values = [ratio for _, ratio, _, _ in ratios]
    if values:
        median = statistics.median(values)
        lines.append(f"- Per-file slowdown: median x{median:.2f}, min x{min(values):.2f}, max x{max(values):.2f}")
        # A file only stands out if it slowed down far more than everything
        # else *and* lost a material amount of time: in #218's evidence,
        # runner-level slowdowns alone left individual files anywhere from
        # ~0.5x to ~2.3x of that run's median slowdown, so smaller deviations
        # are noise.
        outliers = [item for item in ratios if item[1] >= 3 * median and item[2] - item[3] >= outlier_extra_seconds]
        slower_share = sum(value >= SLOWDOWN for value in values) / len(values)
        # Same "far more than the rest" test for the fast files, so a
        # uniform runner slowdown that merely scales a small file up is not
        # mistaken for a specific regression.
        fast_regressions = [item for item in fast_regressions if item[1] >= 3 * median]
    else:
        # No comparable files means no median to judge them against: five
        # files all going 4s -> 40s is as uniform as it gets. Show them in
        # the table, but make no specific-file verdict.
        unjudged = fast_regressions
        fast_regressions = []
    if fast_regressions:
        lines.append(
            f"- **{len(fast_regressions)} file(s) under {min_seconds:g}s in the baseline now take at least "
            f"{outlier_extra_seconds:g}s longer** -- investigate these regardless of the pattern below."
        )
    if new_files:
        lines.append(
            f"- {len(new_files)} file(s) have no baseline timing (marked `new`; usually tests added since the "
            "baseline's commit) and are not used for the pattern -- compare against a baseline of the same "
            "commit to judge them."
        )
    if outliers or fast_regressions:
        lines.append(
            f"- Pattern: **{len(outliers) + len(fast_regressions)} file(s) regressed far more than the rest** "
            "(at least 3x the median slowdown, or a large absolute increase) -- investigate these before "
            "blaming the runner."
        )
    elif len(ratios) < MIN_FILES_FOR_PATTERN:
        lines.append(
            f"- Pattern: only {len(ratios)} comparable file(s) -- too few to call the slowdown uniform or "
            f"specific (needs {MIN_FILES_FOR_PATTERN}); read the table below directly."
        )
    elif slower_share >= 0.75:
        # At least three quarters of the files are materially slower: a broad,
        # not a concentrated, slowdown.
        lines.append(
            "- Pattern: **uniform slowdown** across files -- more consistent with runner/environment "
            "variability than with a specific test (confirm with the heartbeat's fsync/iowait/steal figures)."
        )
    elif max(values) < SLOWDOWN:
        lines.append("- Pattern: no material slowdown relative to the baseline.")
    else:
        lines.append(
            "- Pattern: **mixed** -- some files are materially slower and others are not; "
            "inspect the slowest files below before attributing it to the runner."
        )
    rows = fast_regressions + unjudged[:top] + ratios[:top] + new_files[:top]
    lines += ["", "| slowdown | now s | baseline s | file |", "|---:|---:|---:|---|"]
    lines += [
        f"| {'new' if ratio == float('inf') else f'x{ratio:.2f}'} | {n:.1f} | {b:.1f} | `{path}` |"
        for path, ratio, n, b in rows
    ]
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
