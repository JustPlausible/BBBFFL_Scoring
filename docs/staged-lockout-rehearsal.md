# Staged progressive-lockout rehearsal (issue #91)

This deterministic replay scenario supplements the successful issue #85
single-match Round 1 rehearsal; it does not replace or invalidate that broader
coach-to-scorer-to-public rehearsal. This run focuses narrowly on the real
coach draft/submission surface and the persisted BBBFFL lockout plan.

## Initialise

Run every command in this document from the repository's `bbbffl_app`
directory. Use a disposable database and evidence file, then start the
application in replay mode exactly as for the
[Round 1 rehearsal](round1-rehearsal.md):

```bash
cd bbbffl_app
python -m scripts.staged_lockout_rehearsal bootstrap \
  --database-url sqlite:///data/staged-lockout-rehearsal.db \
  --evidence-path data/staged-lockout-rehearsal-evidence.json
export BBBFFL_DATABASE_URL=sqlite:///data/staged-lockout-rehearsal.db
export BBBFFL_AFL_MODE=replay
export BBBFFL_AFL_REPLAY_EVIDENCE_PATH=data/staged-lockout-rehearsal-evidence.json
uvicorn app.main:app --reload
```

Sign in with the Coach A rehearsal credentials printed by the issue #85
bootstrap (`coach.a@rehearsal.bbbffl.local` / `Round1Rehearsal!25`) and open
the Round 1 lineup from `/account`. Save and submit the initial nine-player
lineup before advancing.

## Persisted plan and stages

The plan is configuration, not inferred fixture chronology:

| BBBFFL trigger | AFL matches | Activation evidence |
| --- | --- | --- |
| Selective A | 9101 | match 9101 becomes `LIVE` |
| Selective B | **9102 and 9103** | match 9102 becomes `LIVE`; 9103 remains `UPCOMING` |
| Main | 9105 | match 9105 becomes `LIVE` |

A separate match 9104 is already `LIVE` in the **initial** stage but belongs
to no selective trigger. Its M1 player remains editable until Main, proving
that a player's own match start is not an independent BBBFFL lock.

Advance with the following command, replacing `STAGE` in order with
`selective-a`, `selective-b`, and `main`:

```bash
python -m scripts.staged_lockout_rehearsal advance STAGE \
  --evidence-path data/staged-lockout-rehearsal-evidence.json
```

Restart the application after each command (replay evidence is intentionally
loaded eagerly). The commands perform no sleeps. Every evidence stage embeds
the same explicit replay evaluation instant, `2000-02-01T00:00:00Z`, before
all scheduled boundaries. Consequently even the deliberately past fixture
dates cannot expire against the operator machine's wall clock; only the named
status changes activate triggers:

| Stage | Expected coach view and action |
| --- | --- |
| `initial` | All positions editable; Save Draft and Submit succeed. |
| `selective-a` | F1 (match 9101) locked; the same lineup has editable positions. A crafted F1 change fails clearly; edits involving only editable positions save and submit. |
| `selective-b` | F1 remains locked; F2 (9102) **and F3 (still-upcoming 9103)** lock together. M1 (already-live, uncovered 9104) remains editable. Locked edits fail; an M1/other-editable edit saves and submits. |
| `main` | Every remaining ordinary position locks regardless of its own match status; no player change submits. |

At each stage the coach page displays locked/editable badges and the AFL match
behind each lock. The operator can inspect the effective lineup and immutable
submission-version history in the normal database; rejected submissions must
not replace the previous authoritative lineup. The scorer should observe no
special alternate lockout state: these are the same durable trigger activation
and lineup-lock records used by production services.

## Reset

Stop the app and delete the disposable SQLite database and evidence file, then
run `bootstrap` again:

```bash
rm -f data/staged-lockout-rehearsal.db \
  data/staged-lockout-rehearsal-evidence.json
```

This path matches the explicit `cd bbbffl_app` at initialization. The same
`bootstrap` command can now be copied again to initialise a clean rehearsal.

Opening Round deferred nominations remain a separate composed guard documented
in [Opening Round deferred selection](opening-round-deferred-selection.md).
This ordinary-round scenario neither creates nominations nor changes those
semantics; the existing focused Opening Round suite remains the regression
authority.
