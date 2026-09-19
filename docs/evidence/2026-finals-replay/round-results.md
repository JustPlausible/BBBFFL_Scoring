# 2026 Finals/SuperScore replay execution summary

This is the sanitised execution record for the real 2026 Finals/SuperScore
replay. It deliberately avoids private lineup data, backup filenames,
credentials and hashes.

## Phase result

All four Finals weeks and all four SuperScore rounds were completed and
published. The closeout completion preview reported all eight round
lifecycles as `final`.

| Phase | Round id | Result |
|---|---|---|
| Finals Week 1 | `d522fb1d-754a-4f38-bb41-517fb95d377a` | completed/published |
| SS1 | `e66cf88c-82eb-4a54-96da-fce6052faf0b` | completed/published |
| Finals Week 2 | `24e12fd8-ace3-4900-a787-96daed4ac6db` | completed/published |
| SS2 | `96854834-d9b0-49d4-a78e-4462883d485a` | completed/published |
| Preliminary Final | `e622a000-6202-4ad4-bb6c-9cc61130f361` | completed/published |
| SS3 | `272cf9fb-0b98-42aa-a6dc-c507fa89e2e0` | completed/published |
| Grand Final | `9554e5af-2362-45be-ab0b-5edb9275f5bb` | completed/published |
| SS4 | `292f72f7-0a01-4a00-b4ef-9f5251968852` | completed/published |

The Grand Final pairing was Running Hots v Evil Absolutes. Grand Final and
SS4 results were checked against the operator's historical records and
matched.

## Execution coverage

The replay exercised authenticated Coach submission through the final two
rounds, Finals-only eligibility, SuperScore access for all eligible teams,
cross-Coach private-lineup isolation, delegated Scorer entry,
missed-submission adjudication, locked-lineup correction, DNP/Interchange
decisions, calculation/review/finalisation/publication, bracket
progression, and a Preliminary Final main-only lockout configuration.

## Closeout

The season initially remained in `setup`, which caused completion preview
to refuse. It was transitioned through the supported audited
`setup -> active` path. The next preview returned `ready: true`; the
atomic completion moved the season to `completed` version 3. Independent
archival verification matched completion event
`9cd65eee-d6ec-43b5-bcb7-23b275ac227a` before the final archive was taken.

## Evidence limitations

Exact per-round backup filenames/hashes and several per-round audit-event
ids were not retained in the closeout conversation record and are not
reconstructed here. The repository records this as a provenance gap rather
than inventing evidence. The terminal pre-closeout and post-completion
archives were both preserved privately, and the final archive passed
`pg_restore --list` plus checksum verification.
