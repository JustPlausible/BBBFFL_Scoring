# Season scoring calculations

`app.calculations.MatchupCalculationService` is the regular-season calculation
boundary. It resolves the rule version selected by the persisted competition,
the persisted fixture matchup, and each entry's effective immutable submitted
lineup. It adapts those inputs to `app.scoring.score_position`; replay and live
operation therefore use the same formula implementation as the prototype.

Rules versions may carry a JSON coefficient payload. A missing payload means the
established BBBFFL coefficients, retaining compatibility with historical rows.
Changing rules requires a new append-only `season_rules_version`; competitions
never infer rules from the current year.

The current calculated row records stable season, rules, round, matchup, entry,
lineup-version and upstream identities. Its fingerprint excludes the observation
clock but includes all scoring evidence: identical facts are idempotent, while a
changed AFL stat produces a new revision only for matchups containing that fact.
The replaceable calculation row has no lifecycle pointer to, and cannot publish
or overwrite, immutable `bbbffl_official_result` rows.

`matchup_id` is the sole uniqueness boundary for current calculated state.
`input_fingerprint` is ordinary provenance used by the atomic matchup upsert to
compare inputs; it is not a second identity or a calculation-history key.

Missing player-stat rows remain visible as selected DNP evidence. Upstream null
stat fields remain null and make that slot's calculated score incomplete rather
than being converted to zero. Interchange is retained as an unscored selection;
recommendations, assignments, scorer rulings, review and publication remain
deferred to packages 27 and 28.

## Per-slot scoring source (issue #69)

Not every slot in a round necessarily draws from the round's ordinarily
mapped AFL round. A slot with an active Opening Round deferred nomination
(`app.opening_round`, see
[`opening-round-deferred-selection.md`](opening-round-deferred-selection.md))
resolves its match/stats from the player's AFL Opening Round instead, via
`_RoundFacts`'s per-AFL-round match/stat cache -- while every other slot in
the same lineup is unaffected. Both paths call the identical
`app.scoring.score_position` formula; there is no second scoring engine.
Each slot's evidence in the calculated snapshot records `scoring_source`
(`"ordinary"` or `"opening_round_deferred"`) and `source_afl_round_id`, so a
mixed-source round's provenance stays inspectable wherever the snapshot is
read (`app.round_review`, replay tooling).
