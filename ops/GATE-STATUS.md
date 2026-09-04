# Gate Status

| Gate | Phase | Deadline | Criterion | Status |
|------|-------|----------|-----------|--------|
| G1   | F1    | T+4h     | Bars in DB, or hardcoded CSV | ✅ **PASS** (2026-09-04) — 127 distinct sessions, 0 null ISINs, 311,769 bars. Evidence: ACTION-LOG [F1.5] |
| G2   | F2    | T+9h     | Replay produces deterministic output twice | ✅ **PASS** (2026-09-04) — md5 e972c9ec… identical on both runs, diff clean, REPLAY DIGEST 6979368b… stable across 8 fault scenarios. Evidence: ACTION-LOG [F2.6] |
| G3   | S1    | T+20h    | z-scores sane on real data | ✅ **PASS** (2026-09-05) — `--assert-sane` exit 0; pooled mean −0.0221, sd 1.0993, 0 non-finite over 205,383 residuals. Evidence: ACTION-LOG [S1.4] |
| G4   | S2    | T+24h    | CUSUM stable, or ship D1 only | ✅ **PASS** (2026-09-05) — alerts_per_user_day **0.0446** at k=5 on a held-out window vs a ≤3.0 budget (67× under). **D2 kept**; cut rule remains executable. Evidence: ACTION-LOG [S1.5], [S1.6] |
| G5   | S4    | T+35h    | SUBMIT v1.0 non-negotiable | ⬜ PENDING |
| G6   | U1    | T+46h    | B0/B1/B2 metrics generated | ⬜ PENDING |
| G7   | U2    | T+51h    | Announcement feed reliable | ⬜ PENDING |
| G8   | U4    | T+61h    | Fresh-clone test on clean machine | ⬜ PENDING |

---

Gate status is set only from `[M]`/`[A]` evidence in `ops/ACTION-LOG.md`.
A `[G]` (grep) result can never move a gate to PASS. See [VERIFY.md](VERIFY.md).

_Last updated: 2026-09-05_

---

## Gate 3 — evidence summary

```
z distribution SANE
exit=0
```

| | session 2026-09-03 | pooled, 86 sessions |
|---|---|---|
| symbols scored | 2,463 of 2,883 | 205,383 residuals |
| mean | +0.3590 | **−0.0221** |
| sd | 0.9574 | **1.0993** |
| non-finite / NaN | 0 / 0 | 0 |
| fraction \|z\| > 2 | 0.0499 | 0.0545 |

Criterion is \|mean\| < 0.5 and sd in [0.8, 1.5]. Met on both the single session
and the pooled distribution.

## Gate 4 — evidence summary

| | held out (9 sessions) | full warm window (26 sessions) |
|---|---|---|
| D1 signals | 341 | 1,295 |
| D2 signals | 132 | 586 |
| admitted cards | 225 (25.0/session) | 1,008 (38.8/session) |
| **alerts_per_user_day** (k=5) | **0.0446** | 0.0673 |
| analytic cross-check | 0.0434 | 0.0672 |
| budget | ≤ 3.0 | ≤ 3.0 |

Selected `h1 = 3.0`, `h2 = 4.0` — the loosest point on a 9 × 8 grid, every point
of which fits the budget. **D2 was not cut.** The held-out window is 9 sessions
(see R-10); the margin is 67× and the whole grid spans 0.055–0.079, so the
verdict does not turn on the window size.
