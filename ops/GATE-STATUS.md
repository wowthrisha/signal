# Gate Status

| Gate | Phase | Deadline | Criterion | Status |
|------|-------|----------|-----------|--------|
| G1   | F1    | T+4h     | Bars in DB, or hardcoded CSV | ✅ **PASS** (2026-09-04) — 127 distinct sessions, 0 null ISINs, 311,769 bars. Evidence: ACTION-LOG [F1.5] |
| G2   | F2    | T+9h     | Replay produces deterministic output twice | ✅ **PASS** (2026-09-04) — md5 e972c9ec… identical on both runs, diff clean, REPLAY DIGEST 6979368b… stable across 8 fault scenarios. Evidence: ACTION-LOG [F2.6] |
| G3   | S1    | T+20h    | z-scores sane on real data | ⬜ PENDING |
| G4   | S2    | T+24h    | CUSUM stable, or ship D1 only | ⬜ PENDING |
| G5   | S4    | T+35h    | SUBMIT v1.0 non-negotiable | ⬜ PENDING |
| G6   | U1    | T+46h    | B0/B1/B2 metrics generated | ⬜ PENDING |
| G7   | U2    | T+51h    | Announcement feed reliable | ⬜ PENDING |
| G8   | U4    | T+61h    | Fresh-clone test on clean machine | ⬜ PENDING |

---

Gate status is set only from `[M]`/`[A]` evidence in `ops/ACTION-LOG.md`.
A `[G]` (grep) result can never move a gate to PASS. See [VERIFY.md](VERIFY.md).

_Last updated: 2026-09-04_
