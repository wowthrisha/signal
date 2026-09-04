# CHK-U1 — Benchmark, Ablation & README (§14, §15, §30)

**Gate 6 criterion (spec §25):** B0/B1/B2 metrics auto-generated; if no numbers, cut ablation and ship 3 metrics.  
**Deadline:** T+46h  
**Evidence location:** `ops/ACTION-LOG.md` under `[U1]`

> A box is ticked only after the real command + real output are in ACTION-LOG.md. No output → no tick.
> Check types are defined in [VERIFY.md](VERIFY.md). `[G]` can never close a gate.

Reading a value out of a generated `metrics.json` is `[M]`: the artifact is an outcome of
running the pipeline. Grepping README prose is `[G]`: it proves someone typed a sentence.

---

## §14 Benchmark — the core differentiator

### Systems implemented

- [ ] **[G]** B0 tokens appear in the source
  ```bash
  grep -rn "B0\|baseline.*0\|fixed_threshold\|0\.03" backend/app/ | grep -v ".pyc" | grep -i "baseline\|b0"
  ```
  _Navigation hint only._

- [ ] **[G]** B1 tokens appear in the source
  ```bash
  grep -rn "B1\|baseline.*1\|ewma.*z\|z.*2.*raw" backend/app/ | grep -v ".pyc" | grep -i "b1\|baseline"
  ```
  _Navigation hint only._

- [ ] **[G]** B2 tokens appear in the source
  ```bash
  grep -rn "B2\|residual.*D1\|D1.*D2\|salience" backend/app/ | grep -v ".pyc" | head -5
  ```
  _Navigation hint only._

- [ ] **[M]** All three systems run end-to-end on the same replay window and emit distinct
  alert counts — a baseline that silently returns the same set as B2 is not a baseline
  ```bash
  make evaluate > /dev/null 2>&1
  python3 -c "
import json, glob
m = json.load(open(sorted(glob.glob('results/*/metrics.json'))[-1]))
counts = {k: m[k] for k in ('B0_alerts','B1_alerts','B2_alerts')}
print(counts)
assert len(set(counts.values())) == 3, 'two systems produced identical alert counts'
print('PASS — three distinct systems')
"
  ```

### Metrics generated (all auto — never hand-typed)

- [ ] **[M]** `make evaluate` runs and produces `results/<timestamp>/metrics.json`
  ```bash
  make evaluate 2>&1 | tail -10
  ls results/ | tail -1 | xargs -I{} ls results/{}
  ```
  _Expected: metrics.json, ablation.md, alerts.csv present_

- [ ] **[M]** The results directory is freshly generated, not committed by hand
  ```bash
  LATEST=$(ls -t results/*/metrics.json | head -1)
  echo "$LATEST"
  git ls-files --error-unmatch "$LATEST" 2>/dev/null && echo "FAIL — metrics.json is committed" || echo "PASS — generated, not committed"
  ```

- [ ] **[M]** `alerts_per_user_day` metric in metrics.json
  ```bash
  python3 -c "import json,glob; d=json.load(open(sorted(glob.glob('results/*/metrics.json'))[-1])); print(d['alerts_per_user_day'])"
  ```

- [ ] **[M]** `alert_reduction_vs_B0` metric in metrics.json (most honest headline)
  ```bash
  python3 -c "import json,glob; d=json.load(open(sorted(glob.glob('results/*/metrics.json'))[-1])); print(d['alert_reduction_vs_B0'])"
  ```

- [ ] **[M]** `event_coverage` metric (fraction of I≥2 events surfaced)
  ```bash
  python3 -c "import json,glob; d=json.load(open(sorted(glob.glob('results/*/metrics.json'))[-1])); print(d['event_coverage'])"
  ```

- [ ] **[M]** `market_day_alert_count` metric (alerts on 5 largest |index return| sessions)
  ```bash
  python3 -c "import json,glob; d=json.load(open(sorted(glob.glob('results/*/metrics.json'))[-1])); print(d['market_day_alert_count'])"
  ```

- [ ] **[M]** Metrics are reproducible — two consecutive `make evaluate` runs on the same
  window produce identical metric values
  ```bash
  make evaluate > /dev/null 2>&1; A=$(python3 -c "import json,glob;print(json.dumps(json.load(open(sorted(glob.glob('results/*/metrics.json'))[-1])),sort_keys=True))")
  make evaluate > /dev/null 2>&1; B=$(python3 -c "import json,glob;print(json.dumps(json.load(open(sorted(glob.glob('results/*/metrics.json'))[-1])),sort_keys=True))")
  [ "$A" = "$B" ] && echo "PASS — metrics reproducible" || { echo "FAIL — metrics drifted"; diff <(echo "$A") <(echo "$B"); }
  ```

### Ground truth limitations stated

- [ ] **[G]** README contains the limitations sentences
  ```bash
  grep -n "meaningfulness\|no ground truth\|partially endogenous\|CAR.*endogenous" README.md
  ```
  _Navigation hint only._

- [ ] **[G]** README claims to be auto-generated
  ```bash
  grep -n "AUTO-GENERATED\|auto.generated\|make evaluate\|DO NOT EDIT" README.md
  ```
  _Navigation hint only — the banner can be typed above hand-written numbers._

