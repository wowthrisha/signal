# CHK-F1 — Data Ingest & Database (§5, §16)

**Gate 1 criterion (spec §25):** bars in DB, or drop to a single hardcoded CSV and continue.  
**Deadline:** T+4h  
**Evidence location:** `ops/ACTION-LOG.md` under `[F1]`

> A box is ticked only after the real command + real output are in ACTION-LOG.md. No output → no tick.

---

## §16 Data Strategy

### Bhavcopy downloader

- [ ] **[M]** Probe script returns 0 exit code on real trading day
  ```bash
  python scripts/probe_bhavcopy.py
  ```
  _Expected: "✓ Parsed N,NNN rows" where N > 1000_

- [ ] **[A]** Row-count assert fires on empty-body response (adversarial: simulate 200-OK with 0 rows)
  ```bash
  python -c "from backend.app.ingest.bhavcopy import parse_udiff; parse_udiff(b'TradDt,ISIN\n')"
  ```
  _Expected: AssertionError or ValueError with "rows > 1000" message_

- [ ] **[M]** URL pattern matches `configs/data_sources.json`
  ```bash
  python -c "import json; d=json.load(open('configs/data_sources.json')); print(d['bhavcopy_udiff']['url_pattern'])"
  ```

- [ ] **[M]** Fallback to `data/sample/` parquet works when network is unavailable
  ```bash
  # Simulate no network (or just verify the fallback path exists and is readable)
  ls data/sample/ && python -c "import pandas as pd; df=pd.read_parquet('data/sample/'); print(df.shape)"
  ```

### ISIN as canonical identity (§16)

- [ ] **[M]** Instrument master table populated; ISIN is the primary key
  ```bash
  psql $DATABASE_URL -c "SELECT count(*) FROM instrument;"
  ```
  _Expected: > 1000 rows_

- [ ] **[A]** Symbol rename does not fracture ISIN history
  ```bash
  psql $DATABASE_URL -c "SELECT isin, count(DISTINCT tckr_symb) FROM bar GROUP BY isin HAVING count(DISTINCT tckr_symb) > 1 LIMIT 3;"
  ```
  _Expected: rows exist (aliases tracked) — query should not error_

### Corporate-action adjustment (§16, R-04)

- [ ] **[M]** Adjustment factor applied before log-return computation
  ```bash
  grep -rn "adjust" backend/app/ingest/ | grep -v ".pyc"
  ```
  _Expected: at least one match showing corp-action adjustment call_

- [ ] **[A]** Unadjusted split would produce −90 % return — verify the adjustment path is tested
  ```bash
  python -m pytest backend/tests/ -k "corp_action or adjust" -v
  ```

---

## §5 Schema — bars in DB

### Schema applied

- [ ] **[M]** Schema migrations applied; `bar` table exists
  ```bash
  psql $DATABASE_URL -c "\d bar"
  ```

- [ ] **[M]** Gate 1 criterion satisfied: rows in `bar` table
  ```bash
  psql $DATABASE_URL -c "SELECT count(*) FROM bar;"
  ```
  _Expected: > 0 rows (> 1000 for a real ingest)_

### Data quality

- [ ] **[M]** No null ISIN rows
  ```bash
  psql $DATABASE_URL -c "SELECT count(*) FROM bar WHERE isin IS NULL;"
  ```
  _Expected: 0_

- [ ] **[M]** Session dates are UTC-stored, business key is `(session_date, isin)` unique
  ```bash
  psql $DATABASE_URL -c "SELECT count(*) FROM (SELECT session_date, isin, count(*) FROM bar GROUP BY 1,2 HAVING count(*) > 1) dups;"
  ```
  _Expected: 0_

- [ ] **[A]** Missing-bar forward-fill capped at 1 bar; beyond 1 → STALE status
  ```bash
  python -m pytest backend/tests/ -k "stale or forward_fill" -v
  ```

### Timestamps

- [ ] **[M]** No client-clock timestamps in bar rows — exchange timestamp only
  ```bash
  grep -rn "datetime.now" backend/app/ingest/ backend/app/db/
  ```
  _Expected: zero matches_

---

## Gate 1 declaration

```
[ ] All [M] checks above have output in ACTION-LOG.md → Gate 1 PASS
[ ] If bhavcopy download fails: hardcoded CSV fallback confirmed → Gate 1 PASS (fallback mode)
```

_Last updated: 2026-09-04_
