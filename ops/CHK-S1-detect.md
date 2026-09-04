# CHK-S1 — Detection: Attribution, EWMA, D1/D2 (§4, §7, §8)

**Gate 3 criterion (spec §25):** z-scores sane on real data.  
**Deadline:** T+20h  
**Evidence location:** `ops/ACTION-LOG.md` under `[S1]`

> A box is ticked only after the real command + real output are in ACTION-LOG.md. No output → no tick.
> Check types are defined in [VERIFY.md](VERIFY.md). `[G]` can never close a gate.

**Environment for every command below:**

```bash
cd /Users/thrisha/code/signal
export DATABASE_URL="postgresql://signal:signal@localhost:5433/signal"
```

Module paths are `app.*` run from `backend/`. There is no top-level `signal` package.

This phase is where grep is most seductive and least useful: every parameter below is a
number that must be *observed in behaviour*, because a constant can be present in source
and never reach the code path that matters.

---

## §8 Attribution — Orthogonalized Two-Factor OLS

### Regression correctness

- [ ] **[G]** Orthogonalization tokens appear in the engine
  ```bash
  grep -rn "r_sec_perp\|sec_perp\|ortho\|orthogonalized" backend/app/engine/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** `r_sec⊥` really is the residual of sector ~ market — assert it is orthogonal to
  the market series (|corr| ≈ 0) on real ingested data
  ```bash
  cd backend && python -m pytest tests/test_attribution.py -k "orthogonal" -v
  ```
  _Expected: passes, with |corr(r_mkt, r_sec_perp)| < 1e-8. Raw sector return would show a large correlation._

- [ ] **[G]** The numbers 120 / 60 appear near window settings
  ```bash
  grep -rn "120\|60" backend/app/engine/ | grep -i "window\|min_obs\|min_history"
  ```
  _Navigation hint only._

- [ ] **[M]** Estimation window is actually 120 sessions with a 60-bar minimum — feed 59 bars
  and assert no estimate is produced; feed 60 and assert one is
  ```bash
  cd backend && python -m pytest tests/test_attribution.py -k "window or min_obs" -v
  ```

- [ ] **[G]** Shrinkage tokens appear in the engine
  ```bash
  grep -rn "shrink\|n.*n.*60\|sector_mean" backend/app/engine/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** IPO shrinkage weight is `w = n/(n+60)` — assert the returned beta for n=60 sits
  exactly halfway between the raw estimate and the sector mean
  ```bash
  cd backend && python -m pytest tests/test_attribution.py -k "shrink" -v
  ```

- [ ] **[A]** Beta decomposition sums to total return (decomposition is exact)
  ```bash
  cd backend && python -m pytest tests/ -k "attribution or decomp" -v
  ```

### Market-wide crash suppression (§8)

- [ ] **[G]** Breadth / MARKET_REGIME tokens appear in the engine
  ```bash
  grep -rn "breadth\|MARKET_REGIME" backend/app/engine/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** Breadth guard fires at the threshold — synthesise a session with 51 % of symbols
  at |z| > 2 and assert exactly one MARKET_REGIME event and zero JUMP events
  ```bash
  cd backend && python -m pytest tests/test_breadth.py -v
  ```

- [ ] **[A]** On a crash session (>50% of symbols with |z| > 2): output is exactly one MARKET_REGIME event
  ```bash
  cd backend && python -m pytest tests/ -k "market_regime or crash" -v
  ```

---

## §4 Detector Specification — D1 (Jump) and D2 (CUSUM)

### EWMA volatility

- [ ] **[G]** λ = 0.94 / ewma tokens appear in the engine
  ```bash
  grep -rn "0.94\|lambda.*0.9\|ewma" backend/app/engine/ | grep -v ".pyc"
  ```
  _Navigation hint only — passes on `# TODO: use ewma`._

- [ ] **[M]** The recursion really is `σ²_t = 0.94·σ²_{t-1} + 0.06·r²_t` — compare engine output
  against an independently computed series
  ```bash
  cd backend && python -m pytest tests/test_ewma.py -k "recursion" -v
  ```

