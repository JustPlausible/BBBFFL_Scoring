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

Missing player-stat rows remain visible as selected DNP evidence. Upstream null
stat fields remain null and make that slot's calculated score incomplete rather
than being converted to zero. Interchange is retained as an unscored selection;
recommendations, assignments, scorer rulings, review and publication remain
deferred to packages 27 and 28.