- [ ] **[M]** Every number in the README results table is present in the generated
  `metrics.json` — proves the table was not hand-typed
  ```bash
  cd backend && python -m pytest tests/test_readme_numbers.py -v
  ```
  _Expected: passes. Any figure in README that does not appear in metrics.json fails the test._

---

## §15 Ablation Matrix

- [ ] **[G]** Ablation run identifiers appear in the configs
  ```bash
  grep -rn "ablation\|run.*A\|run.*B\|run.*C\|run.*D\|run.*E\|run.*F" configs/ | grep -v ".pyc"
  ```
  _Navigation hint only._

- [ ] **[M]** All six ablation runs A–F actually executed and produced rows
  ```bash
  python3 -c "
import json, glob
m = json.load(open(sorted(glob.glob('results/*/metrics.json'))[-1]))
rows = m['ablation']
print(sorted(rows))
missing = set('ABCDEF') - set(rows)
assert not missing, f'ablation rows never ran: {sorted(missing)}'
print('PASS — all six runs present')
"
  ```

- [ ] **[M]** `ablation.md` is regenerated by `make evaluate`, not stale — its mtime is newer
  than the run that produced it
  ```bash
  BEFORE=$(ls -t results/*/ablation.md 2>/dev/null | head -1)
  make evaluate > /dev/null 2>&1
  AFTER=$(ls -t results/*/ablation.md | head -1)
  echo "before: $BEFORE"; echo "after:  $AFTER"
  [ "$BEFORE" != "$AFTER" ] && echo "PASS — regenerated" || echo "FAIL — ablation.md is stale"
  cat "$AFTER"
  ```

### Pre-commitment written before numbers seen

- [ ] **[G]** The pre-commitment sentence is present
  ```bash
  grep -n "willing to lose\|removed before submission\|pre.commit" README.md docs/DECISIONS.md
  ```
  _Navigation hint only._

- [ ] **[A]** The pre-commitment predates the numbers — its commit is older than the first
  metrics.json commit
  ```bash
  git log --diff-filter=A --format="%ad %h pre-commitment" --date=short -1 -S "willing to lose" -- docs/DECISIONS.md README.md
  git log --format="%ad %h first-metrics" --date=short -1 -- results/
  ```
  _Expected: the pre-commitment date is the earlier of the two_

- [ ] **[A]** If any ablation row does not improve at least one metric without degrading another, it is dropped
  ```bash
  cat $(ls -t results/*/ablation.md 2>/dev/null | head -1) | grep -E "DROPPED|DROP|removed"
  ```

---

## §30 README Structure

- [ ] **[G]** README section headings are present and in order
  ```bash
  grep -n "^## \|^# " README.md | head -20
  ```
  _Navigation hint only._
  _Expected sections: What this is · Run it · Problem decomposed · How "meaningful" is defined · Results · What we do NOT claim · Architecture · Edge cases · What we did not build · docs/DECISIONS.md_

- [ ] **[G]** "What we do NOT claim" appears
  ```bash
  grep -n "do NOT claim\|do not claim\|NOT claim" README.md
  ```
  _Navigation hint only._

- [ ] **[M]** The README really has every required §30 section, in order, and none of
  them is an empty stub
  ```bash
  cd backend && python -m pytest tests/test_readme_structure.py -v
  ```
  _Expected: passes. Asserts section order and a non-trivial body under each heading._

- [ ] **[G]** 12 ADR headings are present in docs/DECISIONS.md
  ```bash
  grep -c "^## ADR-" docs/DECISIONS.md
  ```
  _Navigation hint only — a count does not prove the ADRs are the required twelve._

- [ ] **[M]** The twelve required ADR ids are each present, by id
  ```bash
  python3 -c "
import re, pathlib
text = pathlib.Path('docs/DECISIONS.md').read_text()
found = set(re.findall(r'^## (ADR-\d+)', text, re.M))
need = {f'ADR-{i:03d}' for i in range(1, 13)}
missing = sorted(need - found)
print('found:', len(found), 'missing:', missing)
assert not missing, f'missing ADRs: {missing}'
print('PASS — all 12 ADRs present')
"
  ```

- [ ] **[A]** Fresh-clone README instructions work end-to-end (Gate 8 prerequisite, R-08)
  ```bash
  TMP=$(mktemp -d)
  git clone --depth 1 file:///Users/thrisha/code/signal "$TMP/signal"
  cd "$TMP/signal" && docker compose up -d --build
  sleep 30
  curl -sf http://localhost:8000/health && echo "API UP"
  curl -sf http://localhost:5173/ > /dev/null && echo "UI UP"
  docker compose down -v
  ```
  _Expected: both UP with zero manual steps. Run on a machine that has never built this repo._

---

## Gate 6 declaration

```
[ ] metrics.json auto-generated with at least alert_reduction_vs_B0, alerts_per_user_day,
    and event_coverage → Gate 6 PASS
[ ] If ablation incomplete: cut ablation.md, ship 3 metrics only → Gate 6 PASS (reduced scope)
```

Gate 6 may not be closed on any `[G]` evidence.

_Last updated: 2026-09-04_
