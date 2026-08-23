# BBBFFL 2026 scorer workbook findings

## Purpose

This document records a structural/domain audit of the original 2026 scorer workbook (`.xls`) and a light consistency check against the 2021–2025 workbooks.

The workbook is evidence of how BBBFFL has historically been operated. It is **not automatically the league constitution**: human-confirmed rules in `2027-season-model.md` take precedence where spreadsheet implementation details and league intent differ.

## Scope and confidence

The 2026 workbook was inspected without modifying the original file. A temporary converted copy was used only to make formulas and cached values easier to analyse.

The 2021–2025 files were checked primarily for structural consistency rather than exhaustively re-audited. All six seasons use the same core workbook shape and strongly support treating 2026 as a representative modern scorer workbook:

- `Team List`
- `Fixtures`
- `Results`
- `Ladder`
- `Superscore`
- `Records`
- `biggest margins team v team`
- `Previous Ladders`
- `recent draft history`

The same Results/finals/SuperScore structures recur across 2021–2025, with expected season-specific changes such as AFL bye annotations and expanding historical/draft data.

## 2026 workbook scale

The workbook contains nine visible sheets and more than 25,000 populated cells. Major formula-driven sheets include approximately:

- `Results`: 10,269 populated cells / 3,513 formulas
- `Fixtures`: 965 populated cells / 801 formulas
- `Ladder`: 356 populated cells / 343 formulas
- `Superscore`: 1,526 populated cells / 481 formulas
- `Team List`: 920 populated cells / 201 formulas

This confirms that the workbook is effectively a legacy scorer application plus historical archive, rather than merely a weekly results report.

## Confirmed domain behaviour

### Weekly lineup and scoring

The Results and SuperScore sheets independently confirm the modern lineup structure:

- three Forwards;
- three Midfielders;
- one Ruckman;
- one Tackler;
- one Interchange.

They also confirm the current scoring rules already captured in the season model:

- Forward = `(goals * 6) + behinds`;
- Midfielder = disposals;
- Tackler = `tackles * 6`;
- Ruck = hit-outs + marks;
- Interchange normally contributes zero unless activated as a replacement.

The legacy workbook converts non-forward positional totals into football-style `Goals.Behinds` notation for presentation. For example, a 27-point Midfielder is represented as `4.3`, because `4*6+3=27`. This is a presentation convention, not the underlying fantasy scoring rule.

The successful 2026 web prototype's `Goals.Behinds (Total)` presentation therefore has historical precedent.

### Interchange evidence

The workbook contains real historical examples where a normal scoring slot is vacant and the Interchange row carries the replacement score. This confirms that the underlying substitution/loophole behaviour predates the new application.

The spreadsheet implementation leaves the target slot blank and scores the Interchange row. The new domain model should **not** copy that representation literally. The richer application model should retain:

- originally selected player/slot;
- DNP or intentional vacancy;
- selected Interchange player;
- effective replacement position;
- score generated under that position's formula;
- scorer ruling/audit information.

### Fixture-number draw and exact rotation

The workbook confirms the second random draw assigns fixture numbers 1–10 after the player draft.

The exact modern nine-round rotation in fixture-number form is:

| Round | Matchups |
| --- | --- |
| 1 | 1–2, 3–4, 5–6, 7–8, 9–10 |
| 2 | 1–3, 2–8, 9–6, 7–4, 10–5 |
| 3 | 1–4, 2–6, 3–5, 8–10, 7–9 |
| 4 | 1–6, 2–4, 5–7, 3–10, 8–9 |
| 5 | 1–5, 2–7, 4–10, 3–9, 6–8 |
| 6 | 1–7, 2–9, 4–5, 6–10, 3–8 |
| 7 | 1–8, 2–10, 4–6, 3–7, 5–9 |
| 8 | 1–9, 2–5, 4–8, 3–6, 7–10 |
| 9 | 1–10, 2–3, 4–9, 5–8, 6–7 |

Rounds 10–18 repeat the same pairings with nominal home/away reversed. Rounds 19 and 20 repeat the Round 1 and Round 2 pairings respectively in the 24-round AFL structure.

This is now strong enough to treat as the legacy fixture algorithm rather than merely an illustrative example. It should still be regression-tested against historical workbooks when implemented.

### Ladder calculations

The workbook directly confirms:

- 4 premiership points for a win;
- 2 for a draw;
- 0 for a loss;
- Points For and Points Against accumulated from match totals;
- percentage = `Points For / Points Against * 100`;
- Points Per Game retained as a separate informational value.

The workbook visibly orders teams with equal premiership points by percentage. The season model's later Codex clarification adds Points For as the next ordering criterion; this is consistent with the workbook retaining that value, although an exact multi-way historical tiebreak should remain human-defined rather than inferred solely from Excel sorting behaviour.

### Finals structure

The Results and Fixtures sheets confirm the top-five, four-week finals structure documented in the season model:

- Week 1: Elimination Final (4th v 5th), Qualifying Final (2nd v 3rd), 1st has a bye;
- Week 2: First Semi (EF winner v QF loser), Second Semi (1st v QF winner);
- Week 3: Preliminary Final (First Semi winner v Second Semi loser);
- Week 4: Grand Final (Second Semi winner v Preliminary Final winner).

The legacy formulas themselves use simple W/L/D cells and are not a safe specification for tied-finals advancement. The human-confirmed season rule in `2027-season-model.md` — higher home-and-away ladder position advances/wins a tied final, subject to scorer confirmation — should be authoritative.

### SuperScore

The workbook confirms four separate SuperScore scoring blocks, each using the same positional/scoring shape as ordinary BBBFFL lineups.

