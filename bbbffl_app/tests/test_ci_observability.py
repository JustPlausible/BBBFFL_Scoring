"""Regression coverage for the opt-in CI observability plugin and its timing
report (issue #218).

The key property is that the plugin is observational only: enabling it must
not change which tests run, in what order, or with what outcome. The other
tests pin the signals an operator relies on when reading a slow CI run.
"""

import json
from pathlib import Path

import pytest

from scripts import ci_test_timing_report as report

pytest_plugins = ["pytester"]

APP_ROOT = Path(__file__).resolve().parent.parent

MIXED_OUTCOMES = """
import time
import pytest

@pytest.fixture
def broken():
    raise RuntimeError("fixture failure")

@pytest.fixture
def slow_fixture():
    time.sleep(SLEEP)
    yield

def test_a_passes():
    assert True

def test_b_fails():
    assert 1 == 2

@pytest.mark.skip(reason="skipped on purpose")
def test_c_skipped():
    pass

@pytest.mark.xfail(strict=True)
def test_d_xfails():
    assert False

def test_e_errors_in_setup(broken):
    pass

def test_f_slow_setup(slow_fixture):
    assert True
"""


@pytest.fixture
def run_inner(pytester, monkeypatch):
    """Run MIXED_OUTCOMES in a separate pytest process, so the plugin writes
    to a real stdout exactly as it does in CI."""
    monkeypatch.setenv("PYTHONPATH", str(APP_ROOT))

    def run(sleep, *args):
        pytester.makepyfile(test_inner=MIXED_OUTCOMES.replace("SLEEP", str(sleep)))
        return pytester.runpytest_subprocess("-v", "-p", "no:cacheprovider", *args)

    return run


def _outcome_lines(result):
    return [line for line in result.outlines if line.startswith("test_inner.py::")]


def test_plugin_does_not_change_selection_order_or_outcomes(pytester, run_inner):
    plain = run_inner(0)
    observed = run_inner(
        0,
        "-p",
        "tests.ci_observability",
        "--ci-timing-log",
        str(pytester.path / "timings.jsonl"),
    )

    expected = {"passed": 2, "failed": 1, "skipped": 1, "xfailed": 1, "errors": 1}
    plain.assert_outcomes(**expected)
    observed.assert_outcomes(**expected)
    assert plain.ret == observed.ret == pytest.ExitCode.TESTS_FAILED
    # Same tests, same order, same per-test verdicts.
    assert [line.split(" ")[:2] for line in _outcome_lines(plain)] == [
        line.split(" ")[:2] for line in _outcome_lines(observed)
    ]


def test_timing_log_records_every_test_and_the_session_boundaries(pytester, run_inner):
    log_path = pytester.path / "out" / "timings.jsonl"
    result = run_inner(0, "-p", "tests.ci_observability", "--ci-timing-log", str(log_path))
    result.assert_outcomes(passed=2, failed=1, skipped=1, xfailed=1, errors=1)

    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert records[0] == {"event": "collected", "count": 6}
    assert records[-1]["event"] == "finished"
    assert records[-1]["completed"] == 6
    assert records[-1]["exitstatus"] == int(pytest.ExitCode.TESTS_FAILED)
    tests = {record["nodeid"].split("::")[1]: record for record in records[1:-1]}
    assert list(tests) == [
        "test_a_passes",
        "test_b_fails",
        "test_c_skipped",
        "test_d_xfails",
        "test_e_errors_in_setup",
        "test_f_slow_setup",
    ]
    assert tests["test_a_passes"]["outcome"] == "passed"
    assert tests["test_b_fails"]["outcome"] == "failed"
    assert tests["test_c_skipped"]["outcome"] == "skipped"
    assert tests["test_e_errors_in_setup"]["outcome"] == "failed"
    assert all(record["file"] == "test_inner.py" for record in tests.values())
    # The terminal summary names the slowest files.
    result.stdout.fnmatch_lines(["*slowest 15 test files*", "*6 tests  test_inner.py"])


def test_heartbeat_reports_progress_stalls_and_slow_setup_without_failing_the_test(run_inner):
    result = run_inner(
        3.5,
        "-p",
        "tests.ci_observability",
        "--ci-heartbeat",
        "1",
        "--ci-slow-test",
        "1",
    )
    # The slow test still passes: observation never interrupts or fails it.
    result.assert_outcomes(passed=2, failed=1, skipped=1, xfailed=1, errors=1)
    result.stdout.fnmatch_lines(
        [
            "[[]ci-progress[]] environment at start: cpus=*",
            "[[]ci-progress[]] SLOW: test_inner.py::test_f_slow_setup has been in setup for 0m0*s*NOT failed*",
            "[[]ci-progress[]] +0m0*s done 5/6 *current=test_inner.py::test_f_slow_setup [[]setup *]*",
            "[[]ci-progress[]] environment at end: *",
        ]
    )
    assert "NO TESTS FINISHED SINCE LAST HEARTBEAT" in result.stdout.str()


def _write_log(path, tests, finished=True, collected=None, truncated=False):
    lines = [json.dumps({"event": "collected", "count": collected or len(tests)})]
    elapsed = 0.0
    for nodeid, seconds in tests:
        elapsed += seconds
        lines.append(
            json.dumps(
                {
                    "nodeid": nodeid,
                    "file": nodeid.split("::")[0],
                    "setup": seconds / 2,
                    "call": seconds / 2,
                    "teardown": 0.0,
                    "outcome": "passed",
                    "elapsed": elapsed,
                }
            )
        )
    if finished:
        lines.append(json.dumps({"event": "finished", "exitstatus": 0, "completed": len(tests), "elapsed": elapsed}))
    text = "\n".join(lines) + "\n"
    if truncated:
        text += '{"nodeid": "tests/test_cut.py::test_cut", "fi'
    path.write_text(text)
    return path


BASELINE = [(f"tests/test_{name}.py::test_one", seconds) for name, seconds in (("a", 10), ("b", 20), ("c", 40))]


def test_report_flags_an_incomplete_cancelled_run_and_tolerates_a_truncated_line(tmp_path):
    log = report.load(_write_log(tmp_path / "t.jsonl", BASELINE[:2], finished=False, collected=3, truncated=True))
    text = report.summarise(log)
    assert "**INCOMPLETE**" in text
    assert "Tests recorded: 2 of 3 collected (2 passed)" in text
    assert "Last test to finish: `tests/test_b.py::test_one`" in text
    assert text.index("tests/test_b.py") < text.index("tests/test_a.py")  # slowest first


def test_report_distinguishes_uniform_slowdown_from_specific_outliers(tmp_path):
    baseline = report.load(_write_log(tmp_path / "base.jsonl", BASELINE))
    uniform = report.load(_write_log(tmp_path / "uniform.jsonl", [(n, s * 6) for n, s in BASELINE]))
    assert "**uniform slowdown**" in report.compare(uniform, baseline)

    one_file = [(n, s * (10 if "test_a" in n else 1)) for n, s in BASELINE]
    text = report.compare(report.load(_write_log(tmp_path / "one.jsonl", one_file)), baseline)
    assert "**1 file(s) slowed down at least 3x as much as the median**" in text
    assert "| x10.00 | 100.0 | 10.0 | `tests/test_a.py` |" in text

    same = report.compare(baseline, baseline)
    assert "no material slowdown" in same


def test_report_main_never_fails_ci_for_a_missing_log(tmp_path, capsys):
    assert report.main([str(tmp_path / "missing.jsonl")]) == 0
    assert "No timing log" in capsys.readouterr().out
