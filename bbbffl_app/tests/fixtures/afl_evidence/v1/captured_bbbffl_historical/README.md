# `captured_bbbffl_historical` evidence — currently empty

This directory is reserved for BBBFFL-side historical evidence captured
from BBBFFL's own operation (e.g. a past round's persisted scoring
snapshot, lineup submission, or lockout activation) that a later replay
checkpoint (roadmap packages 32–36) wants to compare against curated
afl-api evidence, distinct from afl-api's own authoritative records.

It intentionally contains no fixtures yet: this issue (#40) curates
afl-api evidence and the loader/classification infrastructure only; no
2026 BBBFFL round has produced historical evidence for this repository to
capture yet. Populate this directory the same way as `captured/` (see
`docs/afl-evidence-fixtures.md`) once there is real BBBFFL history to
record, using `derivation: "bbbffl_recorded"`.
