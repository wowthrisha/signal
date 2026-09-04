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

Every phase uses two check types:

| Symbol | Meaning |
|--------|---------|
| `[ ]` | Not done |
| `[x]` | Done — command + output in ACTION-LOG.md |
| `[M]` | Machine check: automated command that can be copy-pasted and re-run |
| `[A]` | Adversarial check: a deliberately hostile probe that must pass |

Both `[M]` and `[A]` items require ACTION-LOG evidence before ticking.

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

```bash
grep -rn "datetime.now" backend/
```

Expected: zero matches in `backend/app/engine/`, `backend/app/ingest/`, `backend/app/db/`.
Permitted only in `backend/app/core/clock.py` (WallClock) and test fixtures.

---

_Last updated: 2026-09-04_
