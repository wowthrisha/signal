# VERIFY.md — Ops Verification Protocol

## Core rule

**A box is ticked only after the real command and its real output appear in `ops/ACTION-LOG.md`.**
No output → no tick. No exceptions.

```
Tick flow:
  1. Run the command locally.
  2. Paste the exact command + full stdout/stderr into ACTION-LOG.md under the correct phase.
  3. Return here and tick the box.
```

---

## Phase-gate checklist structure

Every phase uses three check types:

| Symbol | Proves | Can close a gate? |
|--------|--------|-------------------|
| `[G]` | a string exists in source | NO — never |
| `[M]` | observed behaviour: test passes, SQL count, hash match, HTTP response | YES |
| `[A]` | a hostile probe survives | YES |

A `[G]` passing while its paired `[M]` is absent means NOT DONE. If any
assistant reports a gate closed on grep evidence alone, that is a false
report — reopen the gate. Greps are navigation hints, never proof.

Every `[G]` in a checklist MUST be followed by the `[M]` (or `[A]`) that observes
the corresponding behaviour. A run of closely related `[G]`s may share one paired
check. A `[G]` with no paired check anywhere below it is an unfinished checklist
item, not a check.

Box state markers:

| Marker | Meaning |
|--------|---------|
| `[ ]` | Not done |
| `[x]` | Done — command + output in ACTION-LOG.md |

Both `[M]` and `[A]` items require ACTION-LOG evidence before ticking. A `[G]`
never closes anything, so it never needs evidence and is never ticked — an
unticked `[G]` is the normal, correct state for a navigation hint.

---

## Phase checklists

See individual files in `ops/`:

| File | Phase | Spec sections |
|------|-------|--------------|
| [CHK-F1-ingest.md](CHK-F1-ingest.md) | F1 — Data ingest & DB | §5 §16 |
| [CHK-F2-ledger.md](CHK-F2-ledger.md) | F2 — Ledger & replay | §5 §13 |
| [CHK-S1-detect.md](CHK-S1-detect.md) | S1 — Detection (attribution, EWMA, D1/D2) | §4 §7 §8 |
| [CHK-S3-api-ui.md](CHK-S3-api-ui.md) | S3 — API, auth, digest, cursor | §5 §11 §20 |
| [CHK-U1-evidence.md](CHK-U1-evidence.md) | U1 — Benchmark & ablation | §14 §15 §30 |

---

## Risk register

Open risks tracked in [RISK-REGISTER.md](RISK-REGISTER.md). Review before each phase.

---

## Evidence format for ACTION-LOG.md

```markdown
## [PHASE] — [item description]

**Command:**
```bash
<exact command>
```

**Output:**
```
<full stdout/stderr — do not truncate>
```

**Date:** YYYY-MM-DD HH:MM IST  
**Status:** PASS | FAIL | PARTIAL
```

---

## Invariant: no datetime.now() in engine

Before Gate 3 and Gate 8, run:

`[G]` — navigation hint only, cannot close a gate:

```bash
grep -rn "datetime.now" backend/
```

Expected: zero matches in `backend/app/engine/`, `backend/app/ingest/`, `backend/app/db/`.
Permitted only in `backend/app/core/clock.py` (WallClock) and test fixtures.

`[M]` — the behavioural check that actually closes it. A grep cannot prove the
absence of a wall-clock read (`time.time()`, `date.today()`, `pd.Timestamp.now()`
all evade it). Run the engine under a `FixedClock` and assert every persisted
timestamp equals the injected instant:

```bash
cd backend && python -m pytest tests/test_clock_injection.py -v
```

---

_Last updated: 2026-09-04_
