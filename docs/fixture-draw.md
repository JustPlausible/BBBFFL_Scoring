# BBBFFL fixture draw

The regular-season rotation is a **BBBFFL historical domain rule**, not the
output of a generic round-robin scheduler. Its authority is the 2026 scorer
workbook evidence transcribed in `docs/plans/2026-workbook-findings.md`; the
season model and roadmap identify that table as the required golden fixture.
`app.fixtures.BASE_ROTATION` therefore records the table explicitly and gives
it the durable version `bbbffl-workbook-2026-v1`.

BBBFFL regular-season length is explicit season configuration in
`bbbffl_season.regular_season_round_count`; it defaults to 20 for compatibility
with 2026 and existing seasons are migrated to that value without regenerating
their draws. The nine base rounds are followed by the same nine rounds with
nominal home/away reversed. Round 19 begins the base rotation again, and this
18-round cycle continues deterministically until the configured season length.
For example, 23 rounds end with base rounds 1–5.

Each season has one fixture draw. Fixture numbers 1–10 point to
`season_entry.season_entry_id`, the stable season-specific licence/team entry,
not a coach, display name, AFL club, player, or insertion position. A draft is
saved atomically only when all ten entries from that season are present.
Corrections replace the complete draft and append an audit event containing
the before and after number maps.

Freezing means the scorer/operator has accepted the draw. The database then
rejects changes to its number assignments and its persisted pairings (five per
configured regular-season round), and
the application does not offer an unfreeze operation. Thus later code changes
to the generator cannot silently rewrite historical fixtures. The persisted
rotation version explains how the accepted facts originated.

Consumers should reference `season_fixture_matchup.fixture_matchup_id` for a
particular scheduled head-to-head and its home/away `season_entry_id` values
for participants. `bbbffl_round_number` is only the BBBFFL fixture round. The
configured BBBFFL season length is not inferred from an AFL API fixture.
No AFL-round equality or mapping is implied; the existing explicit mapping domain
owns that separate relationship. Match results and their lifecycle are also
deliberately outside this fixture package.
