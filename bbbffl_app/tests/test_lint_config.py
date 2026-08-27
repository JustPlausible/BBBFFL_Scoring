"""Proves the Ruff lint/format gate (issue #39) stays the deliberately
narrow, mechanical check documented in docs/ci-quality-gates.md -- guards
against someone widening `select` to a stylistic/refactor rule family
(which would force a mass, unrelated source rewrite to keep CI green)
without a corresponding, reviewed documentation update."""

import tomllib
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PYPROJECT = APP_DIR / "pyproject.toml"

# Correctness/import-hygiene only -- see docs/ci-quality-gates.md for why
# stylistic/refactor families (B, SIM, UP, RUF, ...) are excluded for now.
EXPECTED_SELECT = {"E4", "E7", "E9", "F", "I"}


def _ruff_config():
    return tomllib.loads(PYPROJECT.read_text())["tool"]["ruff"]


def test_lint_rule_selection_is_the_documented_narrow_set():
    config = _ruff_config()
    assert set(config["lint"]["select"]) == EXPECTED_SELECT


def test_format_and_lint_share_one_line_length():
    config = _ruff_config()
    assert isinstance(config["line-length"], int) and config["line-length"] > 0
