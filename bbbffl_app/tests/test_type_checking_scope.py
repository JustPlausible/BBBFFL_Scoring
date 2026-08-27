"""Proves the incremental mypy gate (issue #39) is what docs/ci-quality-gates.md
says it is, rather than relying on the CI YAML/pyproject.toml alone drifting
unnoticed: the documented scope and the enforced `[tool.mypy].files` list
must name exactly the same real, in-scope-quality files, and the strict
flags the docs promise must actually be set."""

import re
import tomllib
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_DIR.parent
PYPROJECT = APP_DIR / "pyproject.toml"
DOC = REPO_ROOT / "docs" / "ci-quality-gates.md"


def _configured_mypy_files():
    config = tomllib.loads(PYPROJECT.read_text())
    return config["tool"]["mypy"]["files"]


def _documented_mypy_files():
    text = DOC.read_text()
    match = re.search(r"<!-- mypy-scope:start -->\n```\n(.*?)```", text, re.DOTALL)
    assert match, "docs/ci-quality-gates.md is missing the mypy-scope fenced block"
    return [line for line in match.group(1).splitlines() if line.strip()]


def test_documented_scope_matches_configured_scope():
    assert _documented_mypy_files() == _configured_mypy_files()


def test_every_scoped_file_exists_and_is_a_real_app_module():
    files = _configured_mypy_files()
    assert files, "the mypy gate must cover at least one file"
    for relative_path in files:
        assert relative_path.startswith("app/"), f"{relative_path} is outside app/"
        assert (APP_DIR / relative_path).is_file(), f"{relative_path} does not exist"


def test_gate_enforces_real_annotations_not_a_token_check():
    config = tomllib.loads(PYPROJECT.read_text())
    mypy_config = config["tool"]["mypy"]
    for flag in (
        "disallow_untyped_defs",
        "disallow_incomplete_defs",
        "check_untyped_defs",
        "no_implicit_optional",
        "warn_redundant_casts",
        "warn_unused_ignores",
        "strict_equality",
    ):
        assert mypy_config.get(flag) is True, f"{flag} must be enabled for the gate to mean anything"
