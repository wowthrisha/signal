# CHK-S1 — Detection: Attribution, EWMA, D1/D2 (§4, §7, §8)

**Gate 3 criterion (spec §25):** z-scores sane on real data.  
**Deadline:** T+20h  
**Evidence location:** `ops/ACTION-LOG.md` under `[S1]`

> A box is ticked only after the real command + real output are in ACTION-LOG.md. No output → no tick.

---

## §8 Attribution — Orthogonalized Two-Factor OLS

### Regression correctness

- [ ] **[M]** `r_sec⊥` is the residual of sector ~ market OLS (Step 1), not raw sector return
  ```bash
  grep -rn "r_sec_perp\|sec_perp\|ortho\|orthogonalized" backend/app/engine/ | grep -v ".pyc"
  ```

- [ ] **[M]** Estimation window = 120 sessions, minimum = 60 bars
  ```bash
  grep -rn "120\|60" backend/app/engine/ | grep -i "window\|min_obs\|min_history"
  ```

- [ ] **[M]** IPO shrinkage toward sector mean implemented (`w = n/(n+60)`)
  ```bash
  grep -rn "shrink\|n.*n.*60\|sector_mean" backend/app/engine/ | grep -v ".pyc"
  ```

- [ ] **[A]** Beta decomposition sums to total return (decomposition is exact)
  ```bash
  python -m pytest backend/tests/ -k "attribution or decomp" -v
  ```

### Market-wide crash suppression (§8)

- [ ] **[M]** Breadth guard: `breadth_t > 0.5` → one MARKET_REGIME card, individual JUMPs suppressed
  ```bash
  grep -rn "breadth\|MARKET_REGIME" backend/app/engine/ | grep -v ".pyc"
  ```

- [ ] **[A]** On a crash session (>50% of symbols with |z| > 2): output is exactly one MARKET_REGIME event
  ```bash
  python -m pytest backend/tests/ -k "market_regime or crash" -v
  ```

---

## §4 Detector Specification — D1 (Jump) and D2 (CUSUM)

### EWMA volatility

- [ ] **[M]** λ = 0.94 is hardcoded (not configurable without a comment explaining why)
  ```bash
  grep -rn "0.94\|lambda.*0.9\|ewma" backend/app/engine/ | grep -v ".pyc"
  ```

- [ ] **[M]** Volatility floor `σ̂_t ≥ σ_floor` (5th percentile of cross-sectional σ) applied
  ```bash
  grep -rn "sigma_floor\|σ_floor\|vol_floor\|percentile" backend/app/engine/ | grep -v ".pyc"
  ```

### D1 — Jump detector

- [ ] **[M]** D1 fires on `|z_t| >= h1` (starting value h1 = 3.0)
  ```bash
  grep -rn "h1\|h_1\|jump.*3\.0\|3\.0.*jump" backend/app/engine/ | grep -v ".pyc"
  ```

- [ ] **[M]** Overnight/weekend gap → D1 only (excluded from CUSUM accumulator)
  ```bash
  grep -rn "gap\|overnight\|weekend" backend/app/engine/ | grep -v ".pyc"
  ```

- [ ] **[A]** z-scores sanity check on real bhavcopy data (Gate 3 evidence)
  ```bash
  python -m signal.detect --date 2026-09-03 --report-zscore | head -20
  ```
  _Expected: z-scores roughly Gaussian, |mean| < 0.5, std 0.8–1.5_

### D2 — CUSUM drift detector (§4)

- [ ] **[M]** CUSUM parameters: k = 0.5, h2 = 4.0 (starting values)
  ```bash
  grep -rn "k.*0\.5\|h2\|h_2\|4\.0.*cusum\|cusum.*4\.0" backend/app/engine/ | grep -v ".pyc"
  ```

- [ ] **[M]** Two-sided CUSUM: S⁺ and S⁻ both tracked; reset firing arm to 0 on alarm
  ```bash
  grep -rn "S_plus\|S_pos\|S_neg\|S_minus\|s_plus\|s_minus" backend/app/engine/ | grep -v ".pyc"
  ```

- [ ] **[M]** Cooldown: D2 alarms suppressed for 3 bars on the same symbol after an alarm
  ```bash
  grep -rn "cooldown\|cool_down\|suppress.*3\|3.*bar" backend/app/engine/ | grep -v ".pyc"
  ```

- [ ] **[A]** Gate 4 trigger: if CUSUM is unstable → D1 only, ablation row D marked DROPPED
  ```bash
  # Verify D2 can be disabled via config
  grep -rn "D1_ONLY\|d1_only\|disable_cusum\|cusum.*flag" backend/app/ configs/ | grep -v ".pyc"
  ```

### Warm-up states (§4)

- [ ] **[M]** n_obs < 60 → WARMUP: D2 disabled, D1 uses cross-sectional σ prior, confidence ≤ 0.5
  ```bash
  grep -rn "WARMUP\|warmup\|60\|n_obs" backend/app/engine/ | grep "warmup\|WARMUP"
  ```

- [ ] **[M]** n_obs < 20 → no detection at all
  ```bash
  grep -rn "n_obs.*20\|20.*n_obs\|min.*20" backend/app/engine/ | grep -v ".pyc"
  ```

### Missing data (§4)

- [ ] **[M]** Forward-fill capped at 1 bar; beyond that status = STALE, confidence = 0
  ```bash
  grep -rn "STALE\|forward_fill\|ffill" backend/app/ | grep -v ".pyc"
  ```

- [ ] **[A]** Missing data test: drop one bar from a symbol's history, verify STALE event emitted
  ```bash
  python -m pytest backend/tests/ -k "stale or missing" -v
  ```

---

## §7 Salience — Four-Score Model

### Score construction

- [ ] **[M]** U score = empirical exceedance probability on the residual series
  ```bash
  grep -rn "u_score\|exceedance\|empirical_cdf\|percentile" backend/app/engine/ | grep -v ".pyc"
  ```

- [ ] **[M]** I score = 0..3 from event ontology (0=price-only, 1=BLOCK/INDEX, 2=CORP_ACTION/ANNOUNCE, 3=RESULTS)
  ```bash
  grep -rn "i_score\|I_score\|importance" backend/app/engine/ | grep -v ".pyc"
  ```

- [ ] **[M]** C score ∈ [0,1]; below threshold → suppressed (not down-ranked)
  ```bash
  grep -rn "confidence.*0\.3\|0\.3.*confidence\|suppress" backend/app/ | grep -v ".pyc"
  ```

- [ ] **[M]** Tier table gates: Tier A (U + I), Tier B (I only), Tier C (I only, low C)
  ```bash
  grep -rn "Tier.*A\|Tier.*B\|Tier.*C\|tier_a\|tier_b\|tier_c" backend/app/engine/ | grep -v ".pyc"
  ```

### Threshold calibration

- [ ] **[M]** h1, h2, U cut-points are set on held-out replay, not hard-coded as final
  ```bash
  grep -rn "calibrat\|held.out\|replay.*threshold\|threshold.*replay" backend/app/ configs/ | grep -v ".pyc"
  ```

---

## Gate 3 declaration

```
[ ] z-scores are sane on real data (evidence: zscore report in ACTION-LOG.md) → Gate 3 PASS
```

_Last updated: 2026-09-04_
