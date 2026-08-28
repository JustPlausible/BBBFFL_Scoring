# Weekly lineup validation and availability advice

`LineupValidationService` is the reusable package-24 boundary for coach,
scorer-proxy, carry-forward and replay callers. A submission must fill exactly
the nine positions in `app.lineups.POSITIONS`, use unique stable
`season_player_id` values, select players currently owned by the season entry,
and retain a consistent season/competition/round/entry scope. Ownership is read
only from package 21's ownership periods. Draft saves remain deliberately
permissive so a coach can build a lineup progressively.

`ValidatedLineupSubmissionService` performs that validation before delegating
the write to `WeeklyLineupRepository.submit`. Callers pass package 34's
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