- [ ] **[G]** Volatility-floor tokens appear in the engine
  ```bash
  grep -rn "sigma_floor\|σ_floor\|vol_floor\|percentile" backend/app/engine/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** The floor binds — feed a near-zero-variance symbol and assert σ̂ never falls below
  the 5th percentile of the cross-sectional σ
  ```bash
  cd backend && python -m pytest tests/test_ewma.py -k "floor" -v
  ```

### D1 — Jump detector

- [ ] **[G]** h1 / 3.0 tokens appear in the engine
  ```bash
  grep -rn "h1\|h_1\|jump.*3\.0\|3\.0.*jump" backend/app/engine/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** D1 fires at |z| ≥ 3.0 and not at 2.99 — boundary test
  ```bash
  cd backend && python -m pytest tests/test_d1.py -k "threshold or boundary" -v
  ```

- [ ] **[G]** Gap/overnight tokens appear in the engine
  ```bash
  grep -rn "gap\|overnight\|weekend" backend/app/engine/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** An overnight gap raises D1 but leaves the CUSUM accumulator unchanged
  ```bash
  cd backend && python -m pytest tests/test_d1.py -k "gap" -v
  ```

- [ ] **[A] GATE 3:** z-scores sane on real bhavcopy data
  ```bash
  cd backend && python -m app.detect --date 2026-09-03 --report-zscore | head -20
  ```
  _Expected: z-scores roughly Gaussian, |mean| < 0.5, std 0.8–1.5_

- [ ] **[M] GATE 3:** the same distribution, asserted rather than eyeballed
  ```bash
  cd backend && python -m app.detect --date 2026-09-03 --report-zscore --assert-sane; echo "exit=$?"
  ```
  _Expected: `exit=0`. Non-zero if |mean| ≥ 0.5 or std outside 0.8–1.5._

### D2 — CUSUM drift detector (§4)

- [ ] **[G]** CUSUM parameter tokens appear in the engine
  ```bash
  grep -rn "k.*0\.5\|h2\|h_2\|4\.0.*cusum\|cusum.*4\.0" backend/app/engine/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** k = 0.5 and h2 = 4.0 are the values actually in force — drive a known ramp and
  assert the alarm bar index matches the hand-computed one
  ```bash
  cd backend && python -m pytest tests/test_d2.py -k "params or ramp" -v
  ```

- [ ] **[G]** Two-sided accumulator tokens appear in the engine
  ```bash
  grep -rn "S_plus\|S_pos\|S_neg\|S_minus\|s_plus\|s_minus" backend/app/engine/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** Both arms work and only the firing arm resets — drive a negative drift, assert
  S⁻ alarms, S⁻ resets to 0, and S⁺ is left untouched
  ```bash
  cd backend && python -m pytest tests/test_d2.py -k "two_sided or reset" -v
  ```

- [ ] **[G]** Cooldown tokens appear in the engine
  ```bash
  grep -rn "cooldown\|cool_down\|suppress.*3\|3.*bar" backend/app/engine/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** Cooldown suppresses for exactly 3 bars — sustained drift yields alarms at bars
  t, t+4, t+8, never t+1..t+3
  ```bash
  cd backend && python -m pytest tests/test_d2.py -k "cooldown" -v
  ```

