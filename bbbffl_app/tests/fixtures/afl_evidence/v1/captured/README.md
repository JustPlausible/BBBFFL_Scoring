# `captured` evidence — currently empty

This directory is reserved for **genuinely captured** afl-api v1 evidence:
a real HTTP response recorded from a live deployment, with `derivation:
"live_capture"` and a real `captured_at` timestamp in its provenance.

It intentionally contains no fixtures yet. This session's outbound network
access to the configured `afl-api` deployment is blocked by organisation
egress policy (confirmed via the local agent proxy — see
`docs/afl-api-v1-contract.md`'s "Live validation status" for the same,
still-outstanding restriction recorded against issue #18). Fabricating a
fixture here and labelling it "captured" would misrepresent synthetic data
as authoritative AFL evidence, which issue #40 explicitly prohibits.

See `docs/afl-evidence-fixtures.md` ("Adding a new capture") for the
procedure to populate this directory once network access to a real
deployment is available. Every other classification in this corpus
(`synthetic`, `unresolved`) is populated today; only this one, and
`captured_bbbffl_historical`, wait on that access.
