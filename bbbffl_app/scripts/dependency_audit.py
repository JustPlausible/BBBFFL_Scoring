"""pip-audit wrapper enforcing BBBFFL's documented dependency/security policy
(issue #39 / roadmap package 07). See ../docs/ci-quality-gates.md for the
full policy this implements; in short:

  * Every known vulnerability pip-audit reports against a runtime dependency
    fails required CI by default -- there is no severity threshold below
    which a finding is ignored, because the advisory sources pip-audit uses
    do not reliably carry a normalized severity score to threshold on.
  * The only way to make a specific, already-triaged finding non-blocking is
    a time-boxed entry in security/pip-audit-ignore.toml naming who owns it,
    why it isn't actionable yet, and a review date.
  * An expired entry (`review_by` in the past) is treated as if it were not
    suppressed at all, so a "temporary" exception can never go stale
    silently -- the check goes red again until someone re-reviews it.

Run locally exactly as CI does:

    python -m scripts.dependency_audit
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_IGNORE_FILE = Path(__file__).resolve().parent.parent / "security" / "pip-audit-ignore.toml"
DEFAULT_REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"
REQUIRED_FIELDS = {"id", "reason", "owner", "review_by"}


class SuppressionPolicyError(ValueError):
    """The suppression file is malformed, or contains an expired entry."""


@dataclass(frozen=True)
class Suppression:
    vuln_id: str
    reason: str
    owner: str
    review_by: dt.date


def load_suppressions(path: Path = DEFAULT_IGNORE_FILE, *, today: dt.date | None = None) -> list[Suppression]:
    """Parse and validate the suppression policy file.

    Raises `SuppressionPolicyError` for a malformed entry or one whose
    `review_by` date has passed -- both are policy violations, not warnings,
    because either would let a real finding go unenforced without anyone
    having actually re-reviewed it.
    """
    today = today or dt.datetime.now(dt.timezone.utc).date()
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text())
    entries = data.get("suppressions", [])
    suppressions = []
    expired = []
    for entry in entries:
        missing = REQUIRED_FIELDS - entry.keys()
        if missing:
            raise SuppressionPolicyError(f"suppression entry {entry!r} is missing required field(s): {sorted(missing)}")
        review_by = entry["review_by"]
        if isinstance(review_by, str):
            review_by = dt.date.fromisoformat(review_by)
        suppression = Suppression(
            vuln_id=entry["id"], reason=entry["reason"], owner=entry["owner"], review_by=review_by
        )
        if suppression.review_by < today:
            expired.append(suppression)
        suppressions.append(suppression)
    if expired:
        names = ", ".join(f"{item.vuln_id} (owner={item.owner}, due {item.review_by})" for item in expired)
        raise SuppressionPolicyError(
            f"suppression review date has passed for: {names}. Re-review each finding and either "
            "extend review_by with a fresh justification or remove the entry so the finding is enforced again."
        )
    return suppressions


def build_pip_audit_command(requirement: Path, suppressions: list[Suppression]) -> list[str]:
    """The exact pip-audit invocation the policy requires.

    `--strict` fails the run if pip-audit cannot fetch advisory data for any
    dependency, rather than silently treating an unreachable/unknown package
    as clean -- a fetch failure must be loud, not a free pass.
    """
    command = ["pip-audit", "--strict", "--requirement", str(requirement)]
    for suppression in suppressions:
        command += ["--ignore-vuln", suppression.vuln_id]
    return command


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--requirement", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--ignore-file", type=Path, default=DEFAULT_IGNORE_FILE)
    args = parser.parse_args(argv)

    try:
        suppressions = load_suppressions(args.ignore_file)
    except SuppressionPolicyError as error:
        print(f"dependency audit policy violation: {error}", file=sys.stderr)
        return 1

    command = build_pip_audit_command(args.requirement, suppressions)
    print("+", " ".join(command))
    return subprocess.call(command)


if __name__ == "__main__":
    sys.exit(main())