- [ ] **[G]** A D1-only / disable-CUSUM flag exists somewhere
  ```bash
  grep -rn "D1_ONLY\|d1_only\|disable_cusum\|cusum.*flag" backend/app/ configs/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[A]** Gate 4 cut rule is executable: running with D2 disabled produces a strictly
  smaller event set and still completes
  ```bash
  cd backend
  psql $DATABASE_URL -t -A -c "SELECT count(*) FROM event WHERE event_type='DRIFT';"
  python -m app.detect --date 2026-09-03 --d1-only; echo "exit=$?"
  psql $DATABASE_URL -t -A -c "SELECT count(*) FROM event WHERE event_type='DRIFT';"
  ```
  _Expected: exit=0 and the DRIFT count does not grow_

### Warm-up states (§4)

- [ ] **[G]** WARMUP tokens appear in the engine
  ```bash
  grep -rn "WARMUP\|warmup\|60\|n_obs" backend/app/engine/ | grep "warmup\|WARMUP"
  ```
  _Navigation hint only._

- [ ] **[M]** A symbol with n_obs = 59 is WARMUP: D2 emits nothing and every emitted
  confidence is ≤ 0.5
  ```bash
  cd backend && python -m pytest tests/test_warmup.py -k "warmup" -v
  ```

- [ ] **[G]** An n_obs < 20 guard appears in the engine
  ```bash
  grep -rn "n_obs.*20\|20.*n_obs\|min.*20" backend/app/engine/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** A symbol with n_obs = 19 emits zero events of any type
  ```bash
  cd backend && python -m pytest tests/test_warmup.py -k "no_detection" -v
  ```

### Missing data (§4)

- [ ] **[G]** STALE / forward-fill tokens appear in the source
  ```bash
  grep -rn "STALE\|forward_fill\|ffill" backend/app/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** One missing bar is filled; two consecutive missing bars flip status to STALE
  and force confidence to 0
  ```bash
  cd backend && python -m pytest tests/test_stale.py -v
  ```

- [ ] **[A]** Missing data test: drop one bar from a symbol's history, verify STALE event emitted
  ```bash
  cd backend && python -m pytest tests/ -k "stale or missing" -v
  ```

---

## §7 Salience — Four-Score Model

- [ ] **[G]** U-score tokens appear in the engine
  ```bash
  grep -rn "u_score\|exceedance\|empirical_cdf\|percentile" backend/app/engine/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** U is an empirical exceedance probability: it lies in [0,1] and its distribution
  over a real session is approximately uniform
  ```bash
  cd backend && python -m pytest tests/test_salience.py -k "u_score" -v
  ```

- [ ] **[G]** I-score tokens appear in the engine
  ```bash
  grep -rn "i_score\|I_score\|importance" backend/app/engine/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** I maps the ontology exactly: price-only→0, BLOCK/INDEX→1, CORP_ACTION/ANNOUNCE→2,
  RESULTS→3, and no event escapes 0..3
  ```bash
  cd backend && python -m pytest tests/test_salience.py -k "i_score" -v
  psql $DATABASE_URL -c "SELECT min(i_score), max(i_score) FROM event;"
  ```
  _Expected: min ≥ 0, max ≤ 3_

- [ ] **[G]** Confidence-threshold tokens appear in the source
  ```bash
  grep -rn "confidence.*0\.3\|0\.3.*confidence\|suppress" backend/app/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** Below-threshold events are suppressed, not down-ranked — no event with
  confidence < 0.3 ever reaches a digest response
  ```bash
  cd backend && python -m pytest tests/test_salience.py -k "suppress" -v
  psql $DATABASE_URL -c "SELECT count(*) FROM event WHERE confidence < 0.3;"
  ```
  _Expected: such rows may exist in the ledger but must never appear in a digest payload_

- [ ] **[G]** Tier tokens appear in the engine
  ```bash
  grep -rn "Tier.*A\|Tier.*B\|Tier.*C\|tier_a\|tier_b\|tier_c" backend/app/engine/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** Tier gates route correctly — a table-driven test over the §7 tier matrix
  ```bash
  cd backend && python -m pytest tests/test_salience.py -k "tier" -v
  ```

### Threshold calibration

- [ ] **[G]** Calibration tokens appear in source or configs
  ```bash
  grep -rn "calibrat\|held.out\|replay.*threshold\|threshold.*replay" backend/app/ configs/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** Thresholds are read from the calibration output, not baked in — change the
  calibration file and observe the firing rate change
  ```bash
  cd backend && python -m pytest tests/test_calibration.py -v
  ```

---

## Gate 3 declaration

```
[ ] z-scores are sane on real data — the --assert-sane run exits 0, output in ACTION-LOG.md → Gate 3 PASS
```

Gate 3 may not be closed on any `[G]` evidence.

_Last updated: 2026-09-04_