SuperScore is implemented as its own selection/result stream, supporting the domain requirement that a finals participant may have a separate premiership-finals team and SuperScore team for the same AFL round.

The carry-forward rules clarified during PR review (SS1 may derive from the most recent ordinary lineup when no SuperScore lineup exists; SS2+ use the prior SuperScore lineup) remain human-domain rules rather than something that should be inferred from the spreadsheet formulas.

## AFL bye rounds — workbook evidence requiring explicit domain wording

The 2026 Fixtures sheet explicitly annotates ordinary AFL bye rounds, for example groups of AFL clubs on bye in particular BBBFFL rounds. The 2024 and 2025 workbooks contain the same type of annotations, and the historical Records sheet marks some low-score records with `* denotes bye round`.

This is important evidence that BBBFFL **continues to play normal head-to-head rounds while AFL clubs are on ordinary byes**. Historically, bye-affected team scores can be low enough to be specially annotated in the record book.

What should still be human-confirmed is the precise rule wording:

- are coaches simply expected to select around AFL byes from their available squad;
- is an AFL bye ever treated as a DNP eligible for Interchange replacement (current interpretation: probably not, because the player was never expected to play);
- are there any exceptional bye provisions beyond the historical Opening Round/deferred-stat mechanism already documented?

Until confirmed, implementation tools should not infer that an AFL bye is equivalent to a DNP.

## Historical records

The 2026 `Records` sheet is a manually maintained record book dating from 2004 and contains at least:

- highest team score;
- lowest team score;
- highest winning margin;
- highest combined Midfield score;
- lowest combined Midfield score;
- highest individual Midfield score;
- highest combined Forward score;
- lowest combined Forward score;
- highest individual Forward score;
- highest Ruck score;
- highest Tackler score;
- lowest winning score;
- highest losing score.

Historical annotations include:

- `* denotes bye round`;
- `** denotes COVID shortened quarters`;
- other season-context notes.

The future system should therefore allow a derived record to retain contextual notes/season metadata where the raw score alone does not tell the whole historical story.

### Record-scope ambiguity

The workbook proves that records are historically maintained, but it does not safely answer whether every competition stream should contribute equally to the main BBBFFL record book.

This should be confirmed with coaches/scorer before automatic historical record generation is implemented:

- Do ordinary home-and-away and premiership finals results both count toward the main league records?
- Do SuperScore performances count toward the same all-time positional/team records, or is SuperScore historically separate?
- Should Opening Round/deferred or ordinary bye-affected results remain eligible but simply be annotated, as the existing workbook suggests?

## Historical ladders and club/coach identity

`Previous Ladders` contains season ladders and premiership history dating to 2004, plus additional all-time analyses. It is a promising migration source for:

- final ladders;
- premiers/runners-up;
- all-time ladder summaries;
- top-five appearances;
- historical win/loss information where available.

Because coaches have changed team names and coaches have entered/left the competition, historical migration should not assume a team-name string is a stable identity. The season model's `Coach -> Season Entry -> Team Name` approach remains the appropriate target representation.

## Draft-history evidence

`recent draft history` is already a substantial multi-season archive. It should be treated as a likely source for future features such as:

- historical overall pick;
- draft round;
- prior BBBFFL owner/team;
- player draft-position trends between seasons.

A separate migration audit should map its columns and year coverage before implementation. This is not required to settle 2027 league rules.

## 2021–2025 consistency check

The five additional workbooks strongly support the 2026 workbook as the correct modern reference point:

- all contain the same nine core sheets;
- Results maintains the same large repeated round structure;
- all contain the same four-week finals labels and bracket shape;
- all contain `SUPERSCORE ROUND 1` and the repeated SuperScore structure;
- recent years annotate AFL bye rounds in Fixtures;
- historical ladder/record/draft sheets expand over time rather than representing a completely different application.

For current requirements work there is no need to deep-audit all five older seasons before Grand Final day. They should be retained as regression/migration evidence after the 2026 domain model is settled.

## Legacy implementation details not to copy blindly

The following are useful evidence but should not dictate the new architecture:

1. Non-forward points are stored/displayed as pseudo football scores in spreadsheet cells.
2. Activated Interchange points may physically remain in the Interchange row rather than the target position.
3. Ladder and fixtures use long hard-coded cell-reference formula chains.
4. Finals progression formulas do not by themselves safely encode every human tie/ruling edge case.
5. Historical records are manually maintained rather than reproducibly derived.

The rebuilt application should represent domain facts explicitly and generate presentation from those facts.

## Items to feed into 2026 replay acceptance testing

The workbook gives concrete data for replaying:

- all 20 regular-season fixture pairings;
- weekly submitted players and scores where populated;
- ladder accumulation;
- finals seeding/progression;
- four SuperScore rounds;
- real Interchange activation examples;
- AFL bye-affected rounds;
- historical record comparisons.

This makes the 2026 workbook a primary replay reference alongside WhatsApp submissions and `afl-api` historical match/player data.

## Grand Final-day questions exposed by workbook audit

The following are worth resolving with coaches/scorer now because the workbook exposes genuine ambiguity that software should not guess:

1. **Normal AFL byes:** confirm that a player whose AFL club has a scheduled bye is simply unavailable for selection/scoring and is not a DNP that can activate Interchange; confirm there are no other ordinary-bye exceptions.
2. **All-time record scope:** confirm whether premiership-finals scores count in the normal all-time BBBFFL records and whether SuperScore performances do or do not count in those same records.
3. **Bye-affected records:** confirm that ordinary bye-round scores remain eligible for records, with contextual annotation where useful, rather than being excluded.

These are in addition to the unresolved loophole, partial-submission and 2027 rule/configuration questions already listed in `2026-grand-final-coach-discussion.md`.