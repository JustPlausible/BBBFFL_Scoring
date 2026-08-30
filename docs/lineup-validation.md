# Weekly lineup validation and availability advice

`LineupValidationService` is the reusable package-24 boundary for coach,
scorer-proxy, carry-forward and replay callers. A submission addresses exactly
the nine positions in `app.lineups.POSITIONS`, each either a stable
`season_player_id` or deliberately `null` (vacant), uses unique player values
across populated positions, selects players currently owned by the season
entry, and retains a consistent season/competition/round/entry scope.
Ownership is read only from package 21's ownership periods. Draft saves
remain deliberately permissive so a coach can build a lineup progressively.

A formal submission is **not** required to name a player in every position
(issue #98). A position present in the submitted mapping with a `null` value
is a deliberate vacancy -- legitimate, authoritative competition state, never
a validation failure -- and is reported back only as an advisory
`position_vacant` warning, never an error. This is distinct from a position
whose key is missing from the submitted mapping entirely, which remains the
hard `required_position_missing` error (unknown/corrupt input shape), and
from a scorer DNP ruling on a named player, which is a separate, later
decision recorded by `app.round_review`/`app.service` and never inferred
here. A vacancy is never satisfied by fabricating a player, a DNP ruling, or
a zero-score placeholder -- see `docs/weekly-lineups.md` and
`docs/lockouts.md` for how submission persistence and staged lockout treat
the same vacancy.

`ValidatedLineupSubmissionService` is the single application submission
boundary for coach drafts and explicit-position sources. Scorer proxy and
carry-forward both use it rather than calling the repository submission
methods directly. It performs validation before delegating the write to
`WeeklyLineupRepository.submit` or `submit_positions`. Callers pass package 34's
`lock_guard` unchanged; validation neither calculates nor bypasses staged
lockout. Failed hard validation produces `LineupValidationError.result` and no
submission.

Results contain `valid` plus structured messages with `severity`, `category`,
stable `code`, optional position/player identity, and factual `details`.
`error` blocks submission, `warning` is advisory, and `unknown` identifies
unavailable, indeterminate, or stale evidence. AFL club byes come from
`rounds[].byes` in the public afl-api v1 contract. A bye warning never removes a
selection, writes lineup/scoring state, or creates a DNP decision. In particular
`byes: null` is unknown while `byes: []` is factual evidence of no byes.
Freshness is obtained from `ResilientAflClient.evidence_batch()` around the
round read: cached fallback is stale even though the cached round value itself
is unchanged. Plain afl-api contract clients treat a successful live read as
current.
