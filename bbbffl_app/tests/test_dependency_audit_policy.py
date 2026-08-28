"""Proves the dependency/security exception policy (issue #39) behaves as
documented in docs/ci-quality-gates.md, rather than relying on CI YAML
alone: a malformed or expired suppression must fail loud, a well-formed one
must translate into the exact `--ignore-vuln` pip-audit gets, and the real
committed policy file must currently be valid and unexpired."""

import datetime as dt

import pytest

from scripts.dependency_audit import (
    DEFAULT_IGNORE_FILE,
    SuppressionPolicyError,
    build_pip_audit_command,
    load_suppressions,
)


def _write(tmp_path, body):
    path = tmp_path / "pip-audit-ignore.toml"
    path.write_text(body)
    return path


def test_missing_ignore_file_means_no_suppressions(tmp_path):
    assert load_suppressions(tmp_path / "absent.toml") == []


def test_well_formed_entry_parses_and_is_not_expired(tmp_path):
    path = _write(
        tmp_path,
        """
        [[suppressions]]
        id = "PYSEC-0000-0000"
        reason = "example"
        owner = "someone"
        review_by = "2999-01-01"
        """,
    )
    suppressions = load_suppressions(path, today=dt.date(2026, 1, 1))
    assert len(suppressions) == 1
    assert suppressions[0].vuln_id == "PYSEC-0000-0000"
    assert suppressions[0].review_by == dt.date(2999, 1, 1)


def test_entry_missing_a_required_field_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        """
        [[suppressions]]
        id = "PYSEC-0000-0000"
        reason = "example"
        review_by = "2999-01-01"
        """,
    )
    with pytest.raises(SuppressionPolicyError, match="owner"):
        load_suppressions(path)


def test_entry_with_blank_reason_or_owner_is_rejected(tmp_path):
    """All four keys present isn't enough -- a blank `reason`/`owner` would
    let an unjustified, unowned suppression through unnoticed, defeating the
    whole point of requiring them (Codex review on PR #50)."""
    path = _write(
        tmp_path,
        """
        [[suppressions]]
        id = "PYSEC-0000-0000"
        reason = "   "
        owner = ""
        review_by = "2999-01-01"
        """,
    )
    with pytest.raises(SuppressionPolicyError, match="blank"):
        load_suppressions(path)


def test_expired_entry_is_rejected_rather_than_silently_dropped(tmp_path):
    """An exception whose review date has passed must fail the audit loudly
    -- not fall back to permanently suppressing the finding, and not
    silently stop suppressing it either (both would hide the real state
    from whoever is reading CI's pass/fail signal)."""
    path = _write(
        tmp_path,
        """
        [[suppressions]]
        id = "PYSEC-0000-0000"
        reason = "example"
        owner = "someone"
        review_by = "2020-01-01"
        """,
    )
    with pytest.raises(SuppressionPolicyError, match="PYSEC-0000-0000"):
        load_suppressions(path, today=dt.date(2026, 1, 1))


def test_build_pip_audit_command_is_strict_and_passes_every_suppression(tmp_path):
    path = _write(
        tmp_path,
        """
        [[suppressions]]
        id = "PYSEC-1111-1111"
        reason = "example"
        owner = "someone"
        review_by = "2999-01-01"

        [[suppressions]]
        id = "PYSEC-2222-2222"
        reason = "example"
        owner = "someone"
        review_by = "2999-01-01"
        """,
    )
    suppressions = load_suppressions(path, today=dt.date(2026, 1, 1))
    command = build_pip_audit_command(tmp_path / "requirements.txt", suppressions)
    assert command[:2] == ["pip-audit", "--strict"]
    assert "--ignore-vuln" in command
    assert command.count("--ignore-vuln") == 2
    assert "PYSEC-1111-1111" in command
    assert "PYSEC-2222-2222" in command


def test_committed_policy_file_is_currently_valid_and_unexpired():
    """The real, checked-in exception list must parse and must not be
    silently carrying a stale exception past its review date today."""
    suppressions = load_suppressions(DEFAULT_IGNORE_FILE)
    for suppression in suppressions:
        assert suppression.owner
        assert suppression.reason.strip()
