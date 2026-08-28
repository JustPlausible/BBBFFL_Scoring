# AFL evidence fixtures (roadmap package 08 / issue #40)

Deterministic, provenance-rich, offline afl-api v1 evidence for BBBFFL's
replay foundation. Full documentation — directory/manifest convention,
evidence classifications, loader usage, validation, and the refresh/
addition policy — lives in
[`docs/afl-evidence-fixtures.md`](../../../../docs/afl-evidence-fixtures.md).

Load fixtures only through `tests.afl_evidence` (see that module's
docstring); do not read these files directly by path from test code, and
never edit a committed fixture to make a failing test pass — see the
"Never edit a captured fixture" rule in the doc above.

This directory is distinct from `tests/fixtures/afl_api_v1/`, which is
issue #18's smaller consumer-contract-pinning corpus and is out of scope
for this issue's classification/provenance/versioning system.
