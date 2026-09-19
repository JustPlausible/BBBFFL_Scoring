# 2026 Finals/SuperScore replay UX findings

These are non-blocking usability observations from operating the real 2026
Finals/SuperScore replay.

## Scorer navigation

The browser Scorer workflow became preferable to the original CLI-heavy
procedure for routine operation. The main remaining navigation gap was
Finals preflight discoverability: the preflight index stopped at Round 20
and the Scorer next-action cue did not provide a direct route to the
relevant Finals preflight. This is tracked by issue #221.

## Coach weekly-selection ordering

The Account weekly-selection history can interleave ordinary, Finals and
SuperScore rounds awkwardly because internal sequence numbers collide. For
2027, present ordinary Rounds 1-20 first, then the Finals/SuperScore phase,
with same-week Finals and SuperScore entries visually paired where
applicable.

## Same-week Finals/SuperScore copy-to-draft

A Coach participating in both streams would benefit from an optional
same-week copy action in either direction. It should copy into the
destination private draft only, never auto-submit, never bind the two
lineups, and must revalidate destination eligibility, roster, positions and
lockout state.

## Carry-forward versus private draft

A carry-forward submission can coexist with private draft changes in a way
that makes the authoritative submitted lineup difficult to understand.
Future UI should distinguish clearly between the effective submitted team
and unsaved/private changes.

## Scorer dashboard presentation

The explanatory workflow cards should keep their heading on a full-width
row with the cards aligned beneath it while preserving responsive wrapping.
This presentation tidy-up is included in issue #221.

## Pre-2027 rehearsal

A short beta/usability rehearsal with a non-technical Scorer is recommended
before the 2027 live season. The objective should be to confirm that normal
weekly and Finals tasks can be completed from supported browser navigation
without knowing UUIDs, CLI commands or manually constructed URLs.
