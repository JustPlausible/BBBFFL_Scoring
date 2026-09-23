"""Opt-in pytest plugin that makes long CI runs diagnosable (issue #218).

Enable it explicitly::

    python -m pytest -p tests.ci_observability --ci-timing-log timings.jsonl

It is **observational only**: it never skips, reorders, retries, times out or
fails a test, and it does not touch fixtures, databases or test outcomes. When
the plugin is not requested with ``-p`` nothing here is imported, so ordinary
local ``pytest`` runs are unaffected.

What it adds:

* an environment line at session start and end, including a small disk
  write+fsync latency probe (the SQLite-backed tests are dominated by
  file-system writes, and the slow hosted-runner executions examined in #218
  were slow on exactly those tests and nowhere else);
* a ``[ci-progress]`` heartbeat every ``--ci-heartbeat`` seconds showing how
  many tests have finished, how many finished since the previous heartbeat,
  which test/phase is currently running and for how long, the process's CPU
  use, and host iowait/steal/IO-pressure -- enough to tell "slow but moving"
  from "stalled", and "our code is busy" from "the machine is waiting";
* a ``[ci-progress] SLOW`` line as soon as any single test phase has been
  running longer than ``--ci-slow-test`` seconds, naming the test and whether
  it is in setup (fixtures), call or teardown (later heartbeats keep showing
  it as ``current=`` while it continues);
* a JSON-lines log (one line per finished test) with per-test setup/call/teardown durations,
  flushed after every test so it survives a cancelled job, which
  ``scripts/ci_test_timing_report.py`` summarises and compares across runs;
* a short per-file timing table in the terminal summary.

See docs/ci-quality-gates.md ("Python test runtime and slow CI runs").
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import tempfile
import threading
import time
from collections import defaultdict

import pytest

PREFIX = "[ci-progress]"


def pytest_addoption(parser):
    group = parser.getgroup("ci-observability", "CI runtime observability (issue #218)")
    group.addoption(
        "--ci-heartbeat",
        type=float,
        default=300.0,
        help="Seconds between [ci-progress] heartbeat lines (default 300; 0 disables the heartbeat thread).",
    )
    group.addoption(
        "--ci-slow-test",
        type=float,
        default=120.0,
        help="Report a single test phase once it has run this many seconds (default 120). Never fails the test.",
    )
    group.addoption(
        "--ci-timing-log",
        default=None,
        help="Write one JSON line per finished test (setup/call/teardown seconds) to this file (overwritten).",
    )
    group.addoption(
        "--ci-top-files",
        type=int,
        default=15,
        help="Number of slowest test files listed in the terminal summary (default 15).",
    )


def pytest_configure(config):
    config.pluginmanager.register(CiObservability(config), "ci-observability-reporter")


def format_seconds(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    return f"{minutes}m{secs:02d}s"


def fsync_probe(directory: str, writes: int = 20) -> dict | None:
    """Median/max latency of small write+fsync pairs in ``directory``.

    Mirrors the dominant I/O pattern of the SQLite-backed tests (many small
    committed transactions to a file under the temp directory). Returns None
    if the probe itself cannot run -- observability must never break a run.
    """
    try:
        fd, path = tempfile.mkstemp(prefix="ci-fsync-probe-", dir=directory)
    except OSError:
        return None
    samples = []
    try:
        payload = b"x" * 4096
        for _ in range(writes):
            start = time.perf_counter()
            os.write(fd, payload)
            os.fsync(fd)
            samples.append((time.perf_counter() - start) * 1000)
    except OSError:
        return None
    finally:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
    return {"p50_ms": round(statistics.median(samples), 2), "max_ms": round(max(samples), 2)}


def read_cpu_times() -> dict | None:
    """System-wide jiffies from /proc/stat (Linux only)."""
    try:
        with open("/proc/stat") as handle:
            fields = handle.readline().split()
    except OSError:
        return None
    if not fields or fields[0] != "cpu":
        return None
    values = [int(value) for value in fields[1:]]
    names = ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"]
    return dict(zip(names, values + [0] * (len(names) - len(values))))


def cpu_share(before: dict | None, after: dict | None) -> dict:
    if not before or not after:
        return {}
    delta = {key: after[key] - before[key] for key in after}
    total = sum(delta.values())
    if total <= 0:
        return {}
    return {
        "iowait_pct": round(100 * delta["iowait"] / total, 1),
        "steal_pct": round(100 * delta["steal"] / total, 1),
    }


def read_io_pressure() -> str | None:
    """``some avg60`` from Linux PSI, i.e. % of the last minute in which at
    least one task was stalled waiting for I/O. None where unavailable."""
    try:
        with open("/proc/pressure/io") as handle:
            for line in handle:
                if line.startswith("some"):
                    for part in line.split():
                        if part.startswith("avg60="):
                            return part.split("=", 1)[1]
    except OSError:
        return None
    return None


def environment_line(label: str, probe_dir: str) -> str:
    parts = [f"{PREFIX} {label}:", f"cpus={os.cpu_count()}"]
    if hasattr(os, "getloadavg"):
        parts.append("load=" + "/".join(f"{value:.2f}" for value in os.getloadavg()))
    try:
        with open("/proc/meminfo") as handle:
            meminfo = dict(line.split(":", 1) for line in handle)
        available = int(meminfo["MemAvailable"].split()[0]) // 1024
        total = int(meminfo["MemTotal"].split()[0]) // 1024
        parts.append(f"mem_available={available}MiB/{total}MiB")
    except (OSError, KeyError, ValueError):
        pass
    try:
        free = shutil.disk_usage(probe_dir).free // (1024 * 1024)
        parts.append(f"tmp_free={free}MiB")
    except OSError:
        pass
    probe = fsync_probe(probe_dir)
    if probe:
        parts.append(f"fsync_p50={probe['p50_ms']}ms fsync_max={probe['max_ms']}ms")
    pressure = read_io_pressure()
    if pressure is not None:
        parts.append(f"io_pressure_avg60={pressure}%")
    parts.append(f"tmp={probe_dir}")
    return " ".join(parts)


class CiObservability:
    def __init__(self, config):
        self.config = config
        self.heartbeat = config.getoption("ci_heartbeat")
        self.slow_threshold = config.getoption("ci_slow_test")
        self.timing_log_path = config.getoption("ci_timing_log")
        self.top_files = config.getoption("ci_top_files")
        self.probe_dir = tempfile.gettempdir()

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._out_fd = None
        self._log = None

        self.started = time.monotonic()
        self.collected = 0
        self.completed = 0
        self.current = None  # (nodeid, phase, phase_started_monotonic)
        self.phase_durations: dict[str, dict[str, float]] = defaultdict(dict)
        self.file_seconds: dict[str, float] = defaultdict(float)
        self.file_tests: dict[str, int] = defaultdict(int)

    # -- output -----------------------------------------------------------
    def _emit(self, line: str) -> None:
        """Write a whole line straight to the job log.

        Uses a duplicate of the real stdout taken at session start, so lines
        written while a test is running are not swallowed by pytest's
        per-test output capture. The leading newline keeps the line from
        being glued onto a half-written row of progress dots.
        """
        data = ("\n" + line + "\n").encode("utf-8", "replace")
        try:
            if self._out_fd is not None:
                os.write(self._out_fd, data)
            else:
                os.write(1, data)
        except OSError:
            pass

    # -- session ----------------------------------------------------------
    def pytest_sessionstart(self, session):
        try:
            self._out_fd = os.dup(1)
        except OSError:
            self._out_fd = None
        if self.timing_log_path:
            directory = os.path.dirname(os.path.abspath(self.timing_log_path))
            os.makedirs(directory, exist_ok=True)
            self._log = open(self.timing_log_path, "w", encoding="utf-8")
        self.started = time.monotonic()
        self._emit(environment_line("environment at start", self.probe_dir))
        if self.heartbeat and self.heartbeat > 0:
            self._thread = threading.Thread(target=self._watch, name="ci-observability", daemon=True)
            self._thread.start()

    def pytest_collection_finish(self, session):
        self.collected = len(session.items)
        self._write_record({"event": "collected", "count": self.collected})

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(self, item):
        self._set_phase(item.nodeid, "setup")
        yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        self._set_phase(item.nodeid, "call")
        yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_teardown(self, item):
        self._set_phase(item.nodeid, "teardown")
        yield

    def _set_phase(self, nodeid, phase):
        with self._lock:
            self.current = (nodeid, phase, time.monotonic())

    def pytest_runtest_logreport(self, report):
        self.phase_durations[report.nodeid][report.when] = report.duration
        if report.when == "call" or (report.when == "setup" and not report.passed):
            self.phase_durations[report.nodeid]["outcome"] = report.outcome
        elif report.when == "teardown" and report.failed:
            self.phase_durations[report.nodeid]["outcome"] = "error"

    def pytest_runtest_logfinish(self, nodeid, location):
        phases = self.phase_durations.pop(nodeid, {})
        total = sum(phases.get(name, 0.0) for name in ("setup", "call", "teardown"))
        path = nodeid.split("::", 1)[0]
        with self._lock:
            self.completed += 1
            self.current = None
            self.file_seconds[path] += total
            self.file_tests[path] += 1
        self._write_record(
            {
                "nodeid": nodeid,
                "file": path,
                "setup": round(phases.get("setup", 0.0), 4),
                "call": round(phases.get("call", 0.0), 4),
                "teardown": round(phases.get("teardown", 0.0), 4),
                "outcome": phases.get("outcome", "unknown"),
                "elapsed": round(time.monotonic() - self.started, 2),
            }
        )

    def _write_record(self, record):
        if self._log is None:
            return
        try:
            self._log.write(json.dumps(record) + "\n")
            self._log.flush()
        except (OSError, ValueError):
            pass

    def pytest_sessionfinish(self, session, exitstatus):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._emit(environment_line("environment at end", self.probe_dir))
        self._write_record(
            {
                "event": "finished",
                "exitstatus": int(exitstatus),
                "completed": self.completed,
                "elapsed": round(time.monotonic() - self.started, 2),
            }
        )
        if self._log is not None:
            self._log.close()
            self._log = None

    def pytest_unconfigure(self, config):
        self._stop.set()
        if self._out_fd is not None:
            try:
                os.close(self._out_fd)
            except OSError:
                pass
            self._out_fd = None

    def pytest_terminal_summary(self, terminalreporter):
        if not self.file_seconds or self.top_files <= 0:
            return
        terminalreporter.write_sep("=", f"slowest {self.top_files} test files (setup+call+teardown)")
        ranked = sorted(self.file_seconds.items(), key=lambda item: item[1], reverse=True)
        for path, seconds in ranked[: self.top_files]:
            terminalreporter.write_line(f"{seconds:9.2f}s  {self.file_tests[path]:4d} tests  {path}")
        total = sum(self.file_seconds.values())
        terminalreporter.write_line(
            f"{total:9.2f}s  {sum(self.file_tests.values()):4d} tests  total in-test time "
            f"(wall {format_seconds(time.monotonic() - self.started)})"
        )

    # -- heartbeat thread -------------------------------------------------
    def _watch(self):
        tick = max(1.0, min(5.0, self.heartbeat / 4, self.slow_threshold / 4 if self.slow_threshold else 5.0))
        last_beat = time.monotonic()
        last_completed = 0
        last_cpu = os.times()
        last_sys = read_cpu_times()
        reported = None
        while not self._stop.wait(tick):
            now = time.monotonic()
            with self._lock:
                current = self.current
                completed = self.completed
            if current and self.slow_threshold and self.slow_threshold > 0:
                nodeid, phase, since = current
                running = now - since
                key = (nodeid, phase, since)
                if running >= self.slow_threshold and reported != key:
                    reported = key
                    self._emit(
                        f"{PREFIX} SLOW: {nodeid} has been in {phase} for {format_seconds(running)} "
                        f"(threshold {format_seconds(self.slow_threshold)}; the test is NOT failed or interrupted)"
                    )
            if now - last_beat < self.heartbeat:
                continue
            cpu = os.times()
            wall = now - last_beat
            process_cpu = (cpu.user + cpu.system) - (last_cpu.user + last_cpu.system)
            sys_times = read_cpu_times()
            share = cpu_share(last_sys, sys_times)
            finished = completed - last_completed
            parts = [
                f"{PREFIX} +{format_seconds(now - self.started)}",
                f"done {completed}/{self.collected or '?'}",
                f"(+{finished} in last {format_seconds(wall)})",
                f"proc_cpu={100 * process_cpu / wall:.0f}%",
            ]
            if share:
                parts.append(f"iowait={share['iowait_pct']}% steal={share['steal_pct']}%")
            pressure = read_io_pressure()
            if pressure is not None:
                parts.append(f"io_pressure_avg60={pressure}%")
            probe = fsync_probe(self.probe_dir, writes=10)
            if probe:
                parts.append(f"fsync_p50={probe['p50_ms']}ms")
            if current:
                nodeid, phase, since = current
                parts.append(f"current={nodeid} [{phase} {format_seconds(now - since)}]")
            if finished == 0:
                parts.append("NO TESTS FINISHED SINCE LAST HEARTBEAT")
            self._emit(" ".join(parts))
            last_beat, last_completed, last_cpu, last_sys = now, completed, cpu, sys_times
